import json
import threading

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from app import canvas_client, canvas_rubric, extract, grading, storage
from app.auth_utils import (access_token_for, canvas_required, current_user_id,
                            get_valid_access_token)

rubric_bp = Blueprint("rubric", __name__)

CANVAS_RUBRIC_HEADING = "SCORING RUBRIC (from the Canvas rubric attached to this assignment)"
NOTES_HEADING = "GRADING NOTES / ANSWER KEY (uploaded by the instructor)"


def _combine_rubric_sources(canvas_part, uploaded_text):
    """Build the rubric text the grader sees from whichever sources exist.

    Canvas supplies the authoritative point split; the uploaded file supplies
    what actually earns those points. Either alone is usable, and the headings
    tell the model which is which so it does not treat notes as the scoring
    scheme or vice versa.
    """
    sections = []
    if canvas_part:
        sections.append(f"{CANVAS_RUBRIC_HEADING}\n\n{canvas_part['text']}")
    if uploaded_text:
        sections.append(f"{NOTES_HEADING}\n\n{uploaded_text}")
    return "\n\n\n".join(sections)


@rubric_bp.route("/courses/<course_id>/assignments/<assignment_id>/rubric", methods=["GET", "POST"])
@canvas_required
def upload_rubric(course_id, assignment_id):
    user_id = current_user_id()
    # Fetching the assignment with this user's own Canvas token is what enforces
    # that they actually have access to this course: Canvas rejects the call
    # otherwise, which surfaces as a CanvasAPIError rather than a rubric upload.
    token = get_valid_access_token()
    assignment = canvas_client.get_assignment(token, course_id, assignment_id)
    canvas_part = canvas_rubric.from_assignment(assignment)

    if request.method == "POST":
        file = request.files.get("rubric_file")
        has_file = bool(file and file.filename)

        # With a Canvas rubric attached there is already a usable scoring scheme,
        # so the upload becomes optional grading notes rather than the whole rubric.
        if not has_file and not canvas_part:
            flash("Please choose a rubric file to upload.", "error")
            return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

        uploaded_text = ""
        if has_file:
            try:
                uploaded_text = extract.extract_text(file.filename, file.read())
            except extract.ExtractionError as exc:
                flash(str(exc), "error")
                return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

            if not uploaded_text.strip():
                flash("Could not find any text in that file.", "error")
                return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

        rubric_text = _combine_rubric_sources(canvas_part, uploaded_text.strip())
        source = "canvas+file" if (canvas_part and has_file) else ("canvas" if canvas_part else "file")

        # Canvas's own rubric total beats the assignment's points_possible: when a
        # rubric drives the grade they can legitimately differ, and the rubric is
        # what the grader was given.
        points_possible = assignment.get("points_possible")
        if canvas_part and canvas_part["points_possible"] is not None:
            points_possible = canvas_part["points_possible"]

        # The criteria are stored, not just rendered into the prompt: grading
        # checks the model's breakdown against them, which is what catches a
        # silently skipped question.
        criteria_json = json.dumps([
            {"description": c["description"], "points": c["points"],
             "binary": bool(c.get("binary"))}
            for c in canvas_part["criteria"]
        ]) if canvas_part else None

        rubric_changed = storage.save_rubric(
            user_id, course_id, assignment_id, assignment["name"], rubric_text,
            file.filename if has_file else None,
            canvas_points_possible=points_possible,
            rubric_source=source,
            canvas_criteria_json=criteria_json,
        )
        flash("Rubric saved. You can now run grading.", "success")

        # Scores approved under the previous rubric were reached against a
        # different scoring scheme, so they no longer describe how this
        # assignment is marked.
        if rubric_changed:
            expired = storage.mark_reviews_stale(user_id, course_id, assignment_id)
            if expired:
                flash(
                    f"The rubric changed, so {expired} previously approved "
                    f"{'grade needs' if expired == 1 else 'grades need'} to be re-checked. "
                    "Your earlier scores and comments are still there, but the approval was withdrawn.",
                    "error",
                )
        return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

    existing_rubric = storage.get_rubric(user_id, course_id, assignment_id)
    return render_template(
        "rubric_upload.html",
        course_id=course_id,
        assignment=assignment,
        existing_rubric=existing_rubric,
        canvas_rubric=canvas_part,
    )


@rubric_bp.route("/courses/<course_id>/assignments/<assignment_id>/grade", methods=["POST"])
@canvas_required
def run_grading(course_id, assignment_id):
    user_id = current_user_id()
    token = get_valid_access_token()
    rubric = storage.get_rubric(user_id, course_id, assignment_id)
    if not rubric:
        flash("Upload a rubric before running grading.", "error")
        return redirect(url_for("rubric.upload_rubric", course_id=course_id, assignment_id=assignment_id))

    existing_job = storage.get_grading_job(user_id, course_id, assignment_id)
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

    storage.start_grading_job(user_id, course_id, assignment_id, len(gradable))

    app = current_app._get_current_object()
    # user_id is captured here and passed explicitly: the background thread has no
    # request context, so it cannot read the session to find out who owns this work.
    threading.Thread(
        target=_grade_submissions_job,
        args=(app, user_id, course_id, assignment_id, rubric, gradable, token),
        daemon=True,
    ).start()

    flash(
        f"Grading started for {len(gradable)} submitted assignment(s) "
        f"({len(submissions) - len(gradable)} skipped: unsubmitted or missing student identity). "
        f"This page updates automatically as results come in.",
        "success",
    )
    return redirect(url_for("review.review_queue", course_id=course_id, assignment_id=assignment_id))


