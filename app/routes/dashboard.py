from flask import Blueprint, current_app, render_template, session

from app import canvas_client, storage
from app.auth_utils import canvas_required, current_user_id, get_valid_access_token

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/courses")
@canvas_required
def courses():
    token = get_valid_access_token()
    courses_list = canvas_client.list_instructor_courses(
        token, current_app.config["INSTRUCTOR_ENROLLMENT_TYPES"]
    )
    return render_template("dashboard.html", courses=courses_list, user_name=session.get("iu_name"))


@dashboard_bp.route("/courses/<course_id>/assignments")
@canvas_required
def assignments(course_id):
    user_id = current_user_id()
    token = get_valid_access_token()
    course = canvas_client.get_course(token, course_id)
    assignments_list = canvas_client.list_assignments(token, course_id)
    for a in assignments_list:
        a["_rubric_uploaded"] = storage.get_rubric(user_id, course_id, str(a["id"])) is not None
    # An empty list here is indistinguishable from a course that genuinely has no
    # assignments, and the most common cause is looking at a different course than
    # the one open in Canvas -- so log what was actually asked for.
    current_app.logger.info(
        "Listed %d assignment(s) for Canvas course %s", len(assignments_list), course_id
    )
    return render_template(
        "assignment.html",
        course=course,
        assignments=assignments_list,
        canvas_base_url=current_app.config["CANVAS_BASE_URL"],
    )
