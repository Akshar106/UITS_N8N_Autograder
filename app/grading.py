import json
import re

from app.llm_client import call_reallm

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

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def build_user_prompt(assignment_name, rubric_text, student_id, student_text):
    return (
        f"Assignment:\n{assignment_name}\n\n"
        f"Rubric:\n{rubric_text}\n\n"
        f"Student ID:\n{student_id}\n\n"
        f"Student Answer:\n{student_text}\n"
    )


def _parse_llm_json(raw_text):
    cleaned = _CODE_FENCE_RE.sub("", raw_text.strip()).strip()
    data = json.loads(cleaned)
    missing = [f for f in REQUIRED_FIELDS if f not in data]
    if missing:
        raise ValueError(f"LLM response missing fields: {missing}")
    return data


def grade_submission(assignment_name, rubric_text, student_id, student_text):
    """Calls the LLM and returns a dict:
    {ok: bool, data: {...schema...} | None, error: str | None, raw: str | None}
    """
    user_prompt = build_user_prompt(assignment_name, rubric_text, student_id, student_text)
    raw = call_reallm(SYSTEM_PROMPT, user_prompt)

    if raw is None:
        return {"ok": False, "data": None, "error": "No response from ReaLLM", "raw": None}

    try:
        data = _parse_llm_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        return {"ok": False, "data": None, "error": f"Could not parse grading response: {exc}", "raw": raw}

    return {"ok": True, "data": data, "error": None, "raw": raw}
