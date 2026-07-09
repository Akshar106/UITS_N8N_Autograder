from flask import Blueprint, send_file

from app import excel_export, storage
from app.auth_utils import login_required

export_bp = Blueprint("export", __name__)


@export_bp.route("/courses/<course_id>/assignments/<assignment_id>/export.xlsx")
@login_required
def export_grades(course_id, assignment_id):
    rubric = storage.get_rubric(course_id, assignment_id)
    assignment_name = rubric["assignment_name"] if rubric else assignment_id
    submissions = storage.list_submissions(course_id, assignment_id)

    buffer = excel_export.build_grades_workbook(assignment_name, submissions)
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in assignment_name).strip() or "grades"

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{safe_name}_grades.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
