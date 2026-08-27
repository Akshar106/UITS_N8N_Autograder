import requests
from authlib.integrations.base_client.errors import OAuthError
from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from app import canvas_client, storage
from app.auth_utils import compute_expiry, login_required
from app.iu_oidc import is_configured as iu_configured, oauth

auth_bp = Blueprint("auth", __name__)

# Raised by Authlib/requests when the IU Login discovery URL or token endpoint
# is unreachable or returns something unparseable -- distinct from OAuthError,
# which covers the OAuth protocol-level failures (bad state, denied consent).
IU_OIDC_NETWORK_ERRORS = (requests.exceptions.RequestException, ValueError)


# --- IU Login (front door) ----------------------------------------------------

@auth_bp.route("/")
def index():
    if session.get("iu_user_id"):
        if storage.get_canvas_token(session["iu_user_id"]):
            return redirect(url_for("dashboard.courses"))
        return redirect(url_for("auth.connect_canvas"))
    return render_template("login.html")


@auth_bp.route("/auth/iu/login", methods=["GET", "POST"])
def iu_login():
    # POST-only for the actual redirect. Search crawlers issue GETs, so a GET
    # endpoint that immediately bounces to a university login page reads to
    # automated phishing classifiers exactly like a credential-harvesting
    # middleman -- which is what got the deployed URL flagged. Requiring a form
    # submission keeps crawlers from ever reaching the redirect, and starting a
    # login is a state-changing action that warrants a POST anyway.
    if request.method == "GET":
        return redirect(url_for("auth.index"))

    if not iu_configured():
        return render_template(
            "login.html",
            error="IU Login is not configured yet: set IU_OIDC_DISCOVERY_URL, IU_OIDC_CLIENT_ID "
                  "and IU_OIDC_CLIENT_SECRET in .env (values come from the IU Login team).",
        ), 503
    try:
        return oauth.iu.authorize_redirect(current_app.config["IU_OIDC_REDIRECT_URI"])
    except IU_OIDC_NETWORK_ERRORS as exc:
        return render_template(
            "login.html",
            error=f"Could not reach IU Login right now ({exc}). Please try again shortly.",
        ), 502


@auth_bp.route("/auth/iu/callback")
def iu_callback():
    if not iu_configured():
        return render_template("login.html", error="IU Login is not configured."), 503

    try:
        token = oauth.iu.authorize_access_token()
    except OAuthError as exc:
        # Log the full error server-side: the user-facing text is deliberately
        # short, but diagnosing a failed token exchange needs IU's actual
        # error code (invalid_client, invalid_grant, ...) and description.
        current_app.logger.warning(
            "IU Login token exchange failed: error=%r description=%r uri=%r",
            getattr(exc, "error", None), getattr(exc, "description", None),
            getattr(exc, "uri", None),
        )
        return render_template("login.html", error=f"IU Login failed: {exc.description or exc.error}"), 400
    except IU_OIDC_NETWORK_ERRORS as exc:
        return render_template(
            "login.html",
            error=f"Could not reach IU Login right now ({exc}). Please try again shortly.",
        ), 502

    # Authlib fills token["userinfo"] from the signed ID token only. IU Login runs
    # on Shibboleth, which generally puts just the subject in the ID token and
    # serves the profile/email claims from the UserInfo endpoint, so those have to
    # be fetched separately or the UI ends up showing an opaque subject id.
    id_claims = dict(token.get("userinfo") or {})
    iu_user_id = id_claims.get("sub")
    if not iu_user_id:
        return render_template("login.html", error="IU Login succeeded but returned no user identity."), 502

    claims = id_claims
    if not id_claims.get("name") or not id_claims.get("email"):
        try:
            fetched = dict(oauth.iu.userinfo(token=token) or {})
        except (OAuthError, *IU_OIDC_NETWORK_ERRORS):
            fetched = {}  # non-fatal: the ID token already gave us a usable identity
        # OIDC Core 5.3.2: the UserInfo subject must match the ID token's subject,
        # otherwise the response belongs to someone else and must not be used.
        if fetched.get("sub") == iu_user_id:
            signed = {k: v for k, v in id_claims.items() if v not in (None, "")}
            claims = {**fetched, **signed}

    session.clear()
    session["iu_user_id"] = iu_user_id
    session["iu_name"] = claims.get("name") or claims.get("preferred_username") or iu_user_id
    session["iu_email"] = claims.get("email", "")

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

    # IU Login authenticates every IU account, students included, so this is the
    # gate that keeps the tool to instructors: Canvas must say the user holds an
    # instructor role somewhere. Checked before the token is stored, so a
    # non-instructor never ends up with a usable Canvas link at all.
    try:
        is_instructor = canvas_client.has_instructor_role(
            token_response["access_token"], current_app.config["INSTRUCTOR_ENROLLMENT_TYPES"]
        )
    except canvas_client.CanvasAPIError as exc:
        return render_template("connect_canvas.html", already_connected=False,
                               error=f"Could not verify your Canvas role: {exc}"), 502

    if not is_instructor:
        return render_template(
            "connect_canvas.html",
            already_connected=False,
            error="This tool is available to course instructors. Your Canvas account does not "
                  "have an instructor role in any course, so it cannot be connected. If you "
                  "believe this is a mistake, contact the project maintainer.",
        ), 403

    storage.save_canvas_token(
        session["iu_user_id"],
        str(user["id"]),
        user.get("name", ""),
        token_response["access_token"],
        token_response.get("refresh_token"),
        compute_expiry(token_response.get("expires_in")),
    )
    return redirect(url_for("dashboard.courses"))
