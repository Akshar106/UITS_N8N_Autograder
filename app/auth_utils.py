from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import redirect, session, url_for

from app import canvas_client, storage


class AuthError(Exception):
    """No valid IU Login session; user must log in again."""


class CanvasNotConnected(Exception):
    """IU session is fine but there is no usable Canvas token; user must (re)connect Canvas."""


def login_required(view):
    """Requires an IU Login session (the app's front door).

    NOTE: IU Login authenticates EVERY IU account, students included -- it says
    who someone is, not that they teach anything. Any route that touches course
    or grade data must use @canvas_required instead, and must scope its queries
    to current_user_id().
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("iu_user_id"):
            return redirect(url_for("auth.index"))
        return view(*args, **kwargs)

    return wrapped


def canvas_required(view):
    """Requires an IU session AND a linked Canvas account.

    The Canvas token is what proves the user is an instructor somewhere: it is
    issued by Canvas against their own role and is the only thing standing
    between a route and any IU account holder.
    """
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("iu_user_id"):
            return redirect(url_for("auth.index"))
        if not storage.get_canvas_token(session["iu_user_id"]):
            raise CanvasNotConnected("Connect your Canvas account to continue.")
        return view(*args, **kwargs)

    return wrapped


def current_user_id():
    """The IU Login subject of the signed-in user; the ownership key for all stored data."""
    return session.get("iu_user_id")


def get_valid_access_token():
    """Returns a usable Canvas access token for the logged-in IU user, refreshing if needed.

    Raises AuthError when there is no IU session, and CanvasNotConnected when the
    user hasn't linked Canvas yet (or the link can't be renewed) -- app-level
    handlers turn those into redirects to the login / connect-Canvas pages.
    """
    iu_user_id = session.get("iu_user_id")
    if not iu_user_id:
        raise AuthError("Not logged in.")
    return access_token_for(iu_user_id)


def access_token_for(iu_user_id):
    """The same thing without a request session.

    The background grading job runs for as long as the roster takes -- well past
    the hour a Canvas token lasts -- and it has no session to read. Capturing one
    token at the start means every submission after the expiry fails to download.
    This lets the job ask for a fresh one per submission instead.
    """
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
