import json

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app import canvas_client, extract, grading, storage
from app.auth_utils import login_required, get_valid_access_token

rubric_bp = Blueprint("rubric", __name__)


@rubric_bp.route("/courses/<course_id>/assignments/<assignment_id>/rubric", methods=["GET", "POST"])
@login_required
def upload_rubric(course_id, assignment_id):
    token = get_valid_access_token()
    assignment = canvas_client.get_assignment(token, course_id, assignment_id)

    if request.method == "POST":
        file = request.files.get("rubric_file")
        if not file or not file.filename:
            flash("Please choose a rubric file to upload.", "error")
            return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

        try:
            rubric_text = extract.extract_text(file.filename, file.read())
        except extract.ExtractionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

        if not rubric_text.strip():
            flash("Could not find any text in that rubric file.", "error")
            return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

        storage.save_rubric(course_id, assignment_id, assignment["name"], rubric_text, file.filename)
        flash("Rubric uploaded. You can now run grading.", "success")
        return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

    existing_rubric = storage.get_rubric(course_id, assignment_id)
    return render_template(
        "rubric_upload.html",
        course_id=course_id,
        assignment=assignment,
        existing_rubric=existing_rubric,
    )


@rubric_bp.route("/courses/<course_id>/assignments/<assignment_id>/grade", methods=["POST"])
@login_required
def run_grading(course_id, assignment_id):
    token = get_valid_access_token()
    rubric = storage.get_rubric(course_id, assignment_id)
    if not rubric:
        flash("Upload a rubric before running grading.", "error")
        return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

    submissions = canvas_client.list_submissions(token, course_id, assignment_id)
    graded_count = 0

    for submission in submissions:
        if submission.get("workflow_state") == "unsubmitted":
            continue

        user = submission.get("user") or {}
        student_id = str(submission.get("user_id"))
        student_name = user.get("name", f"Student {student_id}")

        student_text, submission_filename, extraction_error = _extract_submission_text(submission)

        if extraction_error:
            storage.upsert_graded_submission(
                course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                submission_filename, None, None, None, None, ai_error=extraction_error,
            )
            continue

        result = grading.grade_submission(rubric["assignment_name"], rubric["rubric_text"], student_id, student_text)

        if result["ok"]:
            data = result["data"]
            storage.upsert_graded_submission(
                course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                submission_filename,
                data.get("score_awarded"), data.get("total_points"), data.get("overall_feedback"),
                _dump_breakdown(data.get("per_question_breakdown")),
            )
            graded_count += 1
        else:
            storage.upsert_graded_submission(
                course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                submission_filename, None, None, None, None, ai_error=result["error"],
            )

    flash(f"Graded {graded_count} of {len(submissions)} submissions. Review before syncing to Canvas.", "success")
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))


def _extract_submission_text(submission):
    attachments = submission.get("attachments") or []
    if attachments:
        attachment = attachments[0]
        try:
            content = canvas_client.download_attachment(attachment["url"])
            text = extract.extract_text(attachment["filename"], content)
            return text, attachment["filename"], None
        except (canvas_client.CanvasAPIError, extract.ExtractionError) as exc:
            return None, attachment.get("filename"), str(exc)

    body = submission.get("body")
    if body:
        return extract.strip_html(body), None, None

    return None, None, "No file attachment or text entry found for this submission."


def _dump_breakdown(breakdown):
    return json.dumps(breakdown) if breakdown is not None else None
