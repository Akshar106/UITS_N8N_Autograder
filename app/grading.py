import copy
import json
import logging
import os
import re
import unicodedata
from collections import Counter

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

1. The rubric governs wherever it speaks. Where it is silent, grade the answer
   against the QUESTION using your own knowledge of the subject.
2. Never invent a requirement the rubric does not state and then deduct for it.
3. Do NOT reuse or mix questions from other assignments.
4. If a student answers a DIFFERENT question than the one in the rubric, award 0 points for that question.
5. The total score MUST be calculated by summing the rubric point values — NEVER assume a fixed total.
6. Partial credit IS allowed and REQUIRED when appropriate.

────────────────────────────
WHAT THE RUBRIC DECIDES, AND WHAT YOU DECIDE
────────────────────────────

The rubric is absolute about WHAT IS WORTH HOW MUCH. Never change the number of
questions or the marks on any of them.

Beyond that, how much detail the rubric gives varies, and your job changes with it.

CASE 1 -- the rubric states what earns the marks.
Judge whether this answer meets that stated requirement, using your knowledge of
the subject to recognise it however it is expressed:

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

CASE 2 -- the rubric names a criterion but not the standard.
"Write a function that reverses a linked list (10 points)" tells you what is
being asked and what it is worth, and nothing about what correct looks like.
Supply that yourself. Correctness is ALWAYS implicitly required: an answer that
does not work has not met the requirement, whether or not the rubric says so.

CASE 3 -- the rubric says nothing about a question, or there is no rubric.
Grade against the question as asked, on subject-matter correctness, and award
the marks the question carries.

THE ONE ASYMMETRY THAT MATTERS

Use your knowledge freely to AWARD credit -- to recognise a correct answer the
rubric did not anticipate, an equivalent method, a valid alternative example.

Be far more careful using it to DEDUCT. You may deduct for something that is
actually WRONG: a false statement, code that does not work, an analysis that
contradicts itself. You may NOT deduct because an answer omits something you
would have included, is less thorough than you would like, uses a library or
style you would not have chosen, or could have been optimised further. If the
rubric did not ask for it and its absence does not make the answer incorrect, it
costs nothing.

The reason is fairness: a grade a student cannot predict from the assignment and
the rubric is a grade nobody can defend.

────────────────────────────
GRADING CODE
────────────────────────────

When an answer contains code, judge whether the code WORKS, by reading it:

- Trace the logic. Would it produce the required behaviour on ordinary input,
  and on the obvious edge cases (empty input, a single element, a zero divisor)?
- Faults that stop it working cost marks: wrong operator or direction, an
  off-by-one, an undefined name, a variable used before assignment, a call that
  does not do what the author thinks, a method applied to the wrong type.
- Any approach that works earns full credit. Do not require a specific library,
  function or idiom unless the rubric names one; do not reward the approach you
  would have picked over an equally correct one.
- Ignore naming, formatting, comments and language style entirely.
- Ignore efficiency unless the question or rubric asks about it -- a correct
  O(n^2) answer to a question that never mentioned complexity is correct.
- Judge the code the student wrote against the task, not against the prose next
  to it. Code that contradicts its own explanation is a fault in one of them:
  say which, and grade each criterion on its own evidence.

────────────────────────────
OPEN-ENDED AND ANALYTICAL ANSWERS
────────────────────────────

Some questions have several defensible answers. Where the rubric does not name
one, grade the REASONING, not the conclusion:

- A well-argued position you disagree with earns full credit.
- An unsupported assertion earns little, even when you happen to agree with it.
- Credit reasoning that engages with the specifics of the question or dataset;
  a generic statement that would fit any assignment shows nothing.
- Factual errors cost marks whatever conclusion they support.

────────────────────────────
SAY WHAT YOU GRADED AGAINST
────────────────────────────

