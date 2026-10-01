"""Turn page images into text the grader can read.

Some assignments are answered on paper. A photographed or scanned page carries
no text layer, so the extractor refuses it rather than grading a blank -- this
module is what turns that refusal into a transcript.

WHY THIS IS NOT JUST "ADD OCR"

The grading pipeline's strongest guarantee is that every award quotes the
student's own words and the app checks the quote really appears in the
submission. Against a transcript that guarantee goes CIRCULAR: the quote is
checked against a machine's guess at the writing, so a misreading that costs a
student marks is confirmed rather than caught. Nothing here can fix that. What
it can do is refuse to hide it:

- text that came from a machine is labelled as such, all the way to the review
  queue and the Excel export;
- the transcript keeps the reader's own uncertainty markers instead of a clean
  guess, so a faculty member can see where it struggled;
- rows built on a transcript are pushed to the top of the review queue and
  never look as settled as rows read from a real text layer.

A second failure mode is subtler and specific to using a language model as the
reader: asked to transcribe, it tends to CORRECT. It fixes the spelling, tidies
the grammar, and repairs the broken formula -- handing marks to a student who
did not earn them, invisibly, because the grader downstream only ever sees the
improved version. TRANSCRIPTION_PROMPT fights that, and the tests check it.

Backends, chosen with OCR_BACKEND:

  none    the default. Images stay unreadable and the submission is refused,
          which is the behaviour this app had before.
  vision  a vision-capable model on the configured ReaLLM gateway. No new
          vendor and no new data path for student work, so nothing new to
          clear with UITS. Reports no per-word confidence, so uncertainty
          comes only from the markers the model is asked to leave.
  azure   Azure AI Document Intelligence (prebuilt-read). Purpose-built for
          handwriting and the only backend that reports per-word confidence,
          which is what lets a doubtful reading be flagged rather than
          guessed at. Sends student work to a new service -- check with UITS
          before enabling it.
"""
import base64
import hashlib
import itertools
import logging
import re
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

logger = logging.getLogger(__name__)

BACKEND = os.getenv("OCR_BACKEND", "none").strip().lower()

# Anything below this is reported as doubtful. Azure's per-word confidence on
# clean handwriting sits well above it; a word this uncertain is one a faculty
# member should look at rather than one the grader should act on.
LOW_CONFIDENCE = float(os.getenv("OCR_LOW_CONFIDENCE", "0.75"))

# A page is a big image and a slow request. Beyond this a submission is almost
# certainly not what this tool is for, and the cost and latency stop being
# reasonable for a whole roster.
MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "20"))

# A 31B model reading a dense page of handwriting is slow, and 120s was not
# enough: a real submission timed out on page 2 of 11. Transcription is already
# a background job, so waiting is far cheaper than losing the page.
REQUEST_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "240"))

# One slow page should not cost a student their whole submission. The LLM client
# has retried transient failures since the beginning; the image reader did not,
# so a single read timeout discarded eleven pages of work.
# Pages are transcribed concurrently. This matters more than it looks: the
# gateway's latency has a long tail -- on a real submission two pages of eleven
# took over 240s while the rest were quick -- so a serial run is hostage to its
# worst pages and takes their SUM. In parallel a submission costs roughly its
# slowest page instead. Conservative by default because the gateway's
# concurrency limit is unknown; raise it once IU confirms what they allow.
CONCURRENCY = max(1, int(os.getenv("OCR_CONCURRENCY", "4")))

# The default must match GRADING_CONCURRENCY in app/routes/rubric.py, which is
# the real number of submissions in flight. It is read from the environment
# here rather than imported because importing the route module from the reader
# would be a cycle. scripts/test_concurrency.py pins the two together so they
# cannot drift apart silently.
_SUBMISSIONS_IN_FLIGHT = max(1, int(os.getenv("GRADING_CONCURRENCY", "3")))

# A ceiling on calls in flight to the gateway across the WHOLE run, so a roster
# cannot open more connections than IU has agreed to. It defaults to the
# product, which throttles nothing: every submission being graded gets its full
# page budget, exactly as it would if it were the only one.
#
# It defaulted to CONCURRENCY alone for one commit, and that was a mistake
# worth remembering. CONCURRENCY is tuned for ONE submission's pages, so
# sharing that single budget between three of them made every handwritten
# submission twice as slow while the batch as a whole looked faster. A total
# that does not scale with the number of submissions is not a budget, it is a
# queue. Lower this deliberately if IU names a limit; do not lower it by
# accident.
MAX_IN_FLIGHT = max(
    1,
    int(os.getenv("OCR_MAX_IN_FLIGHT", str(CONCURRENCY * _SUBMISSIONS_IN_FLIGHT))),
)
_GATEWAY_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT)


