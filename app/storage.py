import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

# Every table below except canvas_tokens holds per-faculty data (rubrics, student
# grades, feedback). They are all keyed by iu_user_id -- the IU Login subject of the
# faculty member who owns the row -- and EVERY query in this module filters on it.
# Without that scoping any authenticated IU user could read or modify another
# faculty member's students' grades just by guessing a course/assignment id.
SCHEMA = """
CREATE TABLE IF NOT EXISTS canvas_tokens (
    iu_user_id TEXT PRIMARY KEY,
    canvas_user_id TEXT,
    canvas_user_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS rubrics (
    iu_user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    assignment_name TEXT,
    rubric_text TEXT NOT NULL,
    rubric_filename TEXT,
    canvas_points_possible REAL,
    uploaded_at TEXT,
    PRIMARY KEY (iu_user_id, course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS grading_jobs (
    iu_user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT,
    PRIMARY KEY (iu_user_id, course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iu_user_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    assignment_name TEXT,
    student_id TEXT NOT NULL,
    student_name TEXT,
    submission_filename TEXT,
    ai_score REAL,
    ai_total REAL,
    ai_feedback TEXT,
    ai_breakdown_json TEXT,
    ai_error TEXT,
    faculty_score REAL,
    faculty_feedback TEXT,
    approved INTEGER NOT NULL DEFAULT 0,
    graded_at TEXT,
    UNIQUE (iu_user_id, course_id, assignment_id, student_id)
);
"""

OWNED_TABLES = ("rubrics", "grading_jobs", "submissions")


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        # timeout: SQLite raises "database is locked" immediately by default. The
        # background grading thread writes while request threads read, and on Azure
        # the DB lives on network-mounted storage, so waiting is better than failing.
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"], timeout=30)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 30000")
    return g.db