# How often a running job says it is still alive. Short enough that a real
# death is noticed quickly, long enough to be nothing in database terms.
HEARTBEAT_SECONDS = 30


def _heartbeat(app, iu_user_id, course_id, assignment_id, stop):
    """Touch the job while it works, so slow work is not mistaken for a crash.

    Its own thread deliberately: the work it is vouching for spends minutes
    blocked inside a single network call, so a heartbeat on the working thread
    would stop exactly when it is most needed.
    """
    with app.app_context():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                storage.touch_grading_job(iu_user_id, course_id, assignment_id)
            except Exception as exc:  # noqa: BLE001 -- never kill the run over a heartbeat
                current_app.logger.warning("Grading heartbeat failed: %s", exc)


def _grade_submissions_job(app, iu_user_id, course_id, assignment_id, rubric, submissions, access_token):
    """Runs in a background thread so a large roster can't block/timeout the request.

    Each graded submission is written as soon as it's ready, so the review
    queue can show partial results while grading is still in progress.
    """
    stop_heartbeat = threading.Event()
    threading.Thread(
        target=_heartbeat,
        args=(app, iu_user_id, course_id, assignment_id, stop_heartbeat),
        daemon=True,
    ).start()

    with app.app_context():
        # Parsed once, outside the loop: these are what let grading reject a
        # response that dropped a question or graded out of the wrong total.
        expected_criteria = json.loads(rubric["canvas_criteria_json"]) if rubric["canvas_criteria_json"] else None
        expected_total = rubric["canvas_points_possible"]

        # Calibration: submissions this faculty member has already graded and
        # approved become worked examples for the rest of the roster, so the model
        # matches THIS instructor's strictness on THIS assignment rather than a
        # generic reading of the rubric. Empty on a first run, which is fine --
        # grading then works exactly as it did before.
        anchors = _build_anchor_examples(
            iu_user_id, course_id, assignment_id, submissions, access_token
        )
        if anchors:
            current_app.logger.info(
                "Calibrating against %d instructor-graded example(s)", len(anchors)
            )

        try:
            for submission in submissions:
                user = submission.get("user") or {}
                # The student identity always comes from Canvas, never from the LLM's
                # echoed-back student_id, which could be hallucinated or transposed.
                student_id = str(submission["user_id"])
                student_name = user.get("name", f"Student {student_id}")

                # Re-read the token each time rather than reusing the one this
                # job started with: a full roster can outlast a Canvas token, and
                # a stale one turns every remaining download into a failure.
                try:
                    access_token = access_token_for(iu_user_id)
                except Exception as exc:  # noqa: BLE001 -- reported on the row, not raised
                    current_app.logger.warning("Canvas token unavailable mid-run: %s", exc)

                student_text, submission_filename, extraction_error, extraction_notes, from_ocr = \
                    _extract_submission_text(submission, access_token, rubric["assignment_name"])
                notes_text = "\n".join(extraction_notes) or None

                # Canvas bumps `attempt` on every resubmission. Storing it is what
                # lets the next grading run tell "same work, graded again" apart
                # from "different work in the same row", so an approval granted
                # for one attempt cannot silently carry over to another.
                attempt = submission.get("attempt")
                submitted_at = submission.get("submitted_at")

                if extraction_error:
                    storage.upsert_graded_submission(
                        iu_user_id, course_id, assignment_id, rubric["assignment_name"], student_id,
                        student_name, submission_filename, None, None, None, None,
                        ai_error=extraction_error, extraction_notes=notes_text,
                        submission_attempt=attempt, submitted_at=submitted_at,
                    )
                else:
                    # A transcribed submission is forced to low confidence and
                    # sent to a human whatever the passes say, so the second
                    # pass buys a disagreement signal nobody acts on -- and it
                    # doubles the slowest part of an already slow run.
                    result = grading.grade_submission(
                        rubric["assignment_name"], rubric["rubric_text"], student_id, student_text,
                        expected_criteria=expected_criteria, expected_total=expected_total,
                        anchors=anchors, passes=1 if from_ocr else None,
                    )
                    if result["ok"]:
                        data = result["data"]
                        if from_ocr:
                            # The evidence check cannot protect a transcript from
                            # itself: a quote is verified against the machine's
                            # reading, so a misreading is confirmed, not caught.
                            # The row goes to the top of the queue regardless of
                            # how sure the grader says it is.
                            data["confidence"] = "low"
                        storage.upsert_graded_submission(
                            iu_user_id, course_id, assignment_id, rubric["assignment_name"], student_id,
                            student_name, submission_filename,
                            data.get("score_awarded"), data.get("total_points"), data.get("overall_feedback"),
                            _dump_breakdown(data.get("per_question_breakdown")),
                            ai_confidence=data.get("confidence"),
                            ai_disagreement=data.get("max_disagreement"),
                            extraction_notes=notes_text,
                            submission_attempt=attempt, submitted_at=submitted_at,
                        )
                    else:
                        storage.upsert_graded_submission(
                            iu_user_id, course_id, assignment_id, rubric["assignment_name"], student_id,
                            student_name, submission_filename, None, None, None, None,
                            ai_error=result["error"], extraction_notes=notes_text,
                            submission_attempt=attempt, submitted_at=submitted_at,
                        )

                storage.increment_grading_progress(iu_user_id, course_id, assignment_id)

            storage.finish_grading_job(iu_user_id, course_id, assignment_id, "done")
        except Exception as exc:  # noqa: BLE001 -- last-resort guard so the job never hangs at "running" forever
            storage.finish_grading_job(iu_user_id, course_id, assignment_id, "error", str(exc))
        finally:
            stop_heartbeat.set()


