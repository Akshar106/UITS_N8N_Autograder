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


def list_teaching_courses(access_token):
    """Courses where the authenticated user is a teacher.

    Bloomington-only access is expected to be enforced by UITS at the Developer
    Key / account level, not filtered client-side here.
    """
    return _get_paginated(
        access_token,
        "/api/v1/courses",
        params={"enrollment_type": "teacher", "per_page": 100, "include[]": "term"},
    )


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


def download_attachment(url, access_token=None):
    # Submission attachment URLs usually embed a verifier token, but pass the
    # bearer token too for instances/files where the verifier is absent.
    headers = _auth_headers(access_token) if access_token else {}
    resp = _request("GET", url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise CanvasAPIError(f"Failed to download submission file ({resp.status_code})")
    return resp.content
