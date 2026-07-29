import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import storage
from app.auth_utils import login_required

review_bp = Blueprint("review", __name__)


TOTAL_MISMATCH_TOLERANCE = 0.01


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review")
@login_required
def review_queue(course_id, assignment_id):
    rubric = storage.get_rubric(course_id, assignment_id)
    submissions = storage.list_submissions(course_id, assignment_id)
    for s in submissions:
        s["breakdown"] = json.loads(s["ai_breakdown_json"]) if s["ai_breakdown_json"] else []

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

    job = storage.get_grading_job(course_id, assignment_id)

    return render_template(
        "review.html",
        course_id=course_id,
        assignment_id=assignment_id,
        assignment_name=rubric["assignment_name"] if rubric else assignment_id,
        submissions=submissions,
        total_mismatch=total_mismatch,
        job=job,
    )


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review/<int:submission_id>/save", methods=["POST"])
@login_required
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

    storage.save_faculty_review(submission_id, faculty_score, faculty_feedback, approved)
    flash("Saved.", "success")
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))
