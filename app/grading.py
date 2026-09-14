import json
import logging
import re
import unicodedata

from app.llm_client import call_reallm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an automated grading assistant designed for STRICT and ACCURATE academic grading.

You will be given:
1. A grading rubric containing questions, expected concepts, and point values
2. Optionally, instructor-graded examples of other submissions for this same assignment
3. A student's answers for the SAME assignment

Your task is to grade the student's answers with maximum accuracy and ZERO hallucination.

────────────────────────────
WHICH PART OF THE RUBRIC WINS
────────────────────────────

The rubric may arrive in two labelled sections:

- "SCORING RUBRIC" comes from the rubric attached to the assignment. It is
  AUTHORITATIVE for the list of questions, the maximum points on each, and the
  overall total. Grade exactly the questions it lists, out of exactly its points.
- "GRADING NOTES / ANSWER KEY" is supporting material from the instructor. Use it
  for subject-matter detail -- the full question wording, what a correct answer
  contains, worked reasoning.

If the two sections disagree about how many questions there are or how many
points a question is worth, the SCORING RUBRIC section wins. If only one section
is present, use it for both purposes.

────────────────────────────
GRADING RULES (MANDATORY)
────────────────────────────

1. Grade ONLY using the provided rubric.
2. Do NOT assume, infer, or invent any rubric content.
3. Do NOT reuse or mix questions from other assignments.
4. If a student answers a DIFFERENT question than the one in the rubric, award 0 points for that question.
5. The total score MUST be calculated by summing the rubric point values — NEVER assume a fixed total.
6. Partial credit IS allowed and REQUIRED when appropriate.

────────────────────────────
WHAT THE RUBRIC DECIDES, AND WHAT YOU DECIDE
────────────────────────────

The rubric is absolute about WHAT IS REQUIRED and WHAT IT IS WORTH. You may
never add a requirement it does not state, remove one it does, or change how
many points a question carries.

Judging whether a particular answer MEETS a stated requirement is your job, and
you should use your knowledge of the subject to do it. In particular:

- The rubric shows ONE way of expressing a correct answer, not the only way.
  Award the credit when the student expresses the same idea in different words.
- Accept equivalent notation, terminology and symbols.
- When the rubric says "such as" or "for example", any other correct example of
  the same kind earns the same credit.
- A correct answer that reaches the required conclusion by a different valid
  route earns full credit for that requirement.

Example of the distinction: if a rubric requires "worst-case O(n), for example
when already-sorted input produces a degenerate tree", then a student who writes
"worst case is linear once the tree collapses into a chain" has met that
requirement in full -- same concept, different words. But if the rubric never
mentions balancing, you may NOT deduct points for failing to discuss AVL trees,
however relevant you think it is.

In short: the rubric sets the bar; your subject knowledge decides whether this
answer clears it. Every award still has to be supported by a quote from the
student, so recognising a differently-worded correct answer is not licence to be
generous about one that is not there.

────────────────────────────
PARTIAL CREDIT POLICY
────────────────────────────

For each question:
- FULL points: Answer is conceptually correct AND sufficiently complete.
- PARTIAL points: Answer contains correct core idea(s) but is:
  - incomplete, OR
  - imprecise, OR
  - missing important details, OR
  - partially incorrect.
- ZERO points: Answer is incorrect, irrelevant, or answers a different question.

When assigning partial credit:
- Clearly explain WHICH parts are correct
- Clearly explain WHICH parts are missing or incorrect
- Award points proportionally (not just 0 or full)

────────────────────────────
STRICT GRADING PROCEDURE (FOLLOW EXACTLY)
────────────────────────────

For EACH question in the rubric:
1. Identify the question text and maximum points.
2. Identify the key concepts required by the rubric.
3. Compare the student's answer ONLY to those required concepts.
4. Determine correctness level:
   - Fully correct
   - Partially correct
   - Incorrect
5. Assign points accordingly.
6. Provide a concise, factual justification.

Do NOT reward:
- Vague statements
- Buzzwords without explanation
- Answers that merely restate the question
- Length. A short precise answer and a long padded answer that convey the same
  correct content receive the SAME score.

────────────────────────────
GRADE CONTENT, NOT WRITING
────────────────────────────

