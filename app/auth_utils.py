from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import redirect, session, url_for

from app import canvas_client, storage


class AuthError(Exception):
    """No valid IU Login session; user must log in again."""


class CanvasNotConnected(Exception):
    """IU session is fine but there is no usable Canvas token; user must (re)connect Canvas."""


def login_required(view):
    """Requires an IU Login session (the app's front door)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("iu_user_id"):
            return redirect(url_for("auth.index"))
        return view(*args, **kwargs)

    return wrapped


def get_valid_access_token():
    """Returns a usable Canvas access token for the logged-in IU user, refreshing if needed.

    Raises AuthError when there is no IU session, and CanvasNotConnected when the
    user hasn't linked Canvas yet (or the link can't be renewed) -- app-level
    handlers turn those into redirects to the login / connect-Canvas pages.
    """
    iu_user_id = session.get("iu_user_id")
    if not iu_user_id:
        raise AuthError("Not logged in.")

    token = storage.get_canvas_token(iu_user_id)
    if not token:
        raise CanvasNotConnected("Connect your Canvas account to continue.")

    if token["expires_at"]:
        expires_at = datetime.fromisoformat(token["expires_at"])
        if datetime.now(timezone.utc) >= expires_at and token["refresh_token"]:
            try:
                refreshed = canvas_client.refresh_access_token(token["refresh_token"])
            except canvas_client.CanvasAPIError as exc:
                storage.delete_canvas_token(iu_user_id)
                raise CanvasNotConnected(
                    "Your Canvas connection expired and could not be renewed. Please connect Canvas again."
                ) from exc
            expires_at_new = compute_expiry(refreshed.get("expires_in"))
            storage.save_canvas_token(
                iu_user_id,
                token["canvas_user_id"],
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
