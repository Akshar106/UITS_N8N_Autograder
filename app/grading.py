import json
import logging
import re

from app.llm_client import call_reallm

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an automated grading assistant designed for STRICT and ACCURATE academic grading.

You will be given:
1. A grading rubric containing questions, expected concepts, and point values
2. A student's answers for the SAME assignment

Your task is to grade the student's answers with maximum accuracy and ZERO hallucination.

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


def build_user_prompt(assignment_name, rubric_text, student_id, student_text):
    return (
        f"Assignment:\n{assignment_name}\n\n"
        f"Rubric:\n{rubric_text}\n\n"
        f"Student ID:\n{student_id}\n\n"
        f"Student Answer:\n{student_text}\n"
    )


def _parse_llm_json(raw_text, expected_criteria=None, expected_total=None):
    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
    data = json.loads(cleaned)
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"LLM response missing fields: {missing}")
    _coerce_numeric_fields(data)
    _validate_breakdown(data, expected_criteria, expected_total)
    _recompute_totals(data)
    return data


# A malformed or self-inconsistent response is usually a one-off. Retrying is
# much better than marking a real student ungradable, but the count is bounded
# so a genuinely broken rubric surfaces as an error instead of looping.
GRADING_ATTEMPTS = 3


def grade_submission(assignment_name, rubric_text, student_id, student_text,
                     expected_criteria=None, expected_total=None):
    """Calls the LLM and returns a dict:
    {ok: bool, data: {...schema...} | None, error: str | None, raw: str | None}

    expected_criteria/expected_total come from the Canvas rubric when there is
    one, and turn "the model quietly skipped a question" from an invisible lost
    grade into a rejected response.
    """
    user_prompt = build_user_prompt(assignment_name, rubric_text, student_id, student_text)

    last_error, last_raw = None, None
    for attempt in range(1, GRADING_ATTEMPTS + 1):
        raw = call_reallm(SYSTEM_PROMPT, user_prompt)
        if raw is None:
            last_error, last_raw = "No response from ReaLLM", None
            logger.warning("Grading attempt %d/%d: no response", attempt, GRADING_ATTEMPTS)
            continue

        try:
            data = _parse_llm_json(raw, expected_criteria, expected_total)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error, last_raw = f"Could not parse grading response: {exc}", raw
            logger.warning("Grading attempt %d/%d rejected: %s", attempt, GRADING_ATTEMPTS, exc)
            continue

        return {"ok": True, "data": data, "error": None, "raw": raw}

    return {"ok": False, "data": None, "error": last_error, "raw": last_raw}
