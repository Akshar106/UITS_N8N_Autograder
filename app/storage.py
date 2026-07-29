import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

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
    course_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    assignment_name TEXT,
    rubric_text TEXT NOT NULL,
    rubric_filename TEXT,
    canvas_points_possible REAL,
    uploaded_at TEXT,
    PRIMARY KEY (course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS grading_jobs (
    course_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    updated_at TEXT,
    PRIMARY KEY (course_id, assignment_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    UNIQUE (course_id, assignment_id, student_id)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add new columns to a table created by
        # an older version of this schema -- patch those in for existing local DBs.
        try:
            db.execute("ALTER TABLE rubrics ADD COLUMN canvas_points_possible REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
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

def save_rubric(course_id, assignment_id, assignment_name, rubric_text, rubric_filename, canvas_points_possible=None):
    db = get_db()
    db.execute(
        """
        INSERT INTO rubrics (course_id, assignment_id, assignment_name, rubric_text, rubric_filename,
                              canvas_points_possible, uploaded_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(course_id, assignment_id) DO UPDATE SET
            assignment_name = excluded.assignment_name,
            rubric_text = excluded.rubric_text,
            rubric_filename = excluded.rubric_filename,
            canvas_points_possible = excluded.canvas_points_possible,
            uploaded_at = excluded.uploaded_at
        """,
        (course_id, assignment_id, assignment_name, rubric_text, rubric_filename, canvas_points_possible, _now()),
    )
    db.commit()


def get_rubric(course_id, assignment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM rubrics WHERE course_id = ? AND assignment_id = ?",
        (course_id, assignment_id),
    ).fetchone()
    return dict(row) if row else None


# --- submissions / review queue ----------------------------------------------

def upsert_graded_submission(course_id, assignment_id, assignment_name, student_id, student_name,
                              submission_filename, ai_score, ai_total, ai_feedback,
                              ai_breakdown_json, ai_error=None):
    db = get_db()
    db.execute(
        """
        INSERT INTO submissions (
            course_id, assignment_id, assignment_name, student_id, student_name,
            submission_filename, ai_score, ai_total, ai_feedback, ai_breakdown_json, ai_error, graded_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(course_id, assignment_id, student_id) DO UPDATE SET
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
        (course_id, assignment_id, assignment_name, student_id, student_name,
         submission_filename, ai_score, ai_total, ai_feedback, ai_breakdown_json, ai_error, _now()),
    )
    db.commit()


def list_submissions(course_id, assignment_id):
    db = get_db()
    rows = db.execute(
        """
        SELECT * FROM submissions
        WHERE course_id = ? AND assignment_id = ?
        ORDER BY student_name COLLATE NOCASE
        """,
        (course_id, assignment_id),
    ).fetchall()
    return [dict(r) for r in rows]


def save_faculty_review(submission_id, faculty_score, faculty_feedback, approved):
    db = get_db()
    db.execute(
        """
        UPDATE submissions
        SET faculty_score = ?, faculty_feedback = ?, approved = ?
        WHERE id = ?
        """,
        (faculty_score, faculty_feedback, 1 if approved else 0, submission_id),
    )
    db.commit()


# --- grading jobs (background grading progress) -------------------------------

def start_grading_job(course_id, assignment_id, total):
    db = get_db()
    db.execute(
        """
        INSERT INTO grading_jobs (course_id, assignment_id, status, total, done, message, updated_at)
        VALUES (?, ?, 'running', ?, 0, NULL, ?)
        ON CONFLICT(course_id, assignment_id) DO UPDATE SET
            status = 'running', total = excluded.total, done = 0, message = NULL, updated_at = excluded.updated_at
        """,
        (course_id, assignment_id, total, _now()),
    )
    db.commit()


def increment_grading_progress(course_id, assignment_id):
    db = get_db()
    db.execute(
        "UPDATE grading_jobs SET done = done + 1, updated_at = ? WHERE course_id = ? AND assignment_id = ?",
        (_now(), course_id, assignment_id),
    )
    db.commit()


def finish_grading_job(course_id, assignment_id, status, message=None):
    db = get_db()
    db.execute(
        "UPDATE grading_jobs SET status = ?, message = ?, updated_at = ? WHERE course_id = ? AND assignment_id = ?",
        (status, message, _now(), course_id, assignment_id),
    )
    db.commit()


def get_grading_job(course_id, assignment_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM grading_jobs WHERE course_id = ? AND assignment_id = ?",
        (course_id, assignment_id),
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
