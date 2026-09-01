#!/usr/bin/env python3
"""Checks the guards that stand between a model's output and a student's grade.

No network and no API key needed -- the model is stubbed, so this runs anywhere:

    python3 scripts/test_grading_guards.py

Covers the arithmetic the app no longer takes on trust, the rubric-coverage
check that catches a silently skipped question, the retry behaviour on both
malformed responses and transient API failures, and the request settings that
make a re-run reproducible.
"""
import json
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FAILS = []


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILS.append(label)


def response(items, stated_score=None, stated_total=None):
    """A model response with the given per-question awards."""
    breakdown = [
        {"question": f"Q{i}", "max_points": mx, "awarded_points": aw, "comment": "c"}
        for i, (aw, mx) in enumerate(items, start=1)
    ]
    return json.dumps({
        "Assignment": "DSA",
        "student_id": "1",
        "total_points": stated_total if stated_total is not None else sum(m for _, m in items),
        "score_awarded": stated_score if stated_score is not None else sum(a for a, _ in items),
        "per_question_breakdown": breakdown,
        "overall_feedback": "feedback",
    })


FIVE_CRITERIA = [{"description": f"Q{i}", "points": 10.0} for i in range(1, 6)]
GOOD = [(10.0, 10.0), (7.0, 10.0), (4.0, 10.0), (0.0, 10.0), (10.0, 10.0)]


def grade(raw_responses, **kwargs):
    """Run grade_submission against a scripted sequence of model replies."""
    from app import grading

    replies = list(raw_responses)
    with mock.patch.object(grading, "call_reallm", side_effect=lambda *a: replies.pop(0)):
        return grading.grade_submission("DSA", "rubric", "1", "answer", **kwargs)


def test_arithmetic():
    print("\n-- the app does the arithmetic, not the model --")

    result = grade([response(GOOD, stated_score=99.0, stated_total=12.0)])
    check("a wrong stated score is replaced by the breakdown sum",
          result["ok"] and result["data"]["score_awarded"] == 31.0)
    check("a wrong stated total is replaced by the breakdown sum",
          result["data"]["total_points"] == 50.0)

    result = grade([response(GOOD)])
    check("a correct response is left alone",
          result["ok"] and result["data"]["score_awarded"] == 31.0)

    # Quoted numbers are still coerced, as before.
    raw = json.loads(response(GOOD))
    raw["score_awarded"] = "31"
    raw["per_question_breakdown"][0]["awarded_points"] = "10"
    check("stringified numbers are still coerced",
          grade([json.dumps(raw)])["ok"])

    check("a fenced ```json block is still parsed",
          grade(["```json\n" + response(GOOD) + "\n```"])["ok"])


def test_impossible_scores():
    print("\n-- impossible scores are rejected, not stored --")

    over = [(12.0, 10.0)] + GOOD[1:]
    result = grade([response(over)] * 3)
    check("awarding more than the maximum is rejected", not result["ok"])
    check("the error names the offending question", "awarded 12.0 out of a maximum of 10.0" in result["error"])

    negative = [(-5.0, 10.0)] + GOOD[1:]
    check("negative points are rejected", not grade([response(negative)] * 3)["ok"])

    empty = json.dumps({
        "Assignment": "DSA", "student_id": "1", "total_points": 50.0, "score_awarded": 0.0,
        "per_question_breakdown": [], "overall_feedback": "f",
    })
    check("an empty breakdown is rejected", not grade([empty] * 3)["ok"])


def test_rubric_coverage():
    print("\n-- a skipped question is caught instead of quietly costing points --")

    four_of_five = GOOD[:4]
    result = grade([response(four_of_five)] * 3, expected_criteria=FIVE_CRITERIA)
    check("dropping a rubric question is rejected", not result["ok"])
    check("the error says how many were expected and graded",
          "5 question(s)" in result["error"] and "graded 4" in result["error"])

    check("grading all five is accepted",
          grade([response(GOOD)], expected_criteria=FIVE_CRITERIA)["ok"])

    wrong_max = [(10.0, 20.0)] + GOOD[1:]
    result = grade([response(wrong_max)] * 3, expected_criteria=FIVE_CRITERIA)
    check("grading a question out of the wrong maximum is rejected", not result["ok"])
    check("the error names the criterion", "rubric allows 10.0 points" in result["error"])

    result = grade([response(GOOD)] * 3, expected_total=100.0)
    check("a total that disagrees with the rubric is rejected", not result["ok"])
    check("the error names both totals",
          "rubric totals 100.0" in result["error"] and "graded out of 50.0" in result["error"])

    # Without a Canvas rubric there is nothing to check against, and grading
    # must still work from an uploaded file alone.
    check("no expected criteria means no coverage check", grade([response(GOOD[:3])])["ok"])


