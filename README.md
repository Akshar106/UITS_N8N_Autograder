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
GRADING_PASSES=2
OCR_BACKEND=none
DATABASE_PATH=instance/autograder.sqlite3
```

What each value is:
- `IU_OIDC_CLIENT_ID`, `IU_OIDC_CLIENT_SECRET` — issued by the IU Login team (IMS) after the OIDC Integration Request form is approved. The client ID is one you choose on the form; the secret is sent back securely.
- `IU_OIDC_DISCOVERY_URL` — IU Login runs on Shibboleth IdP, so these are fixed, not per-app:
  - pre-production: `https://idp-stg.login.iu.edu/.well-known/openid-configuration`
  - production: `https://idp.login.iu.edu/.well-known/openid-configuration`

  Test against pre-production first; UITS recommends it, and one OIDC Integration Request form covers both environments. IU requires redirect URIs to be **HTTPS**, but localhost is allowed as long as it is `https://localhost:8000/...` served with a self-signed certificate (see the `USE_HTTPS=1` note below).

  Scopes requested are `openid profile email`, all of which are public scopes needing no Data Steward approval. `sub` is used as the per-user ownership key for everything stored in this app; profile/email claims are read from the ID token when present and otherwise fetched from the UserInfo endpoint (Shibboleth typically serves them there), with the UserInfo `sub` checked against the ID token's before being trusted.
- `CANVAS_BASE_URL`, `CANVAS_CLIENT_ID`, `CANVAS_CLIENT_SECRET` — from the Canvas Developer Key (UITS/Erik). Redirect URI is `http://localhost:8000/oauth/callback`. Note the Dev Key currently lives on the Canvas **Test** instance, not production — confirm the exact domain when you receive credentials.
- `CANVAS_OAUTH_SCOPES` — space-separated scopes, only needed if the Developer Key enforces scopes.
- `REALLM_API_KEY`, `REALLM_URL`, `REALLM_MODEL_NAME` — your existing ReaLLM setup.
- `GRADING_PASSES` — how many independent gradings to take of each submission (default 2). Where the passes agree, that is the score; where they differ, the score is their midpoint, and the row is pushed to the top of the review queue if the gap was big enough to change the grade — more than a tenth of that question's marks *and* more than 2% of the paper (`DISAGREEMENT_OF_QUESTION`, `DISAGREEMENT_OF_PAPER`). Smaller gaps are still shown beside the question but do not flag the row: two passes wobble by a point on almost every submission, and a queue where everything is flagged costs the same attention as one where nothing is. Two passes of the same model share its biases, so this does not fix calibration — what it gives is a *measured* uncertainty signal, which matters because the model's self-reported confidence proved close to worthless. Set to 1 to halve the cost — but not to halve the *time*: the passes are taken concurrently, so two cost about as long as one. Peak load stays bounded at `GRADING_CONCURRENCY × GRADING_PASSES` calls in flight.
- `OCR_MAX_PAGES` — the most pages that will be transcribed from one scanned submission (default 20). A submission over the limit is **refused, never graded in part**: the code used to transcribe the first 20 pages and stop, which produced a confident grade on an unknown fraction of the work with a note claiming every page had been read. If real submissions run longer than this, raise it rather than letting work go ungraded.
- `GRADING_CONCURRENCY` — how many submissions are graded at once (default 3). This is where a roster's time actually goes: one submission takes seconds to a few minutes, but a hundred of them queued behind each other take an afternoon, and nothing about them needs to be sequential. Measured on a stubbed roster of 12 submissions at one second each: 12.1s serially, 4.0s at 3 workers, 2.0s at 6 — it scales as linearly as you would hope, because the time is spent waiting on the gateway rather than computing anything. Raising it *does* raise the load on the vision gateway, and that is the point — each submission needs its own pages in flight to finish at its own speed. `OCR_MAX_IN_FLIGHT` is the ceiling on the total and scales with this number by default, so a handwritten submission is no slower for being graded alongside others.
- `OCR_BACKEND` — how to read handwriting and scans. `none` (default) refuses an image-only submission rather than grading it as blank. `vision` sends page images to a vision-capable model on the ReaLLM gateway — no new vendor and no new data path for student work, but no per-word confidence either. Probed against IU's gateway in September 2026: **`gemma-4-31B-it` and `granite-docling-258m` can see images; `gpt-oss-120b` and `glm-5.2` cannot** (glm-5.2 refuses as "not a multimodal model"). `gemma-4-31B-it` is the default because transcription depends on following an instruction prompt. `granite-docling-258m` is a 258M document-conversion model — far cheaper per page and a better fit for scanned *print* than handwriting — and answers in DocTags, which are stripped before the grader sees them. Set `OCR_VISION_MODEL` to transcribe with a different model from the one that grades. `azure` uses Azure AI Document Intelligence (`AZURE_DI_ENDPOINT`, `AZURE_DI_KEY`), which is purpose-built for handwriting and is the only backend reporting per-word confidence, but sends student work to a service UITS has not cleared. Pages are transcribed `OCR_CONCURRENCY` at a time **per submission** (default 4), under a ceiling of `OCR_MAX_IN_FLIGHT` calls to the gateway across the whole run — which defaults to `OCR_CONCURRENCY × GRADING_CONCURRENCY`, i.e. it throttles nothing and only exists to be lowered the day IU names a limit. Do not lower it to one submission's worth: that was shipped once, and sharing a single submission's page budget between three of them made every handwritten submission twice as slow while the batch as a whole looked faster. A total that does not scale with the number of submissions is not a budget, it is a queue. Per-submission concurrency matters more than it sounds: the gateway's latency has a long tail — on a real 11-page submission two pages took over 240s each while the rest were quick — so a serial run costs the *sum* of its worst pages, and a parallel one costs roughly its slowest. Raise it once IU confirms what concurrency the gateway allows. Run `scripts/probe_vision.py` first: it answers whether the gateway can see images at all, which is what decides between the two.
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