def set_concurrency(limit, max_in_flight=None):
    """Change the page budget, and optionally the gateway ceiling. For benchmarks.

    The semaphore is built once at import, so assigning to CONCURRENCY alone
    changes the size of the thread pools and nothing else -- every thread would
    still queue behind the original ceiling, and a benchmark measuring the
    concurrency lever would report that it does nothing.
    """
    global CONCURRENCY, MAX_IN_FLIGHT, _GATEWAY_SLOTS
    CONCURRENCY = max(1, int(limit))
    MAX_IN_FLIGHT = max(1, int(max_in_flight if max_in_flight is not None
                              else CONCURRENCY * _SUBMISSIONS_IN_FLIGHT))
    _GATEWAY_SLOTS = threading.BoundedSemaphore(MAX_IN_FLIGHT)

MAX_ATTEMPTS = int(os.getenv("OCR_MAX_ATTEMPTS", "3"))
BACKOFF_SECONDS = float(os.getenv("OCR_BACKOFF_SECONDS", "3"))


class OCRError(Exception):
    """Reading failed. The caller must refuse to grade, not grade a blank."""


# The instruction that decides whether a transcript is evidence or fiction.
TRANSCRIPTION_PROMPT = """\
You are transcribing a page of a student's handwritten or scanned assignment so \
that it can be graded. You are NOT grading it and NOT answering it.

TRANSCRIBE EXACTLY WHAT IS ON THE PAGE.

- Copy the words as written. Keep the student's own spelling, grammar, notation
  and terminology.
- DO NOT CORRECT MISTAKES. If the student wrote a misspelled word, an incorrect
  formula, a wrong variable name or a false statement, transcribe it wrong. A
  mistake you silently repair becomes marks the student did not earn, and
  nobody downstream can tell you did it. This is the most important rule here.
- Do not complete unfinished work, do not reorder anything, and do not add
  anything that is not on the page.
- Keep the layout that carries meaning: line breaks, numbered answers,
  indentation in code, the rows of a table.

MARK WHAT YOU CANNOT READ.

- A word you cannot make out at all: [illegible]
- A word you can only guess at: [?guess] -- for example [?binary]
- Text the student crossed out: [struck: the crossed out words]. Do not silently
  drop it and do not silently keep it; the grader decides whether it counts.
- A CORRECTION is a crossing-out with the replacement written beside or above
  it, and it is the case most often got wrong. Mark the deletion and keep the
  replacement: a student who struck "outer" and wrote "inner" must transcribe
  as "[struck: outer] inner", NEVER as "outer inner", which reads as a garbled
  sentence and costs them the mark they earned by correcting it.

DESCRIBE WHAT IS NOT TEXT.

- A diagram, drawing, tree, graph or plot cannot be transcribed. Write
  [diagram: a short factual description of what is drawn], describing only what
  is actually there. Do not interpret whether it is correct.
- SOME SUBMISSIONS ARE A FIGURE, NOT A PAGE OF WRITING -- an exported chart, a
  screenshot, a photographed diagram. Describe it the same way, and be
  specific about the things a grader has to check: the title, the axis labels
  and their units, how many bars or series there are and what each is labelled,
  and the rough shape of the result (which is largest, whether one is near
  zero). Transcribe any text that appears in the image exactly. Do not estimate
  precise values off an axis and do not say whether the result looks right.

WRITE PLAIN TEXT, NOT MARKUP.

- No LaTeX and no Markdown. Write what the symbol is, not how to typeset it:
  "<-" or "←" for an assignment arrow, "Theta(n^2)" or "Θ(n^2)", "<=", "therefore"
  or "∴", "sqrt(N)". Never $\leftarrow$, $\Theta$, $\le$, \therefore or $...$.
- Be CONSISTENT across pages: the same symbol must transcribe the same way on
  page 9 as on page 1. A grader comparing two answers cannot tell a difference
  in the student's notation from a difference in yours.
- Keep the student's own notation. If they wrote "lg", write "lg", not "log";
  if they wrote "Θ", do not substitute "O". Those distinctions carry marks.

Output the transcription only. No preamble, no commentary, no grading.\
"""