Give each question a "basis" of exactly:

  "rubric"    -- the rubric stated what earns the marks and you applied it
  "knowledge" -- the rubric was silent on the standard, so you judged the answer
                 on subject-matter correctness yourself
  "both"      -- the rubric set part of the standard and you supplied the rest

Be accurate. A faculty member reviewing a "knowledge" judgment is checking your
subject expertise rather than their own rubric, and they need to know which one
they are looking at.

Every award still has to be supported by a quote from the student, whichever
basis you used.

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
- A question's lettered or bulleted sub-parts, such as "(a) 5 pts", are how its
  marks are DIVIDED, not separate questions. Judge each sub-part, then report
  the question as ONE entry whose max_points is the sub-parts added together.
  A rubric with 6 questions split into 20 sub-parts gets 6 entries, never 20.
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
- The field must contain NOTHING BUT the student's words. Do not introduce the
  quote, label it, or describe it. Write `"evidence": "from sklearn.metrics
  import confusion_matrix"`, never `"evidence": "uses the right imports:
  from sklearn.metrics import confusion_matrix"`. Anything you add is read as
  part of the quote and makes it fail the check.
- Quote the span that earns the points. A LOW OR ZERO SCORE STILL NEEDS ITS
  QUOTE: show the wrong or inadequate words you are marking down. That is the
  score a student is most likely to dispute, and "the answer was weak" with
  nothing quoted is not something a faculty member can defend.
- Leave "evidence" empty ONLY when the student wrote nothing at all for that
  question. If they attempted it and got it wrong, quote the attempt.
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
      "basis": "rubric" | "knowledge" | "both",
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

# What the grader judged a question against. Defaults to "rubric" when a model
# omits it, which is the conservative reading: it does not claim the grader went
# beyond the instructor's stated standard when we cannot tell.
BASIS_VALUES = ("rubric", "knowledge", "both")
DEFAULT_BASIS = "rubric"

