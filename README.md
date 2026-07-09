# UITS_N8N_Autograder
<img width="1354" height="251" alt="Screenshot 2026-01-16 at 11 02 35 AM" src="https://github.com/user-attachments/assets/b06af9b4-1c8c-49ac-b8a0-35a06fbc714b" />

## Faculty Autograder (Flask app)

A Flask app that replaces the earlier n8n/Google Drive prototype (`autograder.json`, kept for reference). Faculty log in with their Canvas account, pick a course/assignment, upload a rubric, and the app grades every submission with an LLM. AI grades land in a review queue — nothing is written back to Canvas automatically; faculty approve/edit grades, then export a formatted Excel sheet for manual entry into the Canvas gradebook.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `CANVAS_BASE_URL`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET` — from the Canvas Developer Key once UITS creates it. Local dev redirect URI is fixed at `http://localhost:8000/oauth/callback` (already communicated to UITS).
- `CANVAS_OAUTH_SCOPES` — space-separated scopes, only needed if the Developer Key enforces scopes.
- `REALLM_API_KEY`, `REALLM_URL`, `REALLM_MODEL_NAME` — your existing ReaLLM setup.
- `FLASK_SECRET_KEY` — any random string for signing the session cookie.

Run it:

```bash
python3 run.py
```

Visit `http://localhost:8000`.

### How it works

1. **Login** (`/login`) redirects to Canvas OAuth2. Canvas handles the actual IU login/Duo — this app never sees a password, only an access token scoped to the faculty member's own Canvas permissions.
2. **Courses/assignments** are pulled live from the Canvas API (read-only).
3. **Rubric upload** accepts PDF/DOCX/TXT, extracts the text, and stores it per assignment.
4. **Grading** downloads each submission (file upload or text-entry), sends rubric + submission text to the LLM with a strict grading prompt, and stores the structured result (score, per-question breakdown, feedback) in a local SQLite review queue.
5. **Review** lets faculty see the AI's score/feedback per student, override either field, and mark it approved.
6. **Export** builds a formatted `.xlsx` (AI score, final score, percentage, approval status, feedback) for manual entry into the Canvas gradebook — the Developer Key is read-only, so there is no automated gradebook write-back.

### Notes / follow-ups

- Access tokens are stored server-side (SQLite), never in the browser session cookie.
- Grading currently runs synchronously in the request; for large rosters this may need to move to a background job.
- `autograder.json` is the original n8n prototype (Google Drive-based) and is kept only for reference.
- Before any real deployment: set a strong random `FLASK_SECRET_KEY`, serve over HTTPS, set `SESSION_COOKIE_SECURE=True`, and add CSRF tokens to the review/rubric forms (not yet wired in — this is local-dev-only right now).
