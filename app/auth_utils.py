from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import redirect, session, url_for

from app import canvas_client, storage


class AuthError(Exception):
    """Session exists but no usable Canvas token; user must log in again."""


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("canvas_user_id"):
            return redirect(url_for("auth.index"))
        return view(*args, **kwargs)

    return wrapped


def get_valid_access_token():
    """Returns a usable Canvas access token for the logged-in user, refreshing if needed.

    Raises AuthError when there is no stored token or the refresh fails —
    the app-level error handler clears the session and sends the user back
    to the login page.
    """
    canvas_user_id = session.get("canvas_user_id")
    if not canvas_user_id:
        raise AuthError("Not logged in.")

    token = storage.get_token(canvas_user_id)
    if not token:
        raise AuthError("Your Canvas session is no longer valid. Please log in again.")

    if token["expires_at"]:
        expires_at = datetime.fromisoformat(token["expires_at"])
        if datetime.now(timezone.utc) >= expires_at and token["refresh_token"]:
            try:
                refreshed = canvas_client.refresh_access_token(token["refresh_token"])
            except canvas_client.CanvasAPIError as exc:
                raise AuthError("Your Canvas session expired and could not be renewed. Please log in again.") from exc
            expires_at_new = compute_expiry(refreshed.get("expires_in"))
            storage.save_token(
                canvas_user_id,
                token["canvas_user_name"],
                refreshed["access_token"],
                refreshed.get("refresh_token", token["refresh_token"]),
                expires_at_new,
            )
            return refreshed["access_token"]

    return token["access_token"]


def compute_expiry(expires_in):
    if not expires_in:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
