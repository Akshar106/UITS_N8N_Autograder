# UITS_N8N_Autograder
<img width="1354" height="251" alt="Screenshot 2026-01-16 at 11 02 35 AM" src="https://github.com/user-attachments/assets/b06af9b4-1c8c-49ac-b8a0-35a06fbc714b" />

## Faculty Autograder (Flask app)

A Flask app that replaces the earlier n8n/Google Drive prototype (`autograder.json`, kept for reference). Faculty log in with IU Login, connect their Canvas account, pick a course/assignment, upload a rubric, and the app grades every submission with an LLM. AI grades land in a review queue — nothing is written back to Canvas automatically; faculty approve/edit grades, then export a formatted Excel sheet for manual entry into the Canvas gradebook.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:
- `IU_OIDC_DISCOVERY_URL`, `IU_OIDC_CLIENT_ID`, `IU_OIDC_CLIENT_SECRET` — from the IU Login team once they register this app. Local dev redirect URI is `http://localhost:8000/auth/iu/callback`.
- `CANVAS_BASE_URL`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET` — from the Canvas Developer Key (UITS/Erik). Redirect URI is `http://localhost:8000/oauth/callback`. Note the Dev Key currently lives on the Canvas **Test** instance, not production — confirm the exact domain when you receive credentials.
- `CANVAS_OAUTH_SCOPES` — space-separated scopes, only needed if the Developer Key enforces scopes.
- `REALLM_API_KEY`, `REALLM_URL`, `REALLM_MODEL_NAME` — your existing ReaLLM setup.
- `FLASK_SECRET_KEY` — any random string for signing the session cookie.
- `APP_ENV` — leave as `development` locally; set to `production` on the deployed (Azure) instance, which turns on secure (HTTPS-only) session cookies.

Run it:

```bash
python3 run.py
```

Visit `http://localhost:8000`.

**Testing the grading logic on its own**, without any of the above: see `scripts/test_grading.py` and `sample_data/`.

### How it works

Two separate logins, in order:

1. **IU Login** (`/auth/iu/login`) — the app's front door. Redirects to IU's identity provider (Duo included); this app never sees a password, only a signed ID token confirming who the user is. This is what the IU Login team needs to register — see "What to give the IU Login team" below.
2. **Connect Canvas** (`/connect-canvas` → `/canvas/connect`) — a one-time step per user, right after their first IU login. Canvas's own OAuth2 flow hands back an access token scoped to that person's own Canvas role/courses. This token (not the IU login) is what actually authorizes every Canvas API call, and is what UITS's Developer Key credentials are for.

Then:

3. **Courses/assignments** are pulled live from the Canvas API (read-only) using the connected Canvas token.
4. **Rubric upload** accepts PDF/DOCX/TXT, extracts the text, and stores it per assignment.
5. **Grading** runs in a background thread (started when you click "Grade all submissions," not blocking the request) — downloads each submission (file upload or text-entry, all attachments if there's more than one), sends rubric + submission text to the LLM with a strict grading prompt, and stores each result (score, per-question breakdown, feedback) as soon as it's ready. The review page shows live progress and refreshes automatically until grading finishes.
6. **Review** lets faculty see the AI's score/feedback per student, override either field, and mark it approved. If the AI's computed total doesn't match the assignment's actual Canvas points total, a warning banner flags it before anything gets entered into Canvas.
7. **Export** builds a formatted `.xlsx` (AI score, final score, percentage, approval status, feedback) for manual entry into the Canvas gradebook — the Developer Key is read-only, so there is no automated gradebook write-back.

### What to give the IU Login team

Once this app is deployed (e.g. to Azure), send them:
- The exact callback URL: `https://<your-deployed-domain>/auth/iu/callback`
- Ask if a `http://localhost:8000/auth/iu/callback` entry can *also* be registered for local development, so local testing isn't blocked on deployment.

They will send back: an OIDC discovery URL (issuer's `/.well-known/openid-configuration`), a client ID, and a client secret. Drop those into `.env` (or your host's secret store) and the login flow works with no code changes.

### What to give UITS (Canvas Developer Key)

The Dev Key already exists (created by Erik on the Test instance). When ready to test:
- Tell them you're ready to receive the client ID/secret via Secure Share.
- Confirm the Test-instance Canvas domain (likely not `iu.instructure.com` — check with UITS) for `CANVAS_BASE_URL`.
- Once deployed, send the production redirect URI (`https://<your-deployed-domain>/oauth/callback`) to be added alongside the localhost one.

### Notes / follow-ups

- Both IU and Canvas tokens are stored server-side (SQLite), never in the browser session cookie.
- CSRF protection (Flask-WTF) is wired into all state-changing forms.
- Session cookies are marked `Secure` automatically when `APP_ENV=production` (requires HTTPS, which Azure App Service provides by default).
- `ProxyFix` is applied so redirect URIs build correctly behind Azure's reverse proxy.
- `/healthz` returns `{"status": "ok"}` for Azure's health check / load balancer probes.
- Grading runs in a background thread per process; only one grading run per assignment can be in flight at a time (a second click while one is running is refused with a message, not queued). Progress is persisted in SQLite, so it survives page refreshes and works correctly even behind multiple `gunicorn` workers.
- `autograder.json` is the original n8n prototype (Google Drive-based) and is kept only for reference.
- Run with `gunicorn` in production (included in `requirements.txt`), not the Flask dev server: `gunicorn -w 2 -b 0.0.0.0:8000 run:app`.
