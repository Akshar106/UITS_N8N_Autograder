import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import storage
from app.auth_utils import login_required

review_bp = Blueprint("review", __name__)


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review")
@login_required
def review_queue(course_id, assignment_id):
    rubric = storage.get_rubric(course_id, assignment_id)
    submissions = storage.list_submissions(course_id, assignment_id)
    for s in submissions:
        s["breakdown"] = json.loads(s["ai_breakdown_json"]) if s["ai_breakdown_json"] else []
    return render_template(
        "review.html",
        course_id=course_id,
        assignment_id=assignment_id,
        assignment_name=rubric["assignment_name"] if rubric else assignment_id,
        submissions=submissions,
    )


@review_bp.route("/courses/<course_id>/assignments/<assignment_id>/review/<int:submission_id>/save", methods=["POST"])
@login_required
def save_review(course_id, assignment_id, submission_id):
    faculty_score_raw = request.form.get("faculty_score", "").strip()
    faculty_score = float(faculty_score_raw) if faculty_score_raw else None
    faculty_feedback = request.form.get("faculty_feedback", "").strip() or None
    approved = request.form.get("approved") == "on"

    storage.save_faculty_review(submission_id, faculty_score, faculty_feedback, approved)
    flash("Saved.", "success")
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))
