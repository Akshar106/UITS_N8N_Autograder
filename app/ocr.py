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
import logging
import os
import time

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

REQUEST_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "120"))


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

DESCRIBE WHAT IS NOT TEXT.

- A diagram, drawing, tree, graph or plot cannot be transcribed. Write
  [diagram: a short factual description of what is drawn], describing only what
  is actually there. Do not interpret whether it is correct.

Output the transcription only. No preamble, no commentary, no grading.\
"""


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


def read_pages(images, context=""):
    """Transcribe page images. Returns [PageText]; raises OCRError if it cannot.

    `images` is a list of raw image bytes, in page order. `context` is a short
    hint (the assignment name) that helps a vision model with proper nouns and
    notation; it must never contain an answer.
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
    pages = []
    for index, image in enumerate(images, start=1):
        try:
            pages.append(reader(image, index, context))
        except OCRError:
            raise
        except requests.RequestException as exc:
            raise OCRError(f"Could not reach the image reader on page {index}: {exc}") from exc
    return pages


# --- the ReaLLM gateway, with a vision-capable model --------------------------

VISION_MODEL = os.getenv("OCR_VISION_MODEL") or os.getenv("REALLM_MODEL_NAME")


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
    encoded = base64.b64encode(image).decode("ascii")
    return [
        [{"type": "text", "text": instruction},
         {"type": "image_url", "image_url": {"url": _data_uri(image)}}],
        [{"type": "text", "text": instruction},
         {"type": "image", "source": {"type": "base64",
                                      "media_type": _media_type(image), "data": encoded}}],
    ]


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
            return PageText(index, text)
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
