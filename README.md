# UITS_N8N_Autograder
<img width="1354" height="251" alt="Screenshot 2026-01-16 at 11 02 35 AM" src="https://github.com/user-attachments/assets/b06af9b4-1c8c-49ac-b8a0-35a06fbc714b" />

## Faculty Autograder (Flask app)

A Flask app that replaces the earlier n8n/Google Drive prototype (`autograder.json`, kept for reference). Faculty log in with IU Login, connect their Canvas account, pick a course/assignment, upload a rubric, and the app grades every submission with an LLM. AI grades land in a review queue — nothing is written back to Canvas automatically; faculty approve/edit grades, then export a formatted Excel sheet for manual entry into the Canvas gradebook.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then create a `.env` file in the repo root (it is gitignored, and is only used for
local development — on Azure these same names are set as Application Settings instead):

```
FLASK_SECRET_KEY=
APP_ENV=development
IU_OIDC_DISCOVERY_URL=
IU_OIDC_CLIENT_ID=
IU_OIDC_CLIENT_SECRET=
IU_OIDC_REDIRECT_URI=http://localhost:8000/auth/iu/callback
CANVAS_BASE_URL=https://iu.instructure.com
CANVAS_CLIENT_ID=
CANVAS_CLIENT_SECRET=
CANVAS_REDIRECT_URI=http://localhost:8000/oauth/callback
CANVAS_OAUTH_SCOPES=
REALLM_API_KEY=
REALLM_URL=
REALLM_MODEL_NAME=
DATABASE_PATH=instance/autograder.sqlite3
```

What each value is:
- `IU_OIDC_CLIENT_ID`, `IU_OIDC_CLIENT_SECRET` — issued by the IU Login team (IMS) after the OIDC Integration Request form is approved. The client ID is one you choose on the form; the secret is sent back securely.
- `IU_OIDC_DISCOVERY_URL` — IU Login runs on Shibboleth IdP, so these are fixed, not per-app:
  - pre-production: `https://idp-stg.login.iu.edu/.well-known/openid-configuration`
  - production: `https://idp.login.iu.edu/.well-known/openid-configuration`

  Test against pre-production first; UITS recommends it. Note IU requires service URLs to be **HTTPS only**, so a `http://localhost` redirect URI generally cannot be registered — plan to test against the deployed HTTPS host.

  Scopes requested are `openid profile email`, all of which are public scopes needing no Data Steward approval. `sub` is used as the per-user ownership key for everything stored in this app; profile/email claims are read from the ID token when present and otherwise fetched from the UserInfo endpoint (Shibboleth typically serves them there), with the UserInfo `sub` checked against the ID token's before being trusted.
- `CANVAS_BASE_URL`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET` — from the Canvas Developer Key (UITS/Erik). Redirect URI is `http://localhost:8000/oauth/callback`. Note the Dev Key currently lives on the Canvas **Test** instance, not production — confirm the exact domain when you receive credentials.
- `CANVAS_OAUTH_SCOPES` — space-separated scopes, only needed if the Developer Key enforces scopes.
- `REALLM_API_KEY`, `REALLM_URL`, `REALLM_MODEL_NAME` — your existing ReaLLM setup.
- `FLASK_SECRET_KEY` — any random string for signing the session cookie.
- `APP_ENV` — leave as `development` locally; set to `production` on the deployed (Azure) instance, which turns on secure (HTTPS-only) session cookies.

Run it:

```bash
python3 run.py
```

To test the IU Login flow locally you need HTTPS, because IU only accepts HTTPS
redirect URIs (confirmed with the IU Login team -- localhost is fine, but it must
be `https://`). Install `cryptography` once, then start the server with TLS:

```bash
pip install cryptography
USE_HTTPS=1 python3 run.py
```

That serves `https://localhost:8000` with a self-signed certificate; accept the
browser warning once. Register `https://localhost:8000/auth/iu/callback` as a
development redirect URI on the OIDC Integration Request form, alongside the
deployed one. Plain `python3 run.py` still serves HTTP for everything else.

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

### Deploying to Azure App Service (manual Zip Deploy)