# <loc_0><loc_120>, <_unknown>, </line_code>, <otsl>... -- Granite Docling's
# layout vocabulary. They describe where text sits on the page, which the
# grader has no use for and would otherwise read as part of the answer.
_DOCTAG_RE = re.compile(r"</?(?:loc_\d+|[a-z_]+)>")


def strip_doctags(text):
    """Remove document-conversion markup, keeping the text it wrapped."""
    if "<loc_" not in text:
        return text
    cleaned = _DOCTAG_RE.sub(" ", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def is_enabled():
    return BACKEND in ("vision", "azure")


def backend_name():
    return BACKEND


class PageText:
    """One transcribed page, with whatever the backend knows about its doubts."""

    def __init__(self, index, text, confidence=None, doubtful_words=()):
        self.index = index
        self.text = text or ""
        # 0..1, or None when the backend does not report confidence at all.
        self.confidence = confidence
        self.doubtful_words = list(doubtful_words)

    @property
    def marker_count(self):
        """Uncertainty the reader flagged in the text itself."""
        return self.text.count("[illegible]") + self.text.count("[?")


def _cache_key(image):
    """Identify a page by its pixels and the model that will read it.

    The model is part of the key: the same page read by a different
    transcriber is a different transcript, and serving the old one would
    silently undo a deliberate change of reader.
    """
    digest = hashlib.sha256(image).hexdigest()
    return f"{BACKEND}:{VISION_MODEL or '-'}:{digest}"


def read_pages(images, context="", progress=None, cache=None):
    """Transcribe page images. Returns [PageText]; raises OCRError if it cannot.

    `images` is a list of raw image bytes, in page order. `context` is a short
    hint (the assignment name) that helps a vision model with proper nouns and
    notation; it must never contain an answer.

    `progress(done, total)` is called as each page lands. A submission can take
    minutes, and without it the only thing anyone can see is that nothing has
    happened yet -- which is indistinguishable from a crash.

    `cache`, when given, is an object with get(key) and set(key, text). Pages
    already transcribed are not read again, which is what lets a run that was
    killed part way resume instead of starting over.
    """
    if not is_enabled():
        raise OCRError(
            "This submission is an image, and image reading is turned off. Set "
            "OCR_BACKEND to enable it, or ask the student to submit a text-based file."
        )
    if not images:
        raise OCRError("No page images to read.")
    if len(images) > MAX_PAGES:
        raise OCRError(
            f"This submission has {len(images)} pages, over the {MAX_PAGES}-page "
            f"limit for image reading."
        )

    reader = _read_page_vision if BACKEND == "vision" else _read_page_azure
    numbered = list(enumerate(images, start=1))

    total = len(numbered)
    done = itertools.count(1)
    lock = threading.Lock()

    def read_one(pair):
        index, image = pair
        key = _cache_key(image) if cache else None
        if key:
            cached = cache.get(key)
            if cached is not None:
                logger.info("Page %d already transcribed; reusing it", index)
                if progress:
                    with lock:
                        progress(next(done), total)
                return PageText(index, cached)

        page = _read_with_retries(reader, image, index, context)
        if key:
            cache.set(key, page.text)
        if progress:
            with lock:
                progress(next(done), total)
        return page

    if CONCURRENCY == 1 or total == 1:
        return [read_one(pair) for pair in numbered]

    workers = min(CONCURRENCY, len(numbered))
    logger.info("Transcribing %d pages, %d at a time", len(numbered), workers)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map keeps input order, so pages come back in page order however they
        # finish. An OCRError from any page surfaces when its result is read,
        # and the submission is refused rather than graded with a hole in it.
        return list(pool.map(read_one, numbered))


def _read_with_retries(reader, image, index, context):
    """Read one page, retrying the failures that are usually transient.

    A timeout or a dropped connection on one page is not a verdict on the
    submission, and without this the first slow page throws the other ten away.
    An OCRError is not retried: it means the reader answered and could not do
    the job, which a second identical request will not change.
    """
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with _GATEWAY_SLOTS:
                return reader(image, index, context)
        except OCRError:
            raise
        except requests.RequestException as exc:
            last_error = exc
            logger.warning("Page %d transcription attempt %d/%d failed: %s",
                           index, attempt, MAX_ATTEMPTS, exc)
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_SECONDS * (2 ** (attempt - 1)))
    raise OCRError(
        f"Could not reach the image reader for page {index} after "
        f"{MAX_ATTEMPTS} attempts: {last_error}")