def close_db(_exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate_pre_ownership_tables(db):
    """Retire tables created before rows carried an owner.

    Those rows have no iu_user_id, so their rightful owner cannot be inferred --
    guessing would hand one faculty member's grades to another. They are renamed
    aside (not dropped) so nothing is silently destroyed; rubrics can be re-uploaded
    and grading re-run.
    """
    for table in OWNED_TABLES:
        exists = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if "iu_user_id" not in columns:
            db.execute(f"ALTER TABLE {table} RENAME TO {table}_preowner_backup")


def init_db(app):
    with app.app_context():
        db = get_db()
        _migrate_pre_ownership_tables(db)
        db.executescript(SCHEMA)
        db.commit()


# --- canvas tokens (keyed by the IU Login identity) ---------------------------

def save_canvas_token(iu_user_id, canvas_user_id, canvas_user_name, access_token, refresh_token, expires_at):
    db = get_db()
    db.execute(
        """
        INSERT INTO canvas_tokens (iu_user_id, canvas_user_id, canvas_user_name, access_token, refresh_token, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(iu_user_id) DO UPDATE SET
            canvas_user_id = excluded.canvas_user_id,
            canvas_user_name = excluded.canvas_user_name,
            access_token = excluded.access_token,
            refresh_token = excluded.refresh_token,
            expires_at = excluded.expires_at
        """,
        (iu_user_id, canvas_user_id, canvas_user_name, access_token, refresh_token, expires_at),
    )
    db.commit()


def get_canvas_token(iu_user_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM canvas_tokens WHERE iu_user_id = ?", (iu_user_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_canvas_token(iu_user_id):
    db = get_db()
    db.execute("DELETE FROM canvas_tokens WHERE iu_user_id = ?", (iu_user_id,))
    db.commit()


# --- rubrics -----------------------------------------------------------------

def save_rubric(iu_user_id, course_id, assignment_id, assignment_name, rubric_text,
                rubric_filename, canvas_points_possible=None):
    db = get_db()
    db.execute(
        """
        INSERT INTO rubrics (iu_user_id, course_id, assignment_id, assignment_name, rubric_text,
                              rubric_filename, canvas_points_possible, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(iu_user_id, course_id, assignment_id) DO UPDATE SET
            assignment_name = excluded.assignment_name,
            rubric_text = excluded.rubric_text,
            rubric_filename = excluded.rubric_filename,
            canvas_points_possible = excluded.canvas_points_possible,
            uploaded_at = excluded.uploaded_at
        """,
        (iu_user_id, course_id, assignment_id, assignment_name, rubric_text,
         rubric_filename, canvas_points_possible, _now()),
    )
    db.commit()


def get_rubric(iu_user_id, course_id, assignment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM rubrics WHERE iu_user_id = ? AND course_id = ? AND assignment_id = ?",
        (iu_user_id, course_id, assignment_id),
    ).fetchone()
    return dict(row) if row else None


# --- submissions / review queue ----------------------------------------------

def upsert_graded_submission(iu_user_id, course_id, assignment_id, assignment_name, student_id,
                             student_name, submission_filename, ai_score, ai_total, ai_feedback,
                             ai_breakdown_json, ai_error=None):
    db = get_db()
    db.execute(
        """
        INSERT INTO submissions (
            iu_user_id, course_id, assignment_id, assignment_name, student_id, student_name,
            submission_filename, ai_score, ai_total, ai_feedback, ai_breakdown_json, ai_error, graded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(iu_user_id, course_id, assignment_id, student_id) DO UPDATE SET
            assignment_name = excluded.assignment_name,
            student_name = excluded.student_name,
            submission_filename = excluded.submission_filename,
            ai_score = excluded.ai_score,
            ai_total = excluded.ai_total,
            ai_feedback = excluded.ai_feedback,
            ai_breakdown_json = excluded.ai_breakdown_json,
            ai_error = excluded.ai_error,
            graded_at = excluded.graded_at
        """,
        (iu_user_id, course_id, assignment_id, assignment_name, student_id, student_name,
         submission_filename, ai_score, ai_total, ai_feedback, ai_breakdown_json, ai_error, _now()),
    )
    db.commit()
    # NOTE: faculty_score / faculty_feedback / approved are deliberately NOT in the
    # DO UPDATE list -- re-running grading must never silently discard a faculty
    # member's own edits or un-approve something they already signed off on.


def list_submissions(iu_user_id, course_id, assignment_id):
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM submissions
        WHERE iu_user_id = ? AND course_id = ? AND assignment_id = ?
        ORDER BY student_name COLLATE NOCASE
        """,
        (iu_user_id, course_id, assignment_id),
    ).fetchall()
    return [dict(r) for r in rows]


def save_faculty_review(iu_user_id, submission_id, faculty_score, faculty_feedback, approved):
    """Returns True if a row owned by this user was updated, False otherwise.

    submission_id is a global autoincrement id, so the iu_user_id filter is what
    stops one user from editing another user's grades by guessing an id.
    """
    db = get_db()
    cur = db.execute(
        """
        UPDATE submissions
        SET faculty_score = ?, faculty_feedback = ?, approved = ?
        WHERE id = ? AND iu_user_id = ?
        """,
        (faculty_score, faculty_feedback, 1 if approved else 0, submission_id, iu_user_id),
    )
    db.commit()
    return cur.rowcount > 0


# --- grading jobs (background grading progress) -------------------------------

def start_grading_job(iu_user_id, course_id, assignment_id, total):
    db = get_db()
    db.execute(
        """
        INSERT INTO grading_jobs (iu_user_id, course_id, assignment_id, status, total, done, message, updated_at)
        VALUES (?, ?, ?, 'running', ?, 0, NULL, ?)
        ON CONFLICT(iu_user_id, course_id, assignment_id) DO UPDATE SET
            status = 'running', total = excluded.total, done = 0, message = NULL, updated_at = excluded.updated_at
        """,
        (iu_user_id, course_id, assignment_id, total, _now()),
    )
    db.commit()


def increment_grading_progress(iu_user_id, course_id, assignment_id):
    db = get_db()
    db.execute(
        """
        UPDATE grading_jobs SET done = done + 1, updated_at = ?
        WHERE iu_user_id = ? AND course_id = ? AND assignment_id = ?
        """,
        (_now(), iu_user_id, course_id, assignment_id),
    )
    db.commit()


def finish_grading_job(iu_user_id, course_id, assignment_id, status, message=None):
    db = get_db()
    db.execute(
        """
        UPDATE grading_jobs SET status = ?, message = ?, updated_at = ?
        WHERE iu_user_id = ? AND course_id = ? AND assignment_id = ?
        """,
        (status, message, _now(), iu_user_id, course_id, assignment_id),
    )
    db.commit()


def get_grading_job(iu_user_id, course_id, assignment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM grading_jobs WHERE iu_user_id = ? AND course_id = ? AND assignment_id = ?",
        (iu_user_id, course_id, assignment_id),
    ).fetchone()
    return dict(row) if row else None


# A background grading thread dies with its process (dev-server auto-reload,
# a gunicorn worker recycling/restart, a deploy). When that happens the job
# row is left at status='running' forever with nothing left to finish it --
# treat "running" with no progress in a while as dead rather than trusting it.
STALE_JOB_SECONDS = 120


def job_is_stale(job):
    if not job or job["status"] != "running" or not job["updated_at"]:
        return False
    updated_at = datetime.fromisoformat(job["updated_at"])
    return (datetime.now(timezone.utc) - updated_at).total_seconds() > STALE_JOB_SECONDS
