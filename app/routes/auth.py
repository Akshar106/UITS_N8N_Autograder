from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from app import canvas_client, storage
from app.auth_utils import compute_expiry

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/")
def index():
    if session.get("canvas_user_id"):
        return redirect(url_for("dashboard.courses"))
    return render_template("login.html")


@auth_bp.route("/login")
def login():
    cfg = current_app.config
    if not cfg["CANVAS_CLIENT_ID"] or not cfg["CANVAS_CLIENT_SECRET"] or not cfg["CANVAS_BASE_URL"]:
        return render_template(
            "login.html",
            error="Canvas is not configured yet: set CANVAS_BASE_URL, CANVAS_CLIENT_ID and "
                  "CANVAS_CLIENT_SECRET in .env (values come from the UITS Developer Key).",
        ), 503
    state = canvas_client.generate_state()
    session["oauth_state"] = state
    return redirect(canvas_client.build_authorize_url(state))


@auth_bp.route("/oauth/callback")
def oauth_callback():
    error = request.args.get("error")
    if error:
        return render_template("login.html", error=f"Canvas denied authorization: {error}")

    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        return render_template("login.html", error="Login request expired or was tampered with. Please try again."), 400
    session.pop("oauth_state", None)

    code = request.args.get("code")
    if not code:
        return render_template("login.html", error="Canvas did not return an authorization code."), 400

    try:
        token_response = canvas_client.exchange_code_for_token(code)
    except canvas_client.CanvasAPIError as exc:
        return render_template("login.html", error=f"Could not complete Canvas login: {exc}"), 502

    user = token_response.get("user") or {}
    if not user.get("id"):
        return render_template("login.html", error="Canvas login succeeded but returned no user identity."), 502
    canvas_user_id = str(user["id"])
    canvas_user_name = user.get("name", "")

    storage.save_token(
        canvas_user_id,
        canvas_user_name,
        token_response["access_token"],
        token_response.get("refresh_token"),
        compute_expiry(token_response.get("expires_in")),
    )

    session["canvas_user_id"] = canvas_user_id
    session["canvas_user_name"] = canvas_user_name
    return redirect(url_for("dashboard.courses"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.index"))