1. **Create the App Service**: Linux, Python 3.11 runtime stack, Basic tier or higher (Free/Shared tiers don't support the always-on background-thread grading reliably, since they can idle/sleep the process mid-job).

2. **Set Application Settings** (Configuration → Application settings) — these become environment variables, same names as `.env`:
   - `FLASK_SECRET_KEY` — a real random value, not the dev default
   - `APP_ENV=production` — turns on secure (HTTPS-only) session cookies
   - `IU_OIDC_DISCOVERY_URL`, `IU_OIDC_CLIENT_ID`, `IU_OIDC_CLIENT_SECRET`, `IU_OIDC_REDIRECT_URI=https://<your-app>.azurewebsites.net/auth/iu/callback`
   - `CANVAS_BASE_URL`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET`, `CANVAS_REDIRECT_URI=https://<your-app>.azurewebsites.net/oauth/callback`
   - `REALLM_API_KEY`, `REALLM_URL`, `REALLM_MODEL_NAME`
   - **`DATABASE_PATH=/home/data/autograder.sqlite3`** — important: do *not* leave this as the local default (`instance/autograder.sqlite3`). Azure Zip Deploy replaces everything under the app's deployment folder (`/home/site/wwwroot`) on every redeploy, so a database living there would be silently wiped out the next time you ship an update. `/home` itself (outside `wwwroot`) persists across deployments and restarts — the app already creates the directory automatically if it doesn't exist.
   - `SCM_DO_BUILD_DURING_DEPLOYMENT=true` — makes Azure run `pip install -r requirements.txt` after unzipping (the repo's `.deployment` file also sets this, but setting it here too doesn't hurt).

3. **Set the Startup Command** (Configuration → General settings → Startup Command):
   ```
   gunicorn --bind=0.0.0.0 --timeout 600 run:app
   ```
   Without this, Azure's auto-detection may not find the app correctly since it uses an app-factory pattern (`run:app`, not a bare `app.py`).

4. **Build and upload the zip**: from the repo root, zip everything *except* `.git/`, `__pycache__/`, `.venv`/`venv`, `.env`, and `instance/` (the local dev database — don't ship that to prod). Upload via **Deployment Center → Zip Deploy**, or **Advanced Tools (Kudu) → Tools → Zip Push Deploy** (drag the zip onto the page), or `az webapp deploy --resource-group <rg> --name <app> --src-path app.zip --type zip` if you have the Azure CLI.

5. **Once deployed**, take the app's real URL (`https://<your-app>.azurewebsites.net`) and:
   - Send `https://<your-app>.azurewebsites.net/auth/iu/callback` to the IU Login team as the production redirect URI.
   - Send `https://<your-app>.azurewebsites.net/oauth/callback` to UITS (Erik) as the production Canvas redirect URI, per what Dhruv already told them to expect.

6. **Verify it's alive**: `https://<your-app>.azurewebsites.net/healthz` should return `{"status": "ok"}`.

### Notes / follow-ups

- Both IU and Canvas tokens are stored server-side (SQLite), never in the browser session cookie.
- CSRF protection (Flask-WTF) is wired into all state-changing forms.
- Session cookies are marked `Secure` automatically when `APP_ENV=production` (requires HTTPS, which Azure App Service provides by default).
- `ProxyFix` is applied so redirect URIs build correctly behind Azure's reverse proxy.
- `/healthz` returns `{"status": "ok"}` for Azure's health check / load balancer probes.
- Grading runs in a background thread per process; only one grading run per assignment can be in flight at a time (a second click while one is running is refused with a message, not queued). Progress is persisted in SQLite, so it survives page refreshes and works correctly even behind multiple `gunicorn` workers. If the process running a job dies mid-run (dev-server reload, worker restart), the job is detected as stale after 2 minutes of no progress and the review page tells you to re-run grading rather than spinning forever.
- `autograder.json` is the original n8n prototype (Google Drive-based) and is kept only for reference.
- Run with `gunicorn` in production (included in `requirements.txt`), not the Flask dev server: `gunicorn -w 2 -b 0.0.0.0:8000 run:app`.