# --- the ReaLLM gateway, with a vision-capable model --------------------------

# Measured against IU's gateway, not guessed: gpt-oss-120b and glm-5.2 report
# no vision (glm-5.2 refuses outright as "not a multimodal model"), while
# gemma-4-31B-it and granite-docling-258m both read a code out of a test image.
# gemma is the default because it follows an instruction prompt, which is what
# the transcription rules depend on; granite-docling is a 258M document-
# conversion model, far cheaper per page and a better fit for scanned PRINT,
# but it emits DocTags rather than prose. Re-run scripts/probe_vision.py before
# trusting any other name here.
DEFAULT_VISION_MODEL = "gemma-4-31B-it"
VISION_MODEL = (os.getenv("OCR_VISION_MODEL")
                or (DEFAULT_VISION_MODEL if BACKEND == "vision" else None)
                or os.getenv("REALLM_MODEL_NAME"))

# IU's gateway is LiteLLM in front of vLLM and accepts only OpenAI-shaped
# messages: the Anthropic block comes back as a 500 on every page. Off by
# default so a roster does not pay for a failed request per page, but kept for
# gateways that want the other shape.
TRY_ANTHROPIC_SHAPE = os.getenv("OCR_TRY_ANTHROPIC_SHAPE", "0") == "1"


def _media_type(image):
    """JPEG for rendered pages, PNG for an uploaded screenshot."""
    return "image/jpeg" if image[:3] == b"\xff\xd8\xff" else "image/png"


def _data_uri(image):
    return (f"data:{_media_type(image)};base64,"
            + base64.b64encode(image).decode("ascii"))


def _vision_payloads(image, index, context):
    """Gateways disagree on how an image is attached, so try both shapes.

    The same two shapes scripts/probe_vision.py tries, for the same reason:
    OpenAI-compatible gateways take image_url, Anthropic-compatible ones take a
    base64 source block, and a gateway that does not understand the one it is
    sent usually answers as though there were no image at all.
    """
    instruction = f"Transcribe page {index}." + (f" Assignment: {context}." if context else "")
    shapes = [[{"type": "text", "text": instruction},
               {"type": "image_url", "image_url": {"url": _data_uri(image)}}]]
    if TRY_ANTHROPIC_SHAPE:
        encoded = base64.b64encode(image).decode("ascii")
        shapes.append([{"type": "text", "text": instruction},
                       {"type": "image", "source": {"type": "base64",
                                                    "media_type": _media_type(image),
                                                    "data": encoded}}])
    return shapes


def _read_page_vision(image, index, context):
    url = os.getenv("REALLM_URL")
    key = os.getenv("REALLM_API_KEY")
    if not url or not VISION_MODEL:
        raise OCRError("OCR_BACKEND=vision but REALLM_URL / model name are not configured.")

    last_error = None
    for content in _vision_payloads(image, index, context):
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": VISION_MODEL,
                "messages": [
                    {"role": "system", "content": TRANSCRIPTION_PROMPT},
                    {"role": "user", "content": content},
                ],
                # Transcription is not a creative task; the same page must
                # transcribe the same way on a re-run or a re-grade changes a
                # grade for no reason the student could understand.
                "temperature": 0,
            },
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code != 200:
            last_error = f"{response.status_code}: {response.text[:200]}"
            continue
        try:
            message = response.json()["choices"][0]["message"]
            text = (message.get("content") or "").strip()
        except (ValueError, KeyError, IndexError) as exc:
            last_error = f"unparseable response ({exc})"
            continue
        if text:
            # No per-word confidence from a chat model; the markers it was asked
            # to leave are the only uncertainty signal available.
            return PageText(index, strip_doctags(text))
        last_error = "the model returned nothing for this page"

    raise OCRError(f"Image reading failed on page {index} ({last_error}).")


# --- Azure AI Document Intelligence (prebuilt-read) ---------------------------

AZURE_ENDPOINT = (os.getenv("AZURE_DI_ENDPOINT") or "").rstrip("/")
AZURE_KEY = os.getenv("AZURE_DI_KEY")
AZURE_API_VERSION = os.getenv("AZURE_DI_API_VERSION", "2024-11-30")
AZURE_POLL_SECONDS = float(os.getenv("AZURE_DI_POLL_SECONDS", "1.5"))
AZURE_POLL_ATTEMPTS = int(os.getenv("AZURE_DI_POLL_ATTEMPTS", "40"))


