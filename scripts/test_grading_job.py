#!/usr/bin/env python3
"""Guards the background grading job: its concurrency rules and its contracts.

No Canvas, no LLM and no network -- real threads, real SQLite, stubbed edges:

    python3 scripts/test_grading_job.py

Grading several submissions at once is the difference between a class of a
hundred taking minutes and taking an hour, but both times this code path has
been made concurrent it has broken in a way that unit tests on single calls
could not see. Everything here therefore runs through a REAL thread pool.

Two rules, each of which has already cost a production outage or nearly did:

  application context   Every storage call reaches the database through
                        Flask's `g`, which is per-context, not per-thread. A
                        pool worker starts with no context at all. Forgetting
                        this took down every multi-page submission once; the
                        failure looked like a killed worker, not a bug.

  the gateway ceiling   OCR_MAX_IN_FLIGHT caps calls in flight to the vision
                        gateway across the whole run, and it SCALES with the
                        number of submissions being graded. For one commit it
                        did not: the total was pinned at one submission's worth
                        of pages, so three handwritten submissions shared what
                        one used to have and each took twice as long, while the
                        batch as a whole looked faster. A total that does not
                        scale is not a budget, it is a queue.

Plus the things a roster needs to survive: one bad submission must not take
the other ninety-nine with it, progress must be counted exactly once per
submission however it ended, and every submission must be graded exactly once.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Set before the first `app` import: Config reads the environment at import
# time, so importing the package first would bind the real database and these
# tests would write into it.
os.environ["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "import-time.sqlite3")
os.environ.setdefault("FLASK_SECRET_KEY", "test-only")

FAILS = []

OWNER, COURSE, ASSIGNMENT = "iu-owner", "101", "202"


def check(label, condition):
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILS.append(label)


def make_app():
    from flask import Flask

    from app import storage

    app = Flask(__name__)
    app.config["DATABASE_PATH"] = os.path.join(tempfile.mkdtemp(), "concurrency.sqlite3")
    storage.init_db(app)
    return app


def fake_submissions(count):
    return [
        {
            "user_id": 3000 + i,
            "user": {"name": f"Student {i}"},
            "attempt": 1,
            "submitted_at": "2026-01-01T00:00:00Z",
            "attachments": [],
        }
        for i in range(count)
    ]


RUBRIC = {
    "assignment_name": "Test Assignment",
    "rubric_text": "Q1 (10 points): do the thing.",
    "canvas_criteria_json": None,
    "canvas_points_possible": 10.0,
}


def run_job(app, rubric_module, submissions):
    """Drive the real background job to completion, in this thread."""
    from app import storage

    with app.app_context():
        storage.start_grading_job(OWNER, COURSE, ASSIGNMENT, len(submissions))
    rubric_module._grade_submissions_job(
        app, OWNER, COURSE, ASSIGNMENT, RUBRIC, submissions, "canvas-token")


def stub_edges(rubric_module, extract=None, grade=None):
    """Replace Canvas and the LLM. Returns the originals for restoring."""
    original = (
        rubric_module._extract_submission_text,
        rubric_module.grading.grade_submission,
        rubric_module._build_anchor_examples,
        rubric_module.access_token_for,
    )

    def default_extract(submission, access_token, assignment_name="", progress=None,
                        cache=None, **kwargs):
        return ("the student's answer", "answer.pdf", None, [], False)

    def default_grade(assignment_name, rubric_text, student_id, student_text, **kwargs):
        return {"ok": True, "data": {
            "score_awarded": 8.0, "total_points": 10.0,
            "overall_feedback": "fine", "per_question_breakdown": [],
            "confidence": "high",
        }}

    rubric_module._extract_submission_text = extract or default_extract
    rubric_module.grading.grade_submission = grade or default_grade
    rubric_module._build_anchor_examples = lambda *a, **k: []
    rubric_module.access_token_for = lambda *a, **k: "canvas-token"
    return original


def restore_edges(rubric_module, original):
    (rubric_module._extract_submission_text,
     rubric_module.grading.grade_submission,
     rubric_module._build_anchor_examples,
     rubric_module.access_token_for) = original


def test_pool_threads_have_an_application_context():
    """The outage: storage from a pool worker, which has no Flask context."""
    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- every pool worker can reach the database --")
    app = make_app()
    rubric_module.GRADING_CONCURRENCY = 4
    submissions = fake_submissions(6)

    seen_threads = set()
    context_errors = []

    def extract(submission, access_token, assignment_name="", progress=None,
                cache=None, **kwargs):
        seen_threads.add(threading.current_thread().name)
        try:
            # Exactly what transcription does from its own worker: a progress
            # callback and a cache lookup, both of which hit the database.
            if progress:
                progress(1, 3)
            if cache is not None:
                cache.get("no-such-page")
                cache.set("a-page-key", "transcribed text")
        except Exception as exc:  # noqa: BLE001 -- recorded, so the run finishes
            context_errors.append(repr(exc))
        return ("the student's answer", "answer.pdf", None, [], False)

    original = stub_edges(rubric_module, extract=extract)
    try:
        run_job(app, rubric_module, submissions)
    finally:
        restore_edges(rubric_module, original)

    check("no 'working outside of application context' from a pool worker",
          not context_errors)
    if context_errors:
        print("      " + context_errors[0])
    check("the work really did run on several threads", len(seen_threads) > 1)

    with app.app_context():
        rows = storage.list_submissions(OWNER, COURSE, ASSIGNMENT)
        job = storage.get_grading_job(OWNER, COURSE, ASSIGNMENT)
    check("every submission was stored", len(rows) == len(submissions))
    check("every submission was scored", all(r["ai_score"] == 8.0 for r in rows))
    check("no submission was graded twice",
          len({r["student_id"] for r in rows}) == len(submissions))
    check("progress counted every submission", job["done"] == len(submissions))
    check("the job finished as done", job["status"] == "done")


def test_one_bad_submission_does_not_take_the_roster():
    """A roster of a hundred must not be lost to one unreadable file."""
    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- one failing submission is isolated to its own row --")
    app = make_app()
    rubric_module.GRADING_CONCURRENCY = 3
    submissions = fake_submissions(5)

    def extract(submission, access_token, assignment_name="", progress=None,
                cache=None, **kwargs):
        if submission["user_id"] == 3002:
            raise RuntimeError("the gateway gave up on this one")
        return ("the student's answer", "answer.pdf", None, [], False)

    original = stub_edges(rubric_module, extract=extract)
    try:
        run_job(app, rubric_module, submissions)
    finally:
        restore_edges(rubric_module, original)

    with app.app_context():
        rows = {r["student_id"]: r for r in storage.list_submissions(OWNER, COURSE, ASSIGNMENT)}
        job = storage.get_grading_job(OWNER, COURSE, ASSIGNMENT)

    check("the other submissions were still graded",
          sum(1 for s, r in rows.items() if r["ai_score"] == 8.0) == 4)
    check("the failure is recorded on its own row", "3002" in rows)
    check("the failing row carries the reason",
          "gateway gave up" in (rows["3002"]["ai_error"] or ""))
    check("the failing row has no invented score", rows["3002"]["ai_score"] is None)
    check("progress still counted the failure", job["done"] == len(submissions))
    check("the job as a whole did not fail", job["status"] == "done")


def _measure_in_flight(submissions, pages, page_seconds=0.05):
    """Run N submissions of M pages at once; return (peak in flight, seconds)."""
    from concurrent.futures import ThreadPoolExecutor

    from app import ocr

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def reader(image, index, context, **kwargs):
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        time.sleep(page_seconds)
        with lock:
            in_flight -= 1
        return "page text"

    def one_submission(_):
        with ThreadPoolExecutor(max_workers=min(ocr.CONCURRENCY, pages)) as pool:
            list(pool.map(
                lambda i: ocr._read_with_retries(reader, f"page-{i}".encode(), i, ""),
                range(pages)))

    started = time.time()
    with ThreadPoolExecutor(max_workers=submissions) as pool:
        list(pool.map(one_submission, range(submissions)))
    return peak, time.time() - started, in_flight


def test_the_gateway_ceiling_is_a_ceiling_not_a_queue():
    """The regression: a total that does not scale starves every submission.

    OCR_MAX_IN_FLIGHT once defaulted to OCR_CONCURRENCY alone -- one
    submission's worth of pages, shared between all of them. The batch got
    faster and every individual handwritten submission got slower, which is
    the opposite of what anyone waiting on a grade experiences as speed.
    """
    from app import ocr

    print("\n-- a submission keeps its own page budget when others are running --")
    saved = (ocr.CONCURRENCY, ocr.MAX_IN_FLIGHT, ocr._GATEWAY_SLOTS)
    try:
        # Four pages each, as the app is configured, three submissions at once.
        ocr.set_concurrency(4, max_in_flight=4 * 3)

        alone_peak, alone_took, _ = _measure_in_flight(submissions=1, pages=8)
        together_peak, together_took, left = _measure_in_flight(submissions=3, pages=8)

        check(f"one submission uses its whole page budget (peak {alone_peak})",
              alone_peak == 4)
        check(f"three submissions scale the budget, not share it (peak {together_peak})",
              together_peak == 12)
        # The regression showed up as wall time, so that is what is measured.
        # Generous bound: this is about 1x versus 3x, not about precision.
        check(f"a submission is no slower for having company "
              f"({alone_took:.2f}s alone, {together_took:.2f}s together)",
              together_took < alone_took * 2)
        check("the budget is fully released", left == 0)

        # The ceiling still binds when it is set deliberately low, which is
        # what happens the day IU names a concurrency limit.
        ocr.set_concurrency(4, max_in_flight=5)
        capped_peak, _, left = _measure_in_flight(submissions=3, pages=8)
        check(f"an explicit low ceiling still holds (peak {capped_peak})",
              capped_peak <= 5)
        check("the low ceiling is actually reached", capped_peak == 5)
        check("the capped budget is released too", left == 0)
    finally:
        ocr.CONCURRENCY, ocr.MAX_IN_FLIGHT, ocr._GATEWAY_SLOTS = saved


def test_the_two_concurrency_defaults_agree():
    """ocr.py reads GRADING_CONCURRENCY from the environment to avoid a cycle.

    Two copies of a default is how a ceiling silently stops scaling, so the
    copies are pinned to each other here rather than left to a code reviewer.
    """
    import inspect

    from app import ocr
    from app.routes import rubric as rubric_module

    print("\n-- the two copies of the submission-count default agree --")
    ocr_source = inspect.getsource(ocr)
    rubric_source = inspect.getsource(rubric_module)
    check("ocr.py reads the same environment variable the route does",
          'getenv("GRADING_CONCURRENCY"' in ocr_source
          and 'getenv("GRADING_CONCURRENCY"' in rubric_source)

    # Not rubric.GRADING_CONCURRENCY: other tests here reassign it, so reading
    # it would make this check depend on the order they ran in. Both defaults
    # are re-derived from source instead.
    def written_default(source):
        import re
        match = re.search(r'getenv\("GRADING_CONCURRENCY",\s*"(\d+)"\)', source)
        return int(match.group(1)) if match else None

    ocr_default = written_default(ocr_source)
    rubric_default = written_default(rubric_source)
    check(f"both default to the same number ({ocr_default} and {rubric_default})",
          ocr_default is not None and ocr_default == rubric_default)
    check("the ceiling leaves every concurrent submission its full page budget",
          ocr.MAX_IN_FLIGHT >= ocr.CONCURRENCY * ocr._SUBMISSIONS_IN_FLIGHT)


def test_concurrency_of_one_still_works():
    """The serial path is what runs when a site sets GRADING_CONCURRENCY=1."""
    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- a single worker still grades the roster --")
    app = make_app()
    rubric_module.GRADING_CONCURRENCY = 1
    submissions = fake_submissions(3)

    original = stub_edges(rubric_module)
    try:
        run_job(app, rubric_module, submissions)
    finally:
        restore_edges(rubric_module, original)

    with app.app_context():
        rows = storage.list_submissions(OWNER, COURSE, ASSIGNMENT)
        job = storage.get_grading_job(OWNER, COURSE, ASSIGNMENT)
    check("every submission was graded serially", len(rows) == 3)
    check("progress is right on the serial path", job["done"] == 3)
    check("the serial job finished", job["status"] == "done")


def test_an_empty_roster_is_not_an_error():
    """`min(n, 0) or 1` exists for this; a zero-worker pool would raise."""
    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- an assignment nobody has submitted to --")
    app = make_app()
    rubric_module.GRADING_CONCURRENCY = 3

    original = stub_edges(rubric_module)
    try:
        run_job(app, rubric_module, [])
    finally:
        restore_edges(rubric_module, original)

    with app.app_context():
        job = storage.get_grading_job(OWNER, COURSE, ASSIGNMENT)
    check("an empty roster finishes cleanly", job["status"] == "done")


def test_transcribed_rows_are_still_forced_to_human_review():
    """Parallelism must not have dropped the handwriting safeguard."""
    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- a transcribed submission still goes to a human --")
    app = make_app()
    rubric_module.GRADING_CONCURRENCY = 3
    submissions = fake_submissions(4)

    passes_seen = []

    def extract(submission, access_token, assignment_name="", progress=None,
                cache=None, **kwargs):
        # Odd students handwrote theirs; even ones typed.
        from_ocr = submission["user_id"] % 2 == 1
        return ("the student's answer", "answer.pdf", None, [], from_ocr)

    def grade(assignment_name, rubric_text, student_id, student_text, **kwargs):
        passes_seen.append(kwargs.get("passes"))
        return {"ok": True, "data": {
            "score_awarded": 8.0, "total_points": 10.0,
            "overall_feedback": "fine", "per_question_breakdown": [],
            # The grader says it is sure. For a transcript that must not stand.
            "confidence": "high",
        }}

    original = stub_edges(rubric_module, extract=extract, grade=grade)
    try:
        run_job(app, rubric_module, submissions)
    finally:
        restore_edges(rubric_module, original)

    with app.app_context():
        rows = {r["student_id"]: r for r in storage.list_submissions(OWNER, COURSE, ASSIGNMENT)}

    check("a transcribed row is forced to low confidence",
          rows["3001"]["ai_confidence"] == "low" and rows["3003"]["ai_confidence"] == "low")
    check("a typed row keeps the grader's own confidence",
          rows["3000"]["ai_confidence"] == "high" and rows["3002"]["ai_confidence"] == "high")
    check("a transcribed row is graded in one pass", passes_seen.count(1) == 2)
    check("a typed row keeps the configured number of passes",
          passes_seen.count(None) == 2)


def test_every_extraction_path_returns_the_same_shape():
    """A submission with no file and no text used to raise ValueError.

    One return out of six carried four values where the caller unpacks five.
    It is not an obscure path: a link submission, an on-paper assignment, and
    a student who submitted and then deleted the file all reach it. Before
    grading ran concurrently, one such student took the whole roster down.

    The arity is checked across every path rather than only the one that was
    wrong, because the next edit is as likely to break a different one.
    """
    import ast
    import inspect

    from app.routes import rubric as rubric_module

    print("\n-- every way out of extraction returns the same number of values --")

    source = inspect.getsource(rubric_module._extract_submission_text)
    tree = ast.parse(source.lstrip())
    arities = {len(node.value.elts) for node in ast.walk(tree)
               if isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple)}
    check(f"all tuple returns agree on arity (found {sorted(arities)})",
          arities == {5})

    # And the behaviour, not just the shape.
    cases = [
        ("a link submission", {"submission_type": "online_url",
                               "url": "https://github.com/a/b"}, "link"),
        ("an on-paper assignment", {"submission_type": "on_paper"}, "on paper"),
        ("an empty submission", {}, "No file attachment"),
    ]
    for label, extra, expected in cases:
        submission = {"user_id": 1, "user": {"name": "A"},
                      "attachments": [], "body": None, **extra}
        try:
            text, _name, error, notes, from_ocr = \
                rubric_module._extract_submission_text(submission, "token")
        except ValueError as exc:
            check(f"{label} does not crash the caller", False)
            print(f"      {exc}")
            continue
        check(f"{label} does not crash the caller", True)
        check(f"{label} explains itself ({expected})", expected in (error or ""))
        check(f"{label} yields no text to grade", text is None)
        check(f"{label} is not reported as transcribed", from_ocr is False)


def test_anchors_are_chosen_before_they_are_read():
    """Reading fifty approved submissions to keep three is most of a run.

    The waste was invisible on typed work and enormous on handwritten work,
    where every approved anchor was re-transcribed from the gateway on every
    run -- all of it before the first new grade appeared.
    """
    from app import grading, storage
    from app.routes import rubric as rubric_module

    print("\n-- calibration reads only the submissions it will actually use --")
    app = make_app()

    approved = 50
    with app.app_context():
        for i in range(approved):
            storage.upsert_graded_submission(
                OWNER, COURSE, ASSIGNMENT, "N", str(i), f"S{i}", "f.pdf",
                float(i), 50.0, "ai", None)
            rows = {r["student_id"]: r for r in
                    storage.list_submissions(OWNER, COURSE, ASSIGNMENT)}
            storage.save_faculty_review(OWNER, rows[str(i)]["id"], float(i), "ok", True)

    submissions = [
        {"user_id": i, "user": {"name": f"S{i}"},
         "attachments": [{"filename": "f.pdf", "url": "u"}]}
        for i in range(approved)
    ]

    read, caches = [], []

    def fake_extract(submission, token, name="", progress=None, cache=None):
        read.append(submission["user_id"])
        caches.append(cache)
        return (f"text {submission['user_id']}", "f.pdf", None, [], False)

    original = rubric_module._extract_submission_text
    try:
        rubric_module._extract_submission_text = fake_extract
        sentinel = object()
        with app.app_context():
            anchors = rubric_module._build_anchor_examples(
                OWNER, COURSE, ASSIGNMENT, submissions, "token", cache=sentinel)
    finally:
        rubric_module._extract_submission_text = original

    check(f"only the chosen anchors are read ({len(read)} of {approved})",
          len(read) == grading.MAX_ANCHORS)
    check("the anchors still span the instructor's range",
          [a["score"] for a in anchors] == [0.0, 24.0, 49.0])
    check("the transcript cache is passed through to them",
          caches and all(c is sentinel for c in caches))
    check("each anchor carries the text that was read",
          all(a["text"] for a in anchors))


def test_a_healthy_job_is_not_declared_dead_under_load():
    """A running job must keep saying so while a roster hammers the database.

    It did not. Progress wrote to the database once per page per submission,
    from every transcription thread at once, and the liveness write had to win
    a lock against that storm. Measured, beats were delayed to six and a half
    times their interval and two thirds never landed -- at a 30s interval
    against a 180s window, a 196s gap. The job was reported dead, the faculty
    member restarted it, and it had been working the whole time.
    """
    import time

    from app import storage
    from app.routes import rubric as rubric_module

    print("\n-- a working job keeps proving it is alive --")

    beats = rubric_module.HEARTBEAT_SECONDS
    window = storage.STALE_JOB_SECONDS
    check(f"the stale window covers many beats, not a handful "
          f"({window}s / {beats}s = {window // beats})",
          window // beats >= 12)

    limiter = rubric_module._RateLimiter(interval=0.2)
    key = "owner/course/assignment"
    allowed = sum(1 for _ in range(500) if limiter.ready(key))
    check(f"a burst of progress writes collapses to one ({allowed})", allowed == 1)
    time.sleep(0.25)
    check("the next one is allowed once the interval passes", limiter.ready(key))
    check("a different job is limited separately", limiter.ready("other/c/a"))

    # The limiter is shared by every transcription thread, so it has to be
    # safe to call from all of them at once.
    import threading
    hits, lock = [], threading.Lock()
    shared = rubric_module._RateLimiter(interval=5.0)

    def hammer():
        if shared.ready("one-job"):
            with lock:
                hits.append(1)

    threads = [threading.Thread(target=hammer) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(f"50 threads racing produce exactly one write ({len(hits)})", len(hits) == 1)


def main():
    test_every_extraction_path_returns_the_same_shape()
    test_a_healthy_job_is_not_declared_dead_under_load()
    test_anchors_are_chosen_before_they_are_read()
    test_pool_threads_have_an_application_context()
    test_one_bad_submission_does_not_take_the_roster()
    test_the_gateway_ceiling_is_a_ceiling_not_a_queue()
    test_the_two_concurrency_defaults_agree()
    test_concurrency_of_one_still_works()
    test_an_empty_roster_is_not_an_error()
    test_transcribed_rows_are_still_forced_to_human_review()

    print()
    if FAILS:
        print(f"{len(FAILS)} CHECK(S) FAILED:")
        for label in FAILS:
            print("  - " + label)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