Unless the rubric explicitly assesses writing quality:
- IGNORE grammar, spelling, punctuation, capitalisation and sentence structure.
- IGNORE fluency and style. Awkward or non-native English that conveys the
  correct concept earns FULL credit for that concept.
- Judge ONLY whether the required concepts from the rubric are present and correct.

A student writing in a second language must not lose points for how the answer
reads. Grade what they know, not how well they write English.

────────────────────────────
COVERAGE (MANDATORY)
────────────────────────────

- Output EXACTLY ONE breakdown entry for EACH question in the rubric.
- Keep them in the SAME ORDER as the rubric.
- Never merge two rubric questions into one entry, and never invent an entry
  for a question the rubric does not contain.
- If the student did not answer a rubric question at all, still output that
  entry with awarded_points 0 and say the question was not attempted.

────────────────────────────
CITE THE STUDENT'S OWN WORDS (MANDATORY)
────────────────────────────

For every question, quote the part of the STUDENT'S ANSWER your score is based on,
in an "evidence" field:

- Copy the words VERBATIM from the student's answer. Do not paraphrase, correct,
  or tidy them. To shorten a long passage, join fragments with " ... ".
- Quote the span that earns the points. If the answer earns nothing, quote the
  part that attempts the question, or use "" if the question was not attempted.
- NEVER quote text that is not in the student's answer. If you cannot find
  supporting words in the answer, you cannot award the points.
- In a "missing" field, state what the rubric required that the answer does not
  contain. Use "" when nothing is missing.

The evidence is checked against the student's answer, so an invented quote will
be detected.

────────────────────────────
REPORT YOUR CONFIDENCE
────────────────────────────

Give each question a "confidence" of exactly "high", "medium" or "low":

- "high"   -- the rubric requirement is either clearly present or clearly absent.
- "medium" -- the answer is partially or ambiguously on target and the split of
              partial credit is a judgment call.
- "low"    -- you are genuinely unsure: the answer is unclear, self-contradictory,
              uses unfamiliar notation, or could be read as correct or incorrect.

Be honest. "low" is not a failure -- it routes the answer to a human, which is
the correct outcome for a genuinely ambiguous response. Do not mark something
"high" to look decisive.

────────────────────────────
OUTPUT FORMAT (STRICT)
────────────────────────────

Return ONLY valid JSON in the following format:

{
  "Assignment": string,
  "student_id": string,
  "total_points": number,
  "score_awarded": number,
  "per_question_breakdown": [
    {
      "question": string,
      "max_points": number,
      "awarded_points": number,
      "evidence": string,
      "missing": string,
      "confidence": "high" | "medium" | "low",
      "comment": string
    }
  ],
  "overall_feedback": string
}