def test_retries():
    print("\n-- one bad response does not fail a real student --")

    result = grade(["not json at all", response(GOOD)])
    check("a malformed response is retried and then succeeds", result["ok"])

    result = grade([response([(12.0, 10.0)]), response(GOOD)])
    check("an invalid response is retried and then succeeds", result["ok"])

    result = grade(["nope", "still nope", "nope again"])
    check("three bad responses give up with an error", not result["ok"] and result["error"])
    check("the raw text is kept for diagnosis", result["raw"] == "nope again")

    result = grade([None, None, None])
    check("no response at all is reported", not result["ok"] and "No response" in result["error"])

    from app import grading
    calls = []

    def counting(*_args):
        calls.append(1)
        return response(GOOD)

    with mock.patch.object(grading, "call_reallm", side_effect=counting):
        grading.grade_submission("DSA", "r", "1", "a")
    check("a good response is not retried", len(calls) == 1)


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ok_payload(content="hello"):
    return {"choices": [{"message": {"content": content}}]}


def test_llm_client():
    print("\n-- the API client survives a flaky gateway --")
    import requests

    from app import llm_client

    def run(responses):
        seq = list(responses)

        def post(*_a, **kwargs):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            post.payload = kwargs.get("json")
            return item

        with mock.patch.object(llm_client.requests, "post", side_effect=post), \
             mock.patch.object(llm_client.time, "sleep"):
            return llm_client.call_reallm("sys", "user"), post

    result, _ = run([FakeResponse(429), FakeResponse(200, ok_payload())])
    check("a 429 is retried and then succeeds", result == "hello")

    result, _ = run([FakeResponse(503), FakeResponse(502), FakeResponse(200, ok_payload())])
    check("repeated 5xx are retried up to the limit", result == "hello")

    result, _ = run([requests.RequestException("boom"), FakeResponse(200, ok_payload())])
    check("a network error is retried and then succeeds", result == "hello")

    result, _ = run([FakeResponse(500), FakeResponse(500), FakeResponse(500)])
    check("persistent failure gives up and returns None", result is None)

    seq = [FakeResponse(400, text="bad request")]
    calls = []

    def post(*_a, **_k):
        calls.append(1)
        return seq.pop(0)

    with mock.patch.object(llm_client.requests, "post", side_effect=post), \
         mock.patch.object(llm_client.time, "sleep"):
        result = llm_client.call_reallm("sys", "user")
    check("a non-retryable 400 fails immediately", result is None and len(calls) == 1)

    result, post = run([FakeResponse(200, {"choices": [{"message": {
        "content": "", "reasoning_content": "fallback"}}]})])
    check("reasoning_content is still used when content is empty", result == "fallback")

    print("\n-- the same submission grades the same way twice --")
    _, post = run([FakeResponse(200, ok_payload())])
    check("temperature is zero", post.payload["temperature"] == 0)
    check("no seed is sent unless configured", "seed" not in post.payload)

    with mock.patch.object(llm_client, "SEED", "42"):
        _, post = run([FakeResponse(200, ok_payload())])
    check("a configured seed is sent", post.payload.get("seed") == 42)


def test_prompt_guards():
    print("\n-- the prompt states the fairness and coverage rules --")
    from app import grading

    prompt = grading.SYSTEM_PROMPT
    check("the prompt tells the model to ignore grammar and spelling",
          "IGNORE grammar, spelling" in prompt)
    check("the prompt protects non-native English writers",
          "second language" in prompt)
    check("the prompt forbids rewarding length",
          "SAME score" in prompt and "padded" in prompt)
    check("the prompt demands one entry per rubric question",
          "EXACTLY ONE breakdown entry" in prompt)
    check("the prompt says unanswered questions still get an entry",
          "not attempted" in prompt)


def main():
    test_arithmetic()
    test_impossible_scores()
    test_rubric_coverage()
    test_retries()
    test_llm_client()
    test_prompt_guards()

    print()
    if FAILS:
        print(f"{len(FAILS)} failure(s):")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
