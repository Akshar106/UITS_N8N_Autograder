from authlib.integrations.base_client.errors import OAuthError
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from app import canvas_client, storage
from app.auth_utils import compute_expiry, login_required
from app.iu_oidc import is_configured as iu_configured, oauth

auth_bp = Blueprint("auth", __name__)


# --- IU Login (front door) ----------------------------------------------------

@auth_bp.route("/")
def index():
    if session.get("iu_user_id"):
        if storage.get_canvas_token(session["iu_user_id"]):
            return redirect(url_for("dashboard.courses"))
        return redirect(url_for("auth.connect_canvas"))
    return render_template("login.html")


@auth_bp.route("/auth/iu/login")
def iu_login():
    if not iu_configured():
        return render_template(
            "login.html",
            error="IU Login is not configured yet: set IU_OIDC_DISCOVERY_URL, IU_OIDC_CLIENT_ID "
                  "and IU_OIDC_CLIENT_SECRET in .env (values come from the IU Login team).",
        ), 503
    return oauth.iu.authorize_redirect(current_app.config["IU_OIDC_REDIRECT_URI"])


@auth_bp.route("/auth/iu/callback")
def iu_callback():
    if not iu_configured():
        return render_template("login.html", error="IU Login is not configured."), 503

    try:
        token = oauth.iu.authorize_access_token()
    except OAuthError as exc:
        return render_template("login.html", error=f"IU Login failed: {exc.description or exc.error}"), 400

    userinfo = token.get("userinfo") or {}
    iu_user_id = userinfo.get("sub")
    if not iu_user_id:
        return render_template("login.html", error="IU Login succeeded but returned no user identity."), 502

    session.clear()
    session["iu_user_id"] = iu_user_id
    session["iu_name"] = userinfo.get("name") or userinfo.get("preferred_username") or iu_user_id
    session["iu_email"] = userinfo.get("email", "")

    if storage.get_canvas_token(iu_user_id):
        return redirect(url_for("dashboard.courses"))
    return redirect(url_for("auth.connect_canvas"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.index"))


# --- Canvas connection (after IU login) ----------------------------------------

@auth_bp.route("/connect-canvas")
@login_required
def connect_canvas():
    already_connected = storage.get_canvas_token(session["iu_user_id"]) is not None
    return render_template("connect_canvas.html", already_connected=already_connected)


@auth_bp.route("/canvas/connect")
@login_required
def canvas_connect():
    cfg = current_app.config
    if not cfg["CANVAS_CLIENT_ID"] or not cfg["CANVAS_CLIENT_SECRET"] or not cfg["CANVAS_BASE_URL"]:
        return render_template(
            "connect_canvas.html",
            already_connected=False,
            error="Canvas is not configured yet: set CANVAS_BASE_URL, CANVAS_CLIENT_ID and "
                  "CANVAS_CLIENT_SECRET in .env (values come from the UITS Developer Key).",
        ), 503
    state = canvas_client.generate_state()
    session["canvas_oauth_state"] = state
    return redirect(canvas_client.build_authorize_url(state))


@auth_bp.route("/oauth/callback")
@login_required
def oauth_callback():
    error = request.args.get("error")
    if error:
        return render_template("connect_canvas.html", already_connected=False,
                               error=f"Canvas denied authorization: {error}")

    state = request.args.get("state")
    if not state or state != session.get("canvas_oauth_state"):
        return render_template("connect_canvas.html", already_connected=False,
                               error="Connection request expired or was tampered with. Please try again."), 400
    session.pop("canvas_oauth_state", None)

    code = request.args.get("code")
    if not code:
        return render_template("connect_canvas.html", already_connected=False,
                               error="Canvas did not return an authorization code."), 400

    try:
        token_response = canvas_client.exchange_code_for_token(code)
    except canvas_client.CanvasAPIError as exc:
        return render_template("connect_canvas.html", already_connected=False,
                               error=f"Could not complete the Canvas connection: {exc}"), 502

    user = token_response.get("user") or {}
    if not user.get("id"):
        return render_template("connect_canvas.html", already_connected=False,
                               error="Canvas connection succeeded but returned no user identity."), 502

    storage.save_canvas_token(
        session["iu_user_id"],
        str(user["id"]),
        user.get("name", ""),
        token_response["access_token"],
        token_response.get("refresh_token"),
        compute_expiry(token_response.get("expires_in")),
    )
    return redirect(url_for("dashboard.courses"))