IMPORTANT:
- Do NOT include markdown
- Do NOT include explanations outside JSON
- Ensure awarded_points <= max_points
- Ensure score_awarded = sum of awarded_points
"""

REQUIRED_FIELDS = ("Assignment", "student_id", "total_points", "score_awarded", "per_question_breakdown", "overall_feedback")
NUMERIC_FIELDS = ("total_points", "score_awarded")
NUMERIC_BREAKDOWN_FIELDS = ("max_points", "awarded_points")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _coerce_numeric_fields(data):
    # The prompt asks for JSON numbers, but LLMs sometimes quote them as
    # strings anyway ("8" instead of 8) -- coerce here so downstream storage
    # and the Excel export's score/percentage math never receive a string.
    for field in NUMERIC_FIELDS:
        try:
            data[field] = float(data[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Field '{field}' is not numeric: {data[field]!r}") from exc

    breakdown = data.get("per_question_breakdown")
    if not isinstance(breakdown, list):
        raise ValueError("'per_question_breakdown' is not a list")
    for item in breakdown:
        if not isinstance(item, dict):
            raise ValueError(f"per_question_breakdown item is not an object: {item!r}")
        for field in NUMERIC_BREAKDOWN_FIELDS:
            try:
                item[field] = float(item[field])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"per_question_breakdown field '{field}' is not numeric: {item.get(field)!r}") from exc


# Points are compared with a small tolerance: rubric values are floats and a
# model may return 9.999999 for 10.
POINT_TOLERANCE = 0.01

CONFIDENCE_LEVELS = ("low", "medium", "high")
DEFAULT_CONFIDENCE = "medium"

_WHITESPACE_RE = re.compile(r"\s+")
# The prompt asks for " ... " between fragments of a shortened quote; accept the
# unicode ellipsis and bare dots too, since models produce all three.
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.+|…)\s*")
# Everything that is not a letter, digit or space. Punctuation is dropped from
# both sides before comparing: a model quoting "node's" as "node's", or leaving
# out a pair of parentheses, is still quoting the student. Requiring the
# punctuation to match byte-for-byte produced false "fabricated quote" warnings
# on quotes that were genuinely present, which is worse than useless -- it
# trains a faculty member to ignore the flag that catches real fabrication.
_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
# Parenthetical asides, which models skip over when quoting.
_PARENTHETICAL_RE = re.compile(r"\([^()]{0,200}\)")


def _normalise(text):
    """Fold away everything that can differ without the words differing."""
    # NFKC first so typographic characters decompose to their plain forms.
    text = unicodedata.normalize("NFKC", text or "")
    text = _PUNCTUATION_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _haystacks(student_text):
    """The forms of the submission a quote may legitimately match against.

    The second drops parenthetical asides. Models routinely quote around them --
    "linear probing (check next slot), quadratic probing" gets quoted as "linear
    probing, quadratic probing" -- which is still the student's words with an
    aside skipped, not an invention.
    """
    full = _normalise(student_text)
    without_asides = _normalise(_PARENTHETICAL_RE.sub(" ", student_text or ""))
    return [h for h in dict.fromkeys([full, without_asides]) if h]


def _quote_is_in(quote, student_text):
    """Is this quote actually present in what the student wrote?

    A model that awards points for something the student never said will
    usually still produce a confident-sounding quote. Checking the quote against
    the source turns that from an invisible fabrication into a visible flag, and
    it is the only automatic grounding check available -- everything else about
    the judgment is a matter of opinion.

    Each fragment of an elided quote has to appear somewhere in the submission,
    but NOT in the order the quote lists them. Models group the fragments the
    rubric's way -- definition, then average case, then worst case -- while the
    student may have written them in any order, and rejecting that was flagging
    quotes whose every word was genuinely the student's. Fabrication is still
    caught, because invented words appear nowhere at all; what is given up is
    detecting real fragments stitched into a misleading order, which is a rarer
    failure than the false alarms this was producing.

    Returns (verified, missing_fragment). Comparison ignores case, whitespace and
    punctuation, so only the sequence of words within a fragment has to match.
    """
    haystacks = _haystacks(student_text)
    if not haystacks:
        return False, _normalise(quote) or None

    # Split on the ellipsis first: normalising would strip the dots and fuse the
    # fragments into one string that appears nowhere.
    for fragment in _ELLIPSIS_RE.split(quote or ""):
        fragment = _normalise(fragment)
        if not fragment:
            continue
        if not any(fragment in haystack for haystack in haystacks):
            return False, fragment
    return True, None


def _annotate_evidence_and_confidence(data, student_text):
    """Fill in and check the evidence/confidence fields.

    Deliberately forgiving: a model that omits these does not fail the whole
    submission, because a missing quote is a quality signal to show a faculty
    member, not a reason to leave a real student ungraded. What the response
    cannot do is claim support that is not there without it being noticed.
    """
    weakest = "high"
    for item in data["per_question_breakdown"]:
        evidence = item.get("evidence")
        item["evidence"] = evidence.strip() if isinstance(evidence, str) else ""
        missing = item.get("missing")
        item["missing"] = missing.strip() if isinstance(missing, str) else ""

        confidence = item.get("confidence")
        confidence = confidence.strip().lower() if isinstance(confidence, str) else ""
        item["confidence"] = confidence if confidence in CONFIDENCE_LEVELS else DEFAULT_CONFIDENCE

        if item["evidence"]:
            verified, missing_fragment = _quote_is_in(item["evidence"], student_text)
            item["evidence_verified"] = verified
            if not verified:
                # The failing fragment is the useful half: with a long elided
                # quote, knowing which piece is absent is the difference between
                # a two-minute diagnosis and an afternoon of guessing.
                logger.warning(
                    "Evidence quote for %r is not present in the submission.\n"
                    "  quote    : %s\n  not found: %s",
                    item.get("question"), item["evidence"], missing_fragment,
                )
        else:
            # No quote offered. Expected when a question was not attempted;
            # worth a faculty glance when points were awarded anyway.
            item["evidence_verified"] = None

        if CONFIDENCE_LEVELS.index(item["confidence"]) < CONFIDENCE_LEVELS.index(weakest):
            weakest = item["confidence"]

    # An unverifiable quote is a reason for a human to look, whatever the model
    # said about its own confidence.
    if any(item["evidence_verified"] is False for item in data["per_question_breakdown"]):
        weakest = "low"

    data["confidence"] = weakest


def _recompute_totals(data):
    """Derive the score from the breakdown instead of trusting the model's sum.

    The prompt asks for score_awarded = sum(awarded_points), but nothing made
    that true: LLMs are unreliable at arithmetic, and a stated total that does
    not match its own breakdown is one of the easiest ways for a wrong grade to
    look completely reasonable. The per-question awards are the judgments; the
    totals are arithmetic, so we do the arithmetic.
    """
    breakdown = data["per_question_breakdown"]
    computed_awarded = sum(item["awarded_points"] for item in breakdown)
    computed_total = sum(item["max_points"] for item in breakdown)

    for field, computed in (("score_awarded", computed_awarded), ("total_points", computed_total)):
        stated = data[field]
        if abs(stated - computed) > POINT_TOLERANCE:
            logger.warning(
                "Model reported %s=%s but its breakdown sums to %s; using the breakdown.",
                field, stated, computed,
            )
        data[field] = computed


def _validate_breakdown(data, expected_criteria=None, expected_total=None):
    """Reject a response whose numbers cannot be right.

    Raises ValueError, which the caller turns into a retry and then a visible
    ai_error -- never a silently plausible grade.
    """
    breakdown = data["per_question_breakdown"]
    if not breakdown:
        raise ValueError("grading response contained no per-question breakdown")

    for index, item in enumerate(breakdown, start=1):
        awarded, maximum = item["awarded_points"], item["max_points"]
        if maximum < 0 or awarded < 0:
            raise ValueError(f"question {index}: negative points ({awarded}/{maximum})")
        if awarded > maximum + POINT_TOLERANCE:
            raise ValueError(f"question {index}: awarded {awarded} out of a maximum of {maximum}")

    # With a Canvas rubric attached we know exactly which criteria must appear.
    # Without this check, a response that silently drops a question produces a
    # total that looks fine on its own but costs the student those points.
    if expected_criteria:
        if len(breakdown) != len(expected_criteria):
            raise ValueError(
                f"rubric has {len(expected_criteria)} question(s) but the response graded "
                f"{len(breakdown)}"
            )
        for index, (item, criterion) in enumerate(zip(breakdown, expected_criteria), start=1):
            expected_points = criterion.get("points")
            if expected_points is None:
                continue
            if abs(item["max_points"] - expected_points) > POINT_TOLERANCE:
                raise ValueError(
                    f"question {index} ({criterion.get('description', '?')}): rubric allows "
                    f"{expected_points} points but the response used {item['max_points']}"
                )

    if expected_total is not None and abs(data["total_points"] - expected_total) > POINT_TOLERANCE:
        raise ValueError(
            f"rubric totals {expected_total} points but the response graded out of "
            f"{data['total_points']}"
        )


# Long enough to show the standard, short enough that three of them do not crowd
# out the rubric and the answer being graded.
ANCHOR_TEXT_LIMIT = 1500
MAX_ANCHORS = 3


def select_anchor_examples(reviewed, limit=MAX_ANCHORS):
    """Pick a spread of instructor-graded submissions to calibrate against.

    Lowest, highest and middle rather than the first few: the point is to show
    the model the whole range of this instructor's standard, and three
    submissions that all scored well teach it nothing about where marks come off.
    """
    usable = sorted(
        (r for r in reviewed if r.get("score") is not None and (r.get("text") or "").strip()),
        key=lambda r: r["score"],
    )
    if len(usable) <= limit:
        return usable

    # Evenly spaced positions across the sorted range, endpoints included.
    step = (len(usable) - 1) / (limit - 1) if limit > 1 else 0
    return [usable[round(i * step)] for i in range(limit)]


def render_anchor_examples(anchors, total_points=None):
    if not anchors:
        return ""

    out_of = f" / {_format_points(total_points)}" if total_points is not None else ""
    lines = [
        "INSTRUCTOR-GRADED EXAMPLES",
        "",
        "The instructor has already graded these submissions for this same assignment.",
        "They show the standard of strictness to apply. Calibrate to them: an answer of",
        "similar quality should receive a similar score. Do NOT copy their scores, and do",
        "NOT grade these submissions -- grade only the student answer at the end.",
        "",
    ]
    for index, anchor in enumerate(anchors, start=1):
        text = (anchor.get("text") or "").strip()
        if len(text) > ANCHOR_TEXT_LIMIT:
            text = text[:ANCHOR_TEXT_LIMIT].rstrip() + " [...truncated]"
        lines.append(
            f"--- Example {index}: the instructor awarded "
            f"{_format_points(anchor['score'])}{out_of} ---"
        )
        lines.append("Submission:")
        lines.append(text)
        if (anchor.get("feedback") or "").strip():
            lines.append(f"Instructor's comments: {anchor['feedback'].strip()}")
        lines.append("")
    return "\n".join(lines).strip()


def _format_points(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def build_user_prompt(assignment_name, rubric_text, student_id, student_text,
                      anchors=None, total_points=None):
    anchor_block = render_anchor_examples(anchors, total_points) if anchors else ""
    parts = [f"Assignment:\n{assignment_name}", f"Rubric:\n{rubric_text}"]
    if anchor_block:
        parts.append(anchor_block)
    parts.append(f"Student ID:\n{student_id}")
    # The answer to grade goes last, immediately before the model responds, so it
    # cannot be confused with the calibration examples above it.
    parts.append(f"Student Answer:\n{student_text}\n")
    return "\n\n".join(parts)


def _parse_llm_json(raw_text, student_text="", expected_criteria=None, expected_total=None):
    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
    data = json.loads(cleaned)
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"LLM response missing fields: {missing}")
    _coerce_numeric_fields(data)
    _validate_breakdown(data, expected_criteria, expected_total)
    _recompute_totals(data)
    _annotate_evidence_and_confidence(data, student_text)
    return data


# A malformed or self-inconsistent response is usually a one-off. Retrying is
# much better than marking a real student ungradable, but the count is bounded
# so a genuinely broken rubric surfaces as an error instead of looping.
GRADING_ATTEMPTS = 3


def grade_submission(assignment_name, rubric_text, student_id, student_text,
                     expected_criteria=None, expected_total=None, anchors=None):
    """Calls the LLM and returns a dict:
    {ok: bool, data: {...schema...} | None, error: str | None, raw: str | None}

    expected_criteria/expected_total come from the Canvas rubric when there is
    one, and turn "the model quietly skipped a question" from an invisible lost
    grade into a rejected response.

    anchors are submissions the instructor has already graded for this same
    assignment, used to calibrate to their standard.
    """
    user_prompt = build_user_prompt(
        assignment_name, rubric_text, student_id, student_text,
        anchors=anchors, total_points=expected_total,
    )

    last_error, last_raw = None, None
    for attempt in range(1, GRADING_ATTEMPTS + 1):
        raw = call_reallm(SYSTEM_PROMPT, user_prompt)
        if raw is None:
            last_error, last_raw = "No response from ReaLLM", None
            logger.warning("Grading attempt %d/%d: no response", attempt, GRADING_ATTEMPTS)
            continue

        try:
            data = _parse_llm_json(raw, student_text, expected_criteria, expected_total)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error, last_raw = f"Could not parse grading response: {exc}", raw
            logger.warning("Grading attempt %d/%d rejected: %s", attempt, GRADING_ATTEMPTS, exc)
            continue

        return {"ok": True, "data": data, "error": None, "raw": raw}

    return {"ok": False, "data": None, "error": last_error, "raw": last_raw}