### Tests and evaluation

Everything under `scripts/` runs from the repository root.

| Script | Needs | What it does |
| --- | --- | --- |
| `test_extraction.py` | nothing (plus `reportlab` for the PDF checks) | Builds real .docx/.pdf/.txt/.ipynb files and confirms each is read *completely* — tables, text boxes, equations, hyperlinks, encodings — or reported as incomplete rather than graded. |
| `test_grading_guards.py` | nothing | The guards between a model's output and a student's grade: arithmetic recomputed from the breakdown, impossible scores rejected, rubric coverage enforced, retries on malformed responses and flaky API calls, reproducible request settings. |
| `test_review_integrity.py` | nothing | An approved grade always describes the work it is attached to: approvals expire when a student resubmits or the rubric changes, faculty edits are never destroyed, owner scoping holds, and an older database upgrades in place. |
| `test_grading_job.py` | nothing | Drives the real grading job through a real thread pool: every worker can reach the database (the context bug that once broke every multi-page submission), the vision gateway's budget holds *across* concurrent submissions, one failing submission is isolated to its own row, progress is counted exactly once, and a transcribed row is still forced to human review. Also the job's contracts: every extraction path returns the same shape (one returned four values where the caller unpacks five, so a link or on-paper submission raised `ValueError`), and calibration reads only the three anchors it will use rather than every approved submission. |
| `test_grading.py` | LLM key | Grades a rubric and a folder of submissions you point it at, and writes an `.xlsx`, so you can eyeball real output without Canvas. |
| `probe_vision.py` | LLM key | Answers "can the configured model see images?" — sends a generated image containing a random code and checks whether the model reads it back. A yes/no fact, not an opinion. |
| `check_labels.py` | nothing | Validates a label set you supply: sub-parts sum to the total, every submission has a verdict on every part, referenced files exist and extract, and the score distribution is not all extremes. |
| `make_pdfs.py` | `reportlab` | Renders an assignment source text into a PDF (`make_pdfs.py path/to/rubric.txt`). The text files are the source of truth; re-run after editing them. |
| `make_canvas_text.py` | nothing | Converts an assignment source file into the two forms Canvas accepts: plain text for the editor, and semantic HTML for its HTML view, which is the only way code blocks keep their indentation. |
| `benchmark_ocr.py` | LLM key | Times transcription of a real submission across model, resolution and concurrency, and prints the first page of each so a faster setting can be checked for whether it can still read the handwriting. |
| `make_deploy_zip.py` | nothing | Builds `app.zip` from an explicit list of what the app needs, so a folder zip cannot sweep up `.env`, the local database or `.venv`. Anything not named in the script does not ship. |
| `eval_grading.py` | LLM key | **Measures** the grader: mean error against known scores, run-to-run spread, and bias probes for writing fluency and answer length. |