def _read_page_azure(image, index, context):
    if not AZURE_ENDPOINT or not AZURE_KEY:
        raise OCRError("OCR_BACKEND=azure but AZURE_DI_ENDPOINT / AZURE_DI_KEY are not set.")

    analyze = requests.post(
        f"{AZURE_ENDPOINT}/documentintelligence/documentModels/prebuilt-read:analyze"
        f"?api-version={AZURE_API_VERSION}",
        headers={"Ocp-Apim-Subscription-Key": AZURE_KEY,
                 "Content-Type": "application/octet-stream"},
        data=image,
        timeout=REQUEST_TIMEOUT,
    )
    if analyze.status_code not in (200, 202):
        raise OCRError(f"Document Intelligence rejected page {index} "
                       f"({analyze.status_code}: {analyze.text[:200]}).")

    # The Read model is asynchronous: 202 plus a URL to poll until it finishes.
    operation = analyze.headers.get("Operation-Location")
    if not operation:
        raise OCRError(f"Document Intelligence gave no result location for page {index}.")

    for _ in range(AZURE_POLL_ATTEMPTS):
        time.sleep(AZURE_POLL_SECONDS)
        poll = requests.get(operation, headers={"Ocp-Apim-Subscription-Key": AZURE_KEY},
                            timeout=REQUEST_TIMEOUT)
        if poll.status_code != 200:
            raise OCRError(f"Document Intelligence poll failed on page {index} "
                           f"({poll.status_code}).")
        body = poll.json()
        status = body.get("status")
        if status == "succeeded":
            return _azure_page(body, index)
        if status == "failed":
            raise OCRError(f"Document Intelligence could not read page {index}.")

    raise OCRError(f"Document Intelligence timed out on page {index}.")


def _azure_page(body, index):
    result = body.get("analyzeResult") or {}
    pages = result.get("pages") or []
    lines, confidences, doubtful = [], [], []

    for page in pages:
        for line in page.get("lines") or []:
            if line.get("content"):
                lines.append(line["content"])
        for word in page.get("words") or []:
            confidence = word.get("confidence")
            if confidence is None:
                continue
            confidences.append(confidence)
            if confidence < LOW_CONFIDENCE and word.get("content"):
                doubtful.append(word["content"])

    text = "\n".join(lines).strip()
    if not text:
        # A genuinely blank page is possible; an empty read of a page that is
        # not blank is not distinguishable from here, so say so rather than
        # returning an empty string that reads as "the student wrote nothing".
        raise OCRError(f"Document Intelligence found no text on page {index}.")

    mean = sum(confidences) / len(confidences) if confidences else None
    # Mark the doubtful words inline so the transcript carries its own
    # uncertainty, the same way the vision backend's markers do.
    for word in set(doubtful):
        text = text.replace(word, f"[?{word}]")
    return PageText(index, text, confidence=mean, doubtful_words=doubtful)


def render_transcript(pages):
    """One block of text from several pages, with the page breaks kept.

    The markers are the tool's, so the grading prompt is told not to read them
    as the student's writing and not to quote them as evidence.
    """
    blocks = []
    for page in pages:
        blocks.append(f"--- page {page.index} (machine transcription) ---\n{page.text}")
    return "\n\n".join(blocks)


def summarise(pages):
    """Warnings a faculty member should see beside a grade built on this."""
    notes = []
    total_markers = sum(page.marker_count for page in pages)
    doubtful = sum(len(page.doubtful_words) for page in pages)
    scored = [p.confidence for p in pages if p.confidence is not None]

    notes.append(
        f"This submission was read from {len(pages)} page image(s) by machine "
        f"transcription ({BACKEND}). The grade rests on what the reader thought "
        f"the writing said, so check it against the original before approving."
    )
    if total_markers:
        notes.append(
            f"The reader marked {total_markers} word(s) as illegible or uncertain."
        )
    if doubtful:
        notes.append(f"{doubtful} word(s) were read with low confidence.")
    if scored:
        worst = min(scored)
        if worst < LOW_CONFIDENCE:
            notes.append(
                f"The least confident page scored {worst:.0%}; treat that page's "
                f"marks as unsettled."
            )
    return notes
