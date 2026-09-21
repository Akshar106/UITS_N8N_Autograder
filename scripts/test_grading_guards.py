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


def response(items, stated_score=None, stated_total=None, extras=None):
    """A model response with the given per-question awards.

    extras, when given, is a list of dicts merged into the matching breakdown
    entry -- used to script evidence and confidence values.
    """
    breakdown = [
        {"question": f"Q{i}", "max_points": mx, "awarded_points": aw, "comment": "c"}
        for i, (aw, mx) in enumerate(items, start=1)
    ]
    for item, extra in zip(breakdown, extras or []):
        item.update(extra)
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


def grade(raw_responses, student_text="answer", **kwargs):
    """Run grade_submission against a scripted sequence of model replies."""
    from app import grading

    replies = list(raw_responses)
    with mock.patch.object(grading, "call_reallm", side_effect=lambda *a: replies.pop(0)):
        return grading.grade_submission("DSA", "rubric", "1", student_text, **kwargs)


STUDENT_ANSWER = (
    "Q1: A binary search tree keeps all values in the left subtree smaller than "
    "the node. Average-case search is O(log n) when the tree is balanced.\n\n"
    "Q2: The naive recursive version is O(2^n) because it recomputes subproblems."
)


def test_evidence_verification():
    print("\n-- an award has to be backed by the student's own words --")

    verbatim = grade(
        [response([(10.0, 10.0)], extras=[{"evidence": "Average-case search is O(log n)"}])],
        student_text=STUDENT_ANSWER,
    )
    item = verbatim["data"]["per_question_breakdown"][0]
    check("a verbatim quote is verified", item["evidence_verified"] is True)

    # The prompt asks for " ... " between fragments of a shortened quote.
    elided = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "A binary search tree keeps ... smaller than the node"}])],
        student_text=STUDENT_ANSWER,
    )
    check("an elided quote is verified when every fragment is present in order",
          elided["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    # Order is deliberately not required: models group fragments the rubric's way
    # (definition, average case, worst case) while the student may have written
    # them in any order. Rejecting that flagged quotes whose every word was the
    # student's -- seen twice in a real eval run.
    out_of_order = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "smaller than the node ... A binary search tree keeps"}])],
        student_text=STUDENT_ANSWER,
    )
    check("fragments quoted out of the student's order still verify",
          out_of_order["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    # A parenthetical aside skipped mid-quote is still the student's words.
    aside = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "probing like linear probing, quadratic probing, or double hashing"}])],
        student_text="Q5: probing like linear probing (check the next slot), quadratic "
                     "probing, or double hashing (a second hash sets the step).",
    )
    check("a skipped parenthetical still verifies",
          aside["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    paraphrase = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "The student demonstrated a clear grasp of collision resolution"}])],
        student_text=STUDENT_ANSWER,
    )
    check("a plausible paraphrase is still flagged as not the student's words",
          paraphrase["data"]["per_question_breakdown"][0]["evidence_verified"] is False)

    fabricated = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "The student clearly explained the degenerate tree case"}])],
        student_text=STUDENT_ANSWER,
    )
    fab_item = fabricated["data"]["per_question_breakdown"][0]
    check("an invented quote is flagged", fab_item["evidence_verified"] is False)
    check("an invented quote does not fail the submission", fabricated["ok"])
    check("an invented quote forces the row to low confidence",
          fabricated["data"]["confidence"] == "low")

    # The two false positives seen in a real eval run: a model quoting with a
    # typographic apostrophe, and one that dropped a pair of parentheses. Both
    # were genuinely present in the submission, and flagging them as fabricated
    # is worse than not checking at all -- it teaches faculty to ignore the flag.
    curly = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "A binary search tree keeps all values in the left subtree "
                        "smaller than the node\u2019s position"}])],
        student_text="Q1: A binary search tree keeps all values in the left subtree "
                     "smaller than the node's position.",
    )
    check("a typographic apostrophe still verifies",
          curly["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    parens = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "storing a linked list or similar bucket"}])],
        student_text="Q5: ... storing a linked list (or similar bucket) of all entries.",
    )
    check("dropped parentheses still verify",
          parens["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    half = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "A binary search tree keeps ... and the student proved the bound"}])],
        student_text=STUDENT_ANSWER,
    )
    check("a half-real, half-invented quote is still flagged",
          half["data"]["per_question_breakdown"][0]["evidence_verified"] is False)

    # Code quotes are inherently several lines, and the lines a model picks are
    # rarely adjacent in the source -- it skips imports and blank lines. Eight of
    # these were wrongly flagged in one real run.
    CODE = ("import pandas as pd\n"
            "from sklearn.pipeline import Pipeline\n\n"
            "df = pd.read_csv('churn.csv')\n"
            "prep = ColumnTransformer([('num', StandardScaler(), num)])\n"
            "X_train, X_test = train_test_split(X, y, stratify=y)\n")
    multiline = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "prep = ColumnTransformer([('num', StandardScaler(), num)])\n"
                        "X_train, X_test = train_test_split(X, y, stratify=y)"}])],
        student_text=CODE,
    )
    check("non-adjacent code lines quoted together still verify",
          multiline["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    # Some models write a literal backslash-n inside the JSON string instead of a
    # real newline; the backslash is stripped as punctuation and the "n" fuses
    # onto the next word.
    escaped = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "df = pd.read_csv('churn.csv')\\nprep = ColumnTransformer("}])],
        student_text=CODE,
    )
    check("a literal backslash-n between lines still verifies",
          escaped["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    invented_line = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "df = pd.read_csv('churn.csv')\nmodel.explain_shap_values()"}])],
        student_text=CODE,
    )
    check("an invented line among real ones is still flagged",
          invented_line["data"]["per_question_breakdown"][0]["evidence_verified"] is False)

    messy = grade(
        [response([(10.0, 10.0)], extras=[{
            "evidence": "AVERAGE-CASE   search\nis O(log n)"}])],
        student_text=STUDENT_ANSWER,
    )
    check("case and whitespace differences still verify",
          messy["data"]["per_question_breakdown"][0]["evidence_verified"] is True)

    none_given = grade([response([(0.0, 10.0)])], student_text=STUDENT_ANSWER)
    blank = none_given["data"]["per_question_breakdown"][0]
    check("no quote offered is neither verified nor flagged", blank["evidence_verified"] is None)
    check("a missing evidence field defaults to empty", blank["evidence"] == "")
    check("a missing 'missing' field defaults to empty", blank["missing"] == "")


def test_confidence():
    print("\n-- confidence is normalised and rolls up to the row --")

    result = grade([response(
        [(10.0, 10.0), (5.0, 10.0), (0.0, 10.0)],
        extras=[{"confidence": "high"}, {"confidence": "LOW"}, {"confidence": "medium"}],
    )], student_text=STUDENT_ANSWER)
    levels = [q["confidence"] for q in result["data"]["per_question_breakdown"]]
    check("valid confidence values are kept", levels == ["high", "low", "medium"])
    check("confidence is case-insensitive", levels[1] == "low")
    check("the row takes the weakest question's confidence",
          result["data"]["confidence"] == "low")

    result = grade([response([(10.0, 10.0)], extras=[{"confidence": "extremely sure"}])])
    check("a nonsense confidence value falls back to medium",
          result["data"]["per_question_breakdown"][0]["confidence"] == "medium")

    result = grade([response([(10.0, 10.0)])])
    check("a missing confidence field falls back to medium",
          result["data"]["confidence"] == "medium")

    result = grade([response(
        [(10.0, 10.0), (10.0, 10.0)],
        extras=[{"confidence": "high"}, {"confidence": "high"}],
    )])
    check("all-high questions give a high-confidence row",
          result["data"]["confidence"] == "high")


def test_basis():
    print("\n-- what each judgment was graded against --")

    result = grade([response(
        [(10.0, 10.0), (5.0, 10.0), (8.0, 10.0)],
        extras=[{"basis": "rubric"}, {"basis": "KNOWLEDGE"}, {"basis": "both"}],
    )])
    bases = [q["basis"] for q in result["data"]["per_question_breakdown"]]
    check("valid basis values are kept", bases == ["rubric", "knowledge", "both"])
    check("basis is case-insensitive", bases[1] == "knowledge")
    check("knowledge and both are counted for the row",
          result["data"]["knowledge_judgments"] == 2)

    result = grade([response([(10.0, 10.0)], extras=[{"basis": "vibes"}])])
    check("a nonsense basis falls back to rubric",
          result["data"]["per_question_breakdown"][0]["basis"] == "rubric")

    result = grade([response([(10.0, 10.0)])])
    check("a missing basis defaults to rubric -- the conservative reading",
          result["data"]["per_question_breakdown"][0]["basis"] == "rubric")
    check("a rubric-only row counts no knowledge judgments",
          result["data"]["knowledge_judgments"] == 0)


def test_anchor_examples():
    print("\n-- calibration against the instructor's own grading --")
    from app import grading

    reviewed = [
        {"score": 12.0, "feedback": "weak", "text": "bad answer"},
        {"score": 25.0, "feedback": "", "text": "middling answer"},
        {"score": 31.0, "feedback": "", "text": "ok answer"},
        {"score": 47.0, "feedback": "strong", "text": "great answer"},
    ]
    picked = grading.select_anchor_examples(reviewed, limit=3)
    check("three anchors are chosen", len(picked) == 3)
    check("the anchors span the score range",
          picked[0]["score"] == 12.0 and picked[-1]["score"] == 47.0)
    check("the anchors are ordered by score",
          [a["score"] for a in picked] == sorted(a["score"] for a in picked))

    check("fewer reviewed than the limit returns them all",
          len(grading.select_anchor_examples(reviewed[:2], limit=3)) == 2)
    check("no reviewed submissions gives no anchors",
          grading.select_anchor_examples([], limit=3) == [])
    check("rows with no score are skipped",
          grading.select_anchor_examples([{"score": None, "text": "x"}]) == [])
    check("rows with no text are skipped",
          grading.select_anchor_examples([{"score": 10.0, "text": "   "}]) == [])

    rendered = grading.render_anchor_examples(picked, total_points=50)
    check("the rendered block states the instructor's scores",
          "awarded 12 / 50" in rendered and "awarded 47 / 50" in rendered)
    check("the rendered block carries the graded text", "great answer" in rendered)
    check("the instructor's comments are included", "strong" in rendered)
    check("the block tells the model not to grade the examples",
          "NOT grade these submissions" in rendered)

    long_anchor = [{"score": 10.0, "feedback": "", "text": "x" * (grading.ANCHOR_TEXT_LIMIT + 500)}]
    check("a very long example is truncated",
          "[...truncated]" in grading.render_anchor_examples(long_anchor))
    check("no anchors renders nothing", grading.render_anchor_examples([]) == "")

    prompt = grading.build_user_prompt(
        "DSA", "the rubric", "1", "the answer being graded",
        anchors=picked, total_points=50,
    )
    check("anchors appear in the prompt", "INSTRUCTOR-GRADED EXAMPLES" in prompt)
    check("the answer being graded comes last",
          prompt.rindex("the answer being graded") > prompt.rindex("great answer"))
    check("a prompt without anchors omits the section",
          "INSTRUCTOR-GRADED" not in grading.build_user_prompt("DSA", "r", "1", "a"))


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

    # The total check must run on the RECOMPUTED total, not the one the model
    # stated. A real run produced a response claiming total_points 100 whose
    # breakdown summed to 114; it passed the 100-point check and then had its
    # total replaced by 114, and the submission scored 108 out of a 100-point
    # paper.
    inflated = [(18.0, 19.0)] * 6
    result = grade([response(inflated, stated_total=100.0)] * 3, expected_total=100.0)
    check("a breakdown that oversums the rubric total is rejected", not result["ok"])
    check("the error names the real total, not the claimed one",
          "graded out of 114.0" in result["error"])

    undersized = [(4.0, 5.0)] * 6
    result = grade([response(undersized, stated_total=100.0)] * 3, expected_total=100.0)
    check("a breakdown that undersums the rubric total is rejected too", not result["ok"])

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

    # Normalised: the prompt is hard-wrapped, so a phrase under test can be
    # split across a line break and still be present.
    prompt = grading.SYSTEM_PROMPT
    flat = " ".join(prompt.split())
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
    check("the prompt says which rubric section is authoritative",
          "SCORING RUBRIC section wins" in prompt)
    check("the prompt demands verbatim quotes from the student",
          "VERBATIM" in prompt and "evidence" in prompt)
    check("the prompt forbids quoting text the student did not write",
          "NEVER quote text that is not in the student's answer" in prompt)
    check("the prompt separates what the rubric decides from what the model decides",
          "The rubric is absolute about WHAT IS WORTH HOW MUCH" in flat)
    check("the prompt allows differently-worded correct answers",
          "same idea in different words" in flat)
    check("the prompt accepts other valid examples after 'such as'",
          "such as" in flat and "same kind earns the same credit" in flat)
    check("the prompt forbids changing the marks on a question",
          "Never change the number of questions or the marks on any of them" in flat)
    check("the prompt ties every basis back to the evidence quote",
          "Every award still has to be supported by a quote from the student, whichever basis"
          in flat)
    print("\n-- the prompt covers rubric-silent questions, code and open analysis --")
    check("the prompt says to use own knowledge where the rubric is silent",
          "Where it is silent, grade the answer against the QUESTION using your own knowledge"
          in flat)
    check("the prompt forbids inventing a requirement then deducting for it",
          "Never invent a requirement the rubric does not state and then deduct for it" in flat)
    check("the prompt makes correctness implicitly required",
          "Correctness is ALWAYS implicitly required" in flat)
    check("the prompt states the award/deduct asymmetry",
          "Use your knowledge freely to AWARD credit" in flat
          and "Be far more careful using it to DEDUCT" in flat)
    check("the prompt lists what may NOT be deducted for",
          "less thorough than you would like" in flat and "could have been optimised further" in flat)
    check("the prompt tells the grader to trace code logic",
          "Trace the logic" in flat and "off-by-one" in flat)
    check("the prompt ignores code style and unasked-for efficiency",
          "Ignore naming, formatting, comments and language style" in flat
          and "Ignore efficiency unless the question or rubric asks" in flat)
    check("the prompt judges code separately from the prose beside it",
          "Code that contradicts its own explanation" in flat)
    check("the prompt grades open questions on reasoning, not conclusion",
          "A well-argued position you disagree with earns full credit" in flat)
    check("the prompt requires the basis to be declared",
          '"basis" of exactly' in flat)
    check("the prompt asks for an honest confidence level",
          '"high", "medium" or "low"' in prompt and "not a failure" in prompt)


def main():
    test_arithmetic()
    test_impossible_scores()
    test_rubric_coverage()
    test_retries()
    test_evidence_verification()
    test_confidence()
    test_basis()
    test_anchor_examples()
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