`eval_grading.py` is the only thing here that answers "how accurate is the
grading?", so run it before and after any change to the prompt or the model:

```bash
python3 scripts/check_labels.py my_labels.json                       # validate first
python3 scripts/eval_grading.py --labels my_labels.json
python3 scripts/eval_grading.py --labels my_labels.json --repeats 3  # run-to-run spread
python3 scripts/eval_grading.py --labels my_labels.json --passes 2   # as the app grades
```

**No label sets ship with the repository.** A corpus of sample assignments and
graded submissions was built and then removed once it had served its purpose; it
is recoverable from this branch's git history if it is ever wanted again. What
that means in practice is that **the grader's accuracy is currently unmeasured**,
and no change to the prompt can be shown to help or hurt until a label set
exists. The guard suites below still pass without one, but they check that the
app cannot produce an impossible grade -- not that the grades are any good.

To measure it, write a label set against real submissions. `check_labels.py`
documents and enforces the format: each rubric question is split into weighted
sub-parts, each submission carries a verdict on every sub-part (1, 0.5 or 0), and
the expected score is the weighted sum -- so a disagreement is traceable to a
specific requirement rather than to a number somebody guessed. Labels written
from the scores a TA actually gave are worth far more than estimates, and two TAs
grading independently also tells you what human-to-human agreement looks like.
That agreement rate is the realistic ceiling; two experienced TAs do not agree
with each other 100% of the time either.

### How it works

Two separate logins, in order:

1. **IU Login** (`/auth/iu/login`) — the app's front door. Redirects to IU's identity provider (Duo included); this app never sees a password, only a signed ID token confirming who the user is. This is what the IU Login team needs to register — see "What to give the IU Login team" below.
2. **Connect Canvas** (`/connect-canvas` → `/canvas/connect`) — a one-time step per user, right after their first IU login. Canvas's own OAuth2 flow hands back an access token scoped to that person's own Canvas role/courses. This token (not the IU login) is what actually authorizes every Canvas API call, and is what UITS's Developer Key credentials are for.

Then:

3. **Courses/assignments** are pulled live from the Canvas API (read-only) using the connected Canvas token.
4. **Rubric upload** accepts PDF/DOCX/TXT, extracts the text, and stores it per assignment. A rubric built in Canvas is read straight off the assignment payload and combined with the uploaded answer key — including each criterion's *longer description*, which is where the actual requirement usually lives (the visible name is often just "Step 1").

   **All-or-nothing criteria.** When a Canvas criterion has exactly two levels — Full Marks / No Marks — the award is snapped back onto that scale. This is not tidiness: two grading passes that split on such a criterion average to half marks, which is the one answer the faculty member said is impossible, and "half of removed the duplicate rows" is not a grade anyone can defend. A tie goes to the lower value, because the row is flagged for review either way and an unchecked inflated grade is the worse failure. Rubrics with three or more levels are left alone — a score between "Proficient" and "Developing" is a real judgment, and Canvas accepts it. Whenever an award is moved, the review page shows what the grader actually judged before the rounding.

   **Extraction** (`app/extract.py`) reads `.pdf`, `.docx`, `.txt`, `.ipynb` and `.py`, and its second job is knowing when it missed something. A `.docx` is walked in document order so paragraphs, tables (nested included), text boxes, equations, hyperlinks and content controls all come through where they belong — a student who answers inside a table used to extract as their name alone and score a confident zero. `.pdf` uses pdfplumber for reading order and columns. `.txt` detects its encoding rather than assuming UTF-8, so a cp1252 or UTF-16 file is not read as gibberish.

   **Notebooks** (`.ipynb`) are read as an ordered transcript of cells rather than a wall of code, because for a programming assignment the source alone does not answer the question being asked. A cell that was never run looks identical to a working one if you only read it, so the transcript carries the execution count, the output, and the traceback, and labels each: `ran as In[3]`, `NEVER RUN -- no output`, `--- ERROR ---`. The grader is told these labels are the tool's, not the student's. Runaway outputs are truncated so one `print(df)` cannot crowd the remaining questions out of the prompt. Figures cannot be read, but the fact that a figure was produced is recorded — enough to credit "the code draws a heatmap and a heatmap came out" without anyone pretending to have looked at the picture. Anything else is refused by name with advice the student can act on ("Save As a .docx and resubmit"), never a stack trace.

   **Handwriting and scans** (`app/ocr.py`) are off by default and switched on with `OCR_BACKEND`. A photographed page, or a PDF with no text layer, is rasterised at 200 dpi and transcribed page by page; the transcript keeps the reader's own uncertainty rather than a tidy guess (`[illegible]`, `[?guess]`, `[struck: ...]`, `[diagram: ...]`), and the grading prompt is told what each marker means, to read a guess charitably, and never to penalise what may be the transcriber's failure rather than the student's.

   Two things about this are worth knowing before turning it on. First, **the evidence check goes circular**: every award quotes the student's words and the app verifies the quote appears in the submission, but against a transcript it is verifying the quote against the machine's own reading — so a misreading that costs a student marks is confirmed rather than caught. Nothing in the code can fix that, so instead a transcribed row is forced to low confidence, pushed to the top of the review queue, and labelled in red on the review page telling the faculty member to compare with the original. Second, **a language model asked to transcribe will silently correct**: it fixes the spelling, tidies the grammar and repairs the broken formula, handing marks to a student who did not earn them, invisibly. The transcription prompt fights this explicitly and a test asserts a misspelling survives the round trip.

   Critically, extraction distinguishes **"the student wrote very little"** (grade it — it should score accordingly) from **"we failed to read the file"** (refuse it). A document holding images or diagrams but almost no text is not graded at all, because an invented zero on correct work is the worst thing this pipeline can produce and it looks entirely plausible. Softer warnings — images alongside readable text, an odd encoding, a table whose order may be off — are attached to the row, shown in the review queue, carried into the Excel export, and force the row to the top of the attention queue.
