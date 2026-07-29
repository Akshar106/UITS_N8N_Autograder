import json
import threading

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

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

        storage.save_rubric(
            course_id, assignment_id, assignment["name"], rubric_text, file.filename,
            canvas_points_possible=assignment.get("points_possible"),
        )
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

    existing_job = storage.get_grading_job(course_id, assignment_id)
    if existing_job and existing_job["status"] == "running" and not storage.job_is_stale(existing_job):
        flash("Grading is already running for this assignment -- check back shortly.", "error")
        return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))

    # Fetching the submission list is a single fast Canvas call; only the
    # per-submission LLM grading (which can take minutes for a large roster)
    # runs in the background, so this request returns immediately either way.
    submissions = canvas_client.list_submissions(token, course_id, assignment_id)
    gradable = [
        s for s in submissions
        if s.get("workflow_state") != "unsubmitted" and s.get("user_id") is not None
    ]

    storage.start_grading_job(course_id, assignment_id, len(gradable))

    app = current_app._get_current_object()
    threading.Thread(
        target=_grade_submissions_job,
        args=(app, course_id, assignment_id, rubric, gradable, token),
        daemon=True,
    ).start()

    flash(
        f"Grading started for {len(gradable)} submitted assignment(s) "
        f"({len(submissions) - len(gradable)} skipped: unsubmitted or missing student identity). "
        f"This page updates automatically as results come in.",
        "success",
    )
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))


def _grade_submissions_job(app, course_id, assignment_id, rubric, submissions, access_token):
    """Runs in a background thread so a large roster can't block/timeout the request.

    Each graded submission is written as soon as it's ready, so the review
    queue can show partial results while grading is still in progress.
    """
    with app.app_context():
        try:
            for submission in submissions:
                user = submission.get("user") or {}
                student_id = str(submission["user_id"])
                student_name = user.get("name", f"Student {student_id}")

                student_text, submission_filename, extraction_error = _extract_submission_text(submission, access_token)

                if extraction_error:
                    storage.upsert_graded_submission(
                        course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                        submission_filename, None, None, None, None, ai_error=extraction_error,
                    )
                else:
                    result = grading.grade_submission(
                        rubric["assignment_name"], rubric["rubric_text"], student_id, student_text
                    )
                    if result["ok"]:
                        data = result["data"]
                        storage.upsert_graded_submission(
                            course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                            submission_filename,
                            data.get("score_awarded"), data.get("total_points"), data.get("overall_feedback"),
                            _dump_breakdown(data.get("per_question_breakdown")),
                        )
                    else:
                        storage.upsert_graded_submission(
                            course_id, assignment_id, rubric["assignment_name"], student_id, student_name,
                            submission_filename, None, None, None, None, ai_error=result["error"],
                        )

                storage.increment_grading_progress(course_id, assignment_id)

            storage.finish_grading_job(course_id, assignment_id, "done")
        except Exception as exc:  # noqa: BLE001 -- last-resort guard so the job never hangs at "running" forever
            storage.finish_grading_job(course_id, assignment_id, "error", str(exc))


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
