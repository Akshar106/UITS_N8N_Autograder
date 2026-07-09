from flask import Blueprint, render_template, session

from app import canvas_client, storage
from app.auth_utils import login_required, get_valid_access_token

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/courses")
@login_required
def courses():
    token = get_valid_access_token()
    courses_list = canvas_client.list_teaching_courses(token)
    return render_template("dashboard.html", courses=courses_list, user_name=session.get("canvas_user_name"))


@dashboard_bp.route("/courses/<course_id>/assignments")
@login_required
def assignments(course_id):
    token = get_valid_access_token()
    course = canvas_client.get_course(token, course_id)
    assignments_list = canvas_client.list_assignments(token, course_id)
    for a in assignments_list:
        a["_rubric_uploaded"] = storage.get_rubric(course_id, str(a["id"])) is not None
    return render_template("assignment.html", course=course, assignments=assignments_list)
