import os
import secrets

import requests
from flask import current_app

TOKEN_PATH = "/login/oauth2/token"
AUTH_PATH = "/login/oauth2/auth"


class CanvasAPIError(Exception):
    pass


def _request(method, url, **kwargs):
    """requests wrapper that turns network-level failures into CanvasAPIError.

    Without this, a DNS failure, refused connection, TLS error, proxy error or
    read timeout raises a bare requests exception that no error handler catches,
    and the user gets a raw 500 instead of the friendly Canvas-unavailable page.
    """
    try:
        return requests.request(method, url, **kwargs)
    except requests.RequestException as exc:
        raise CanvasAPIError(f"could not reach Canvas ({type(exc).__name__}): {exc}") from exc


def _base_url():
    base = current_app.config["CANVAS_BASE_URL"]
    if not base:
        raise CanvasAPIError("CANVAS_BASE_URL is not configured")
    return base


def generate_state():
    return secrets.token_urlsafe(32)


def build_authorize_url(state):
    cfg = current_app.config
    params = {
        "client_id": cfg["CANVAS_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": cfg["CANVAS_REDIRECT_URI"],
        "state": state,
    }
    if cfg["CANVAS_OAUTH_SCOPES"]:
        params["scope"] = " ".join(cfg["CANVAS_OAUTH_SCOPES"])
    req = requests.Request("GET", f"{_base_url()}{AUTH_PATH}", params=params)
    return req.prepare().url


def exchange_code_for_token(code):
    cfg = current_app.config
    resp = _request(
        "POST",
        f"{_base_url()}{TOKEN_PATH}",
        data={
            "grant_type": "authorization_code",
            "client_id": cfg["CANVAS_CLIENT_ID"],
            "client_secret": cfg["CANVAS_CLIENT_SECRET"],
            "redirect_uri": cfg["CANVAS_REDIRECT_URI"],
            "code": code,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise CanvasAPIError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def refresh_access_token(refresh_token):
    cfg = current_app.config
    resp = _request(
        "POST",
        f"{_base_url()}{TOKEN_PATH}",
        data={
            "grant_type": "refresh_token",
            "client_id": cfg["CANVAS_CLIENT_ID"],
            "client_secret": cfg["CANVAS_CLIENT_SECRET"],
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise CanvasAPIError(f"Token refresh failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _auth_headers(access_token):
    return {"Authorization": f"Bearer {access_token}"}


def _get_paginated(access_token, path, params=None):
    """Follow Canvas's Link-header pagination and return the combined list."""
    url = f"{_base_url()}{path}"
    items = []
    while url:
        resp = _request("GET", url, headers=_auth_headers(access_token), params=params, timeout=20)
        if resp.status_code != 200:
            raise CanvasAPIError(f"GET {path} failed ({resp.status_code}): {resp.text}")
        items.extend(resp.json())
        params = None  # only needed on the first request; `next` link carries the rest
        url = resp.links.get("next", {}).get("url")
    return items


def list_instructor_courses(access_token, enrollment_types):
    """Courses where the user holds one of the given instructor-ish roles.

    Canvas accepts a single enrollment_type per request, so query each and merge.
    Canvas itself is the authority on who teaches what -- this is why the app can
    tell an instructor from a student without asking IU for restricted
    affiliation attributes.

    Bloomington-only access is expected to be enforced by UITS at the Developer
    Key / account level, not filtered client-side here.
    """
    by_id = {}
    for enrollment_type in enrollment_types:
        for course in _get_paginated(
            access_token,
            "/api/v1/courses",
            params={"enrollment_type": enrollment_type, "per_page": 100, "include[]": "term"},
        ):
            by_id.setdefault(course["id"], course)
    return list(by_id.values())


def has_instructor_role(access_token, enrollment_types):
    """True if the user holds an instructor role in at least one course."""
    for enrollment_type in enrollment_types:
        courses = _get_paginated(
            access_token,
            "/api/v1/courses",
            params={"enrollment_type": enrollment_type, "per_page": 1},
        )
        if courses:
            return True
    return False


def list_assignments(access_token, course_id):
    return _get_paginated(access_token, f"/api/v1/courses/{course_id}/assignments", params={"per_page": 100})


def get_assignment(access_token, course_id, assignment_id):
    resp = _request(
        "GET",
        f"{_base_url()}/api/v1/courses/{course_id}/assignments/{assignment_id}",
        headers=_auth_headers(access_token),
        timeout=15,
    )
    if resp.status_code != 200:
        raise CanvasAPIError(f"Failed to fetch assignment ({resp.status_code}): {resp.text}")
    return resp.json()


def get_course(access_token, course_id):
    resp = _request(
        "GET",
        f"{_base_url()}/api/v1/courses/{course_id}",
        headers=_auth_headers(access_token),
        timeout=15,
    )
    if resp.status_code != 200:
        raise CanvasAPIError(f"Failed to fetch course ({resp.status_code}): {resp.text}")
    return resp.json()


def list_submissions(access_token, course_id, assignment_id):
    return _get_paginated(
        access_token,
        f"/api/v1/courses/{course_id}/assignments/{assignment_id}/submissions",
        params={"include[]": "user", "per_page": 100},
    )


# A submission is a document, and this instance has 1.75 GB of RAM shared by
# every request. `resp.content` buffers the whole body, so one student attaching
# a video takes the app down for the entire roster. The upload limit the app
# enforces on its own forms does not apply to anything fetched from Canvas.
MAX_ATTACHMENT_BYTES = int(os.getenv("MAX_ATTACHMENT_MB", "25")) * 1024 * 1024


def _read_capped(resp):
    """Read a response body, refusing anything over the cap."""
    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_ATTACHMENT_BYTES:
        raise CanvasAPIError(
            f"Submission file is {int(declared) // (1024 * 1024)} MB, over the "
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit; it was not downloaded."
        )
    chunks, total = [], 0
    for chunk in resp.iter_content(64 * 1024):
        total += len(chunk)
        if total > MAX_ATTACHMENT_BYTES:
            resp.close()
            raise CanvasAPIError(
                f"Submission file exceeds the "
                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit; it was not downloaded."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def download_attachment(url, access_token=None):
    """Fetch a submission attachment, unauthenticated first.

    Canvas signs attachment URLs with a `verifier` query parameter, so the plain
    URL is already authorized and needs no Authorization header. Sending one
    anyway is actively harmful with a scoped Developer Key: file downloads live
    at /files/:id/download, which is outside the /api/v1 scopes granted to this
    key, and Canvas rejects an out-of-scope bearer token with 401 -- turning a
    request that would have succeeded on the verifier alone into a failure.

    The bearer token is kept only as a fallback, for instances or files served
    without a verifier.
    """
    resp = _request("GET", url, timeout=30, stream=True)
    if resp.status_code == 200:
        return _read_capped(resp)

    if access_token and resp.status_code in (401, 403):
        retried = _request("GET", url, headers=_auth_headers(access_token),
                           timeout=30, stream=True)
        if retried.status_code == 200:
            return _read_capped(retried)
        raise CanvasAPIError(
            f"Failed to download submission file "
            f"({resp.status_code} unauthenticated, {retried.status_code} with token)"
        )

    raise CanvasAPIError(f"Failed to download submission file ({resp.status_code})")
