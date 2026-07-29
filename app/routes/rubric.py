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
    attempted_count = 0

    for submission in submissions:
        if submission.get("workflow_state") == "unsubmitted":
            continue
        raw_user_id = submission.get("user_id")
        if raw_user_id is None:
            continue  # can't reliably record a grade with no student identity
        attempted_count += 1

        user = submission.get("user") or {}
        student_id = str(raw_user_id)
        student_name = user.get("name", f"Student {student_id}")

        student_text, submission_filename, extraction_error = _extract_submission_text(submission, token)

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

    flash(
        f"Graded {graded_count} of {attempted_count} submitted assignments "
        f"({len(submissions) - attempted_count} skipped: unsubmitted or missing student identity). "
        f"Review before entering grades in Canvas.",
        "success",
    )
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))


def _extract_submission_text(submission, access_token):
    attachments = submission.get("attachments") or []
    if attachments:
        # Students can upload more than one file (e.g. code + a writeup); grade
        # all of them by concatenating their text, not just the first.
        parts = []
        notes = []
        filenames = []
        for attachment in attachments:
            filename = attachment.get("filename", "file")
            try:
                content = canvas_client.download_attachment(attachment["url"], access_token)
                text = extract.extract_text(filename, content)
            except (canvas_client.CanvasAPIError, extract.ExtractionError) as exc:
                notes.append(f"[Could not read {filename}: {exc}]")
                continue
            if not text.strip():
                notes.append(f"[No text could be extracted from {filename} (scanned/image-only?)]")
                continue
            filenames.append(filename)
            parts.append(f"--- {filename} ---\n{text}")

        combined = "\n\n".join(parts)
        if not combined.strip():
            reason = " ".join(notes) or "No text could be extracted from the submitted file(s)."
            return None, ", ".join(a.get("filename", "file") for a in attachments), reason
        if notes:
            combined += "\n\n" + "\n".join(notes)
        return combined, ", ".join(filenames), None

    body = submission.get("body")
    if body and extract.strip_html(body):
        return extract.strip_html(body), None, None

    return None, None, "No file attachment or text entry found for this submission."


def _dump_breakdown(breakdown):
    return json.dumps(breakdown) if breakdown is not None else None
