import json

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app import storage
from app.auth_utils import canvas_required, current_user_id

review_bp = Blueprint("review", __name__)


TOTAL_MISMATCH_TOLERANCE = 0.01

# Lower sorts first. Faculty attention is the scarce resource in this whole
# design, so the queue leads with the rows where it actually changes an outcome:
# something the grader could not do, a review the work has outrun, and then the
# judgments the grader itself reported as shaky. Rows it was confident about --
# usually most of them -- come last, where a quick scan is enough.
ATTENTION_ORDER = {"error": 0, "stale": 1, "low": 2, "medium": 3, "high": 4}


def _attention_bucket(submission):
    if submission["ai_error"]:
        return "error"
    if submission["review_stale"]:
        return "stale"
    # A grade based on a file we only partly read needs a human eye whatever the
    # model thought of its own certainty.
    if submission["extraction_notes"]:
        return "low"
    # Two independent readings that landed on different numbers is the strongest
    # uncertainty signal available, and unlike the model's self-assessment it is
    # measured rather than claimed. The stored value is already filtered to
    # spreads big enough to change the student's grade (see grading.py), so any
    # disagreement that survives to here is worth a look.
    if (submission["ai_disagreement"] or 0) > 0:
        return "low"
    return submission["ai_confidence"] or "medium"


def _sort_key(submission):
    return (
        ATTENTION_ORDER.get(_attention_bucket(submission), 3),
        (submission["student_name"] or "").lower(),
    )


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review")
@canvas_required
def review_queue(course_id, assignment_id):
    user_id = current_user_id()
    rubric = storage.get_rubric(user_id, course_id, assignment_id)
    submissions = storage.list_submissions(user_id, course_id, assignment_id)
    for s in submissions:
        s["breakdown"] = json.loads(s["ai_breakdown_json"]) if s["ai_breakdown_json"] else []
        s["attention"] = _attention_bucket(s)
        s["unverified_quotes"] = sum(1 for q in s["breakdown"] if q.get("evidence_verified") is False)
        s["notes"] = [n for n in (s["extraction_notes"] or "").split("\n") if n.strip()]
        # Recorded by app/ocr.py when a submission had to be transcribed.
        s["from_ocr"] = "machine transcription" in (s["extraction_notes"] or "")
        s["knowledge_judgments"] = sum(
            1 for q in s["breakdown"] if q.get("basis") in ("knowledge", "both"))

    # storage returns them by name; re-order unless the faculty member asked for
    # the roster order instead (handy when entering grades into Canvas).
    sort = "name" if request.args.get("sort") == "name" else "attention"
    if sort == "attention":
        submissions.sort(key=_sort_key)

    total_mismatch = None
    expected_total = rubric.get("canvas_points_possible") if rubric else None
    if expected_total is not None:
        mismatched_totals = {
            s["ai_total"] for s in submissions
            if s["ai_total"] is not None and abs(s["ai_total"] - expected_total) > TOTAL_MISMATCH_TOLERANCE
        }
        if mismatched_totals:
            total_mismatch = {
                "expected": expected_total,
                "found": sorted(mismatched_totals),
            }

    job = storage.get_grading_job(user_id, course_id, assignment_id)
    job_stale = storage.job_is_stale(job)
    stale_reviews = sum(1 for s in submissions if s["review_stale"])
    needs_attention = sum(1 for s in submissions if s["attention"] in ("error", "stale", "low"))
    approved_count = sum(1 for s in submissions if s["approved"])

    return render_template(
        "review.html",
        course_id=course_id,
        assignment_id=assignment_id,
        assignment_name=rubric["assignment_name"] if rubric else assignment_id,
        submissions=submissions,
        total_mismatch=total_mismatch,
        stale_reviews=stale_reviews,
        needs_attention=needs_attention,
        approved_count=approved_count,
        sort=sort,
        job=job,
        job_stale=job_stale,
    )


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review/<int:submission_id>/save", methods=["POST"])
@canvas_required
def save_review(course_id, assignment_id, submission_id):
    faculty_score_raw = request.form.get("faculty_score", "").strip()
    if faculty_score_raw:
        try:
            faculty_score = float(faculty_score_raw)
        except ValueError:
            flash(f"'{faculty_score_raw}' is not a valid score — nothing was saved.", "error")
            return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))
    else:
        faculty_score = None
    faculty_feedback = request.form.get("faculty_feedback", "").strip() or None
    approved = request.form.get("approved") == "on"

    # submission_id is a global id; save_faculty_review only touches rows owned by
    # this user, and reports back whether anything matched.
    saved = storage.save_faculty_review(
        current_user_id(), submission_id, faculty_score, faculty_feedback, approved
    )
    if not saved:
        abort(404)

    flash("Saved.", "success")
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))