_WHITESPACE_RE = re.compile(r"\s+")
# The prompt asks for " ... " between fragments of a shortened quote; accept the
# unicode ellipsis and bare dots too, since models produce all three.
_ELLIPSIS_RE = re.compile(r"\s*(?:\.\.\.+|…)\s*")
# A quote from code is inherently several lines, and the lines a model picks are
# usually not adjacent in the source -- it skips the imports and the blank lines
# between them. Each line is therefore treated as its own fragment.
_LINE_BREAK_RE = re.compile(r"[\r\n]+")
# Models sometimes emit a literal backslash-n inside the JSON string rather than
# a real newline. Left alone, the backslash is stripped as punctuation and the
# "n" fuses onto the next word ("1e-6\nout[...]" -> "1e 6 nout").
_ESCAPED_WHITESPACE_RE = re.compile(r"\\[nrt]")
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

    # Split before normalising: normalising strips the dots and the newlines, so
    # the fragments would fuse into one string that appears nowhere.
    text = _ESCAPED_WHITESPACE_RE.sub("\n", quote or "")
    for chunk in _ELLIPSIS_RE.split(text):
        for fragment in _LINE_BREAK_RE.split(chunk):
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

        basis = item.get("basis")
        basis = basis.strip().lower() if isinstance(basis, str) else ""
        item["basis"] = basis if basis in BASIS_VALUES else DEFAULT_BASIS

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
    # Surfaced on the row so faculty can find the judgments made on the grader's
    # own subject knowledge rather than on their rubric -- those are the ones
    # where they are checking someone else's expertise, not their own standard.
    data["knowledge_judgments"] = sum(
        1 for item in data["per_question_breakdown"] if item["basis"] in ("knowledge", "both")
    )


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

    if data["score_awarded"] > data["total_points"] + POINT_TOLERANCE:
        raise ValueError(
            f"awarded {data['score_awarded']} out of a possible {data['total_points']}"
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
    # Recompute BEFORE validating. The whole point of recomputing is that the
    # model's stated totals are not trusted, so checking them against the rubric
    # checks the wrong number: a response claiming 100 while its breakdown summed
    # to 114 passed a 100-point check and then had its total replaced by 114.
    _recompute_totals(data)
    _validate_breakdown(data, expected_criteria, expected_total)
    _annotate_evidence_and_confidence(data, student_text)
    return data


# How many independent grades to take of each submission. Two passes of the same
# model are not independent of its biases -- both will be equally generous on a
# thin rubric -- but they do disagree on genuinely ambiguous answers, and that
# disagreement is a MEASURED uncertainty signal rather than a self-reported one.
# The model's own confidence proved close to useless: it reported "high" on
# submissions it scored thirty points away from.
GRADING_PASSES = max(1, int(os.getenv("GRADING_PASSES", "2")))

# Two passes disagree a little almost every time -- on a 100 point paper they
# typically land within a point or two of each other on most questions. Treating
# every such wobble as uncertainty flags the entire class, which costs a faculty
# member exactly as much attention as flagging nobody. A disagreement earns a
# human's time only when it is large enough to change what the student gets: a
# tenth of the marks for that question, and two percent of the paper.
DISAGREEMENT_OF_QUESTION = float(os.getenv("DISAGREEMENT_OF_QUESTION", "0.10"))
DISAGREEMENT_OF_PAPER = float(os.getenv("DISAGREEMENT_OF_PAPER", "0.02"))


def _disagreement_threshold(question_points, paper_points):
    """The smallest spread between passes worth sending to a human."""
    return max(
        POINT_TOLERANCE,
        DISAGREEMENT_OF_QUESTION * (question_points or 0),
        DISAGREEMENT_OF_PAPER * (paper_points or 0),
    )


def _merge_passes(datas):
    """Combine independent grades of the same submission.

    Where the passes agree, the score is that agreed value. Where they differ,
    the award is their mean and the question is marked low confidence, which
    pushes the row to the top of the review queue -- the disagreement is the
    point, not something to average quietly away.
    """
    merged = copy.deepcopy(datas[0])
    breakdown = merged["per_question_breakdown"]

    sizes = {len(d["per_question_breakdown"]) for d in datas}
    if len(sizes) > 1:
        # The passes did not even agree on how many questions there are, so
        # there is no per-question comparison to make. Keep the first grade and
        # send the whole row to a human.
        logger.warning("Grading passes disagreed on structure: %s questions", sorted(sizes))
        for item in breakdown:
            item["confidence"] = "low"
        merged.update(passes=len(datas), max_disagreement=None,
                      raw_disagreement=None, structural_disagreement=True)
        merged["confidence"] = "low"
        return merged

    paper_points = sum(i["max_points"] for i in breakdown)
    worst = 0.0
    worst_material = 0.0
    for index, item in enumerate(breakdown):
        awards = [d["per_question_breakdown"][index]["awarded_points"] for d in datas]
        spread = max(awards) - min(awards)
        mean = sum(awards) / len(awards)
        worst = max(worst, spread)
        material = spread > _disagreement_threshold(item["max_points"], paper_points)
        if material:
            worst_material = max(worst_material, spread)

        # The commentary comes from whichever pass landed nearest the merged
        # award, so the explanation a faculty member reads matches the number
        # beside it instead of arguing with it.
        nearest = min(datas, key=lambda d: abs(d["per_question_breakdown"][index]["awarded_points"] - mean))
        source = nearest["per_question_breakdown"][index]
        for field in ("comment", "evidence", "missing", "evidence_verified", "basis"):
            item[field] = source.get(field)

        item["awarded_points"] = round(mean, 2)
        # The raw spread is always recorded -- it is what the review page shows
        # beside the question, and a small one is still worth seeing once you are
        # already looking at the row. Only a material spread overrides the
        # confidence, because that is what decides whether anyone looks at all.
        item["disagreement"] = round(spread, 2)
        item["disagreement_material"] = material
        item["confidence"] = (
            "low" if material
            else min((d["per_question_breakdown"][index]["confidence"] for d in datas),
                     key=CONFIDENCE_LEVELS.index)
        )

    merged["score_awarded"] = round(sum(i["awarded_points"] for i in breakdown), 2)
    merged["total_points"] = round(sum(i["max_points"] for i in breakdown), 2)
    nearest_overall = min(datas, key=lambda d: abs(d["score_awarded"] - merged["score_awarded"]))
    merged["overall_feedback"] = nearest_overall.get("overall_feedback", "")

    merged["confidence"] = min((i["confidence"] for i in breakdown), key=CONFIDENCE_LEVELS.index)
    if any(i.get("evidence_verified") is False for i in breakdown):
        merged["confidence"] = "low"
    merged["knowledge_judgments"] = sum(
        1 for i in breakdown if i["basis"] in ("knowledge", "both"))
    # max_disagreement is what gets stored and drives triage, so it carries the
    # worst spread that met the bar; raw_disagreement keeps the unfiltered number
    # for the eval harness, which needs to see the wobble the bar is filtering.
    merged.update(passes=len(datas), max_disagreement=round(worst_material, 2),
                  raw_disagreement=round(worst, 2), structural_disagreement=False)
    return merged


# A malformed or self-inconsistent response is usually a one-off. Retrying is
# much better than marking a real student ungradable, but the count is bounded
# so a genuinely broken rubric surfaces as an error instead of looping.
GRADING_ATTEMPTS = 3


def grade_submission(assignment_name, rubric_text, student_id, student_text,
                     expected_criteria=None, expected_total=None, anchors=None,
                     passes=None):
    """Grade a submission, taking `passes` independent readings of it.

    With passes > 1 the readings are compared question by question: agreement
    becomes the score, disagreement becomes the score's midpoint plus a low
    confidence flag that routes the row to a human. See _merge_passes.
    """
    passes = GRADING_PASSES if passes is None else max(1, passes)

    def read_once():
        return _grade_once(assignment_name, rubric_text, student_id, student_text,
                           expected_criteria, expected_total, anchors)

    results = [read_once() for _ in range(passes)]
    usable = [r for r in results if r["ok"]]
    if not usable:
        return results[-1]
    if len(usable) > 1:
        usable, retried = _agree_on_structure(usable, read_once)
    else:
        retried = False
    if len(usable) == 1:
        data = usable[0]["data"]
        data.update(passes=1, max_disagreement=0.0, raw_disagreement=0.0,
                    structural_disagreement=False, structure_retried=retried)
        return usable[0]
    merged = _merge_passes([r["data"] for r in usable])
    merged["structure_retried"] = retried
    return {"ok": True, "error": None, "raw": usable[0]["raw"], "data": merged}


def _structure_of(result):
    return len(result["data"]["per_question_breakdown"])


def _agree_on_structure(usable, read_once):
    """Resolve passes that split the rubric into different numbers of questions.

    A rubric whose questions carry lettered sub-parts invites one reading to
    grade the 6 questions and another to grade the 20 sub-parts. Neither is
    malformed on its own, but they cannot be compared position by position, and
    picking whichever came back first is a coin toss over the whole grade. One
    extra reading breaks the tie by weight of evidence instead.
    """
    shapes = {_structure_of(r) for r in usable}
    if len(shapes) == 1:
        return usable, False

    logger.warning("Grading passes split the rubric %s ways: %s; taking a tie-breaker",
                   len(shapes), sorted(shapes))
    extra = read_once()
    if extra["ok"]:
        usable = usable + [extra]

    counts = Counter(_structure_of(r) for r in usable)
    best, votes = counts.most_common(1)[0]
    if votes < 2 or list(counts.values()).count(votes) > 1:
        # Still no majority. Leave every reading in and let _merge_passes send
        # the row to a human, which is the honest outcome when the tie-breaker
        # did not break anything.
        return usable, True
    return [r for r in usable if _structure_of(r) == best], True


def _grade_once(assignment_name, rubric_text, student_id, student_text,
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