5. **Grading** runs in a background thread (started when you click "Grade all submissions," not blocking the request) — downloads each submission (file upload or text-entry, all attachments if there's more than one), sends rubric + submission text to the LLM with a strict grading prompt, and stores each result (score, per-question breakdown, feedback) as soon as it's ready. The review page shows live progress and refreshes automatically until grading finishes.
6. **Review** lets faculty see the AI's score/feedback per student, override either field, and mark it approved. If the AI's computed total doesn't match the assignment's actual Canvas points total, a warning banner flags it before anything gets entered into Canvas. Three things shape this page:
   - **Evidence.** Every per-question award quotes the student's own words, and the app checks that quote is really in the submission. A quote it can't find is shown in red — that's the one place a fabricated award becomes visible.
   - **Confidence.** The grader reports high/medium/low per question; the row takes the weakest, and an unverifiable quote forces it to low.
   - **Triage order.** The queue leads with failed rows, then reviews the work has outrun, then low-confidence judgments — so faculty attention goes where it changes an outcome. Sort by name instead with the link above the table.
7. **Calibration.** Once some grades are approved, later runs on the same assignment include a spread of those instructor-graded submissions (lowest, middle, highest) as worked examples, so the model matches *that* instructor's strictness rather than a generic reading of the rubric. A first run has none and behaves exactly as before. Only approved, non-stale rows with a human score count — an AI score nobody confirmed would just teach the model to agree with itself.
8. **Export** builds a formatted `.xlsx` (AI score, final score, percentage, approval status, feedback) for manual entry into the Canvas gradebook — the Developer Key is read-only, so there is no automated gradebook write-back.

### What to give the IU Login team

Registration goes through the **OIDC Integration Request form**. One submission covers both pre-production and production — choose "Pre-production & production" under Environments.

Redirect URIs to register:
- Deployed: `https://<your-deployed-domain>/auth/iu/callback`
- Local development: `https://localhost:8000/auth/iu/callback` (must be `https`; run with `USE_HTTPS=1`)

The form also asks for a **Service Owner** — per the IU Login team, this is simply the IU-side contact reachable for maintenance or service-impacting changes — and a **Service Provider**, the technical contact (whoever maintains this app).

You choose the client ID yourself on the form. They send back the client secret securely; the discovery URL is the fixed Shibboleth endpoint listed above. Drop those into `.env` (or Azure Application Settings) and the login flow works with no code changes.

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