def _build_anchor_examples(iu_user_id, course_id, assignment_id, submissions, access_token):
    """Pair the faculty member's own settled grades with the text they graded.

    The submission text is deliberately not kept in the database -- it is student
    work, and the less of it stored the better -- so it is re-extracted here from
    the same Canvas fetch the run is already working through.
    """
    reviewed = storage.list_reviewed_student_ids(iu_user_id, course_id, assignment_id)
    if not reviewed:
        return []

    by_student = {str(s["user_id"]): s for s in submissions if s.get("user_id") is not None}
    candidates = []
    for row in reviewed:
        submission = by_student.get(str(row["student_id"]))
        if submission is None:
            continue  # graded earlier but no longer in this fetch
        text, _filename, error, _notes, _ocr = _extract_submission_text(submission, access_token)
        if error or not text:
            continue
        candidates.append({
            "score": row["faculty_score"],
            "feedback": row["faculty_feedback"],
            "text": text,
        })

    return grading.select_anchor_examples(candidates)


def _extract_submission_text(submission, access_token, assignment_name=""):
    """Return (text, filenames, error, notes, from_ocr) for one Canvas submission.

    notes are warnings about *how well* the file was read -- images that could
    not be looked at, a table whose ordering may be off, a non-UTF-8 encoding.
    They are not failures, but they are the difference between a faculty member
    trusting a score and knowing to open the file first.

    from_ocr says the text is a machine's reading of handwriting or a scan
    rather than a text layer. It travels separately from the notes because it
    changes how much the resulting grade can be trusted, not merely what the
    faculty member is told.
    """
    attachments = submission.get("attachments") or []
    if attachments:
        # Students can upload more than one file (e.g. code + a writeup); grade
        # all of them by concatenating their text, not just the first.
        parts, problems, notes, filenames = [], [], [], []
        incomplete = False
        from_ocr = False

        for attachment in attachments:
            filename = attachment.get("filename", "file")
            try:
                content = canvas_client.download_attachment(attachment["url"], access_token)
                document = extract.extract_document(filename, content, assignment_name)
            except (canvas_client.CanvasAPIError, extract.ExtractionError) as exc:
                problems.append(f"[Could not read {filename}: {exc}]")
                continue

            filenames.append(filename)
            parts.append(f"--- {filename} ---\n{document.text}")
            notes.extend(f"{filename}: {note}" for note in document.notes)
            incomplete = incomplete or document.looks_incomplete
            from_ocr = from_ocr or bool(document.stats.get("from_ocr"))

        combined = "\n\n".join(parts)
        if not combined.strip():
            reason = " ".join(problems) or "No text could be extracted from the submitted file(s)."
            return None, ", ".join(a.get("filename", "file") for a in attachments), reason, notes, from_ocr

        # A file that could not be read AT ALL is different from one read with
        # warnings, and conflating them produced a real wrong grade: a
        # handwritten PDF timed out during transcription, the two code files
        # beside it graded fine, and the student got 19.5/100 with "problems 3-5
        # were not attempted" -- for work they had done. A missing document
        # means the grade covers unknown scope, so there is no honest score to
        # report and the row goes back as an error naming the file.
        if problems:
            notes.extend(problems)
            return (None, ", ".join(filenames) or ", ".join(
                        a.get("filename", "file") for a in attachments),
                    "Part of this submission could not be read, so it was not graded: "
                    + " ".join(problems),
                    notes, from_ocr)

        # A file that visibly holds content we could not read is refused rather
        # than graded: an invented zero on correct work is the worst outcome
        # this pipeline can produce, and it looks entirely plausible.
        if incomplete:
            return None, ", ".join(filenames), " ".join(notes) or "Extraction looks incomplete.", notes, from_ocr

        return combined, ", ".join(filenames), None, notes, from_ocr

    body = submission.get("body")
    if body and extract.strip_html(body):
        return extract.strip_html(body), None, None, [], False

    return None, None, "No file attachment or text entry found for this submission.", []


def _dump_breakdown(breakdown):
    return json.dumps(breakdown) if breakdown is not None else None
