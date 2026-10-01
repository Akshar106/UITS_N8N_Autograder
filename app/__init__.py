import logging
import os

from flask import Flask, flash, redirect, render_template, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from app import grading
from app.config import Config
from app.storage import close_db, init_db

csrf = CSRFProtect()

logger = logging.getLogger(__name__)


def _register_google_site_verification(app):
    """Serve Google Search Console's googleXXXX.html verification file.

    Search Console verifies ownership by fetching that file from the site root,
    but Flask serves nothing outside /static, so without this route the check
    404s and the domain cannot be verified -- which also blocks requesting a
    Safe Browsing review. Drop the file Google gives you in the project root.
    """
    project_root = os.path.dirname(app.root_path)
    try:
        names = [
            n for n in os.listdir(project_root)
            if n.startswith("google") and n.endswith(".html")
        ]
    except OSError:
        return

    for name in names:
        try:
            with open(os.path.join(project_root, name), encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            continue

        app.add_url_rule(
            f"/{name}",
            endpoint=f"google_verify_{name}",
            view_func=(lambda body=body: (body, 200, {"Content-Type": "text/html; charset=utf-8"})),
        )
        logger.info("Serving Google site verification file at /%s", name)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Azure (and most PaaS) terminates TLS at a proxy; trust its forwarding
    # headers so url_for builds https:// redirect URIs.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    csrf.init_app(app)

    @app.after_request
    def set_security_headers(response):
        # IU's own integration requirements state the login flow must not be
        # renderable inside an iframe, so users can always see the real IU Login
        # URL and verify they are not on a phishing site. DENY enforces that.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # The app serves only its own CSS and has no inline/third-party scripts.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'none'; style-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        if app.config["APP_ENV"] == "production":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    os.makedirs(os.path.dirname(app.config["DATABASE_PATH"]) or ".", exist_ok=True)
    init_db(app)
    app.teardown_appcontext(close_db)

    from app.iu_oidc import init_oidc
    init_oidc(app)

    from app.auth_utils import AuthError, CanvasNotConnected
    from app.canvas_client import CanvasAPIError

    @app.errorhandler(AuthError)
    def handle_auth_error(exc):
        session.clear()
        flash(str(exc), "error")
        return redirect(url_for("auth.index"))

    @app.errorhandler(CanvasNotConnected)
    def handle_canvas_not_connected(exc):
        flash(str(exc), "error")
        return redirect(url_for("auth.connect_canvas"))

    @app.errorhandler(CanvasAPIError)
    def handle_canvas_error(exc):
        # The exception text can contain a raw upstream response body; log it for
        # debugging but keep it out of the page, which a student or an attacker
        # probing the app could otherwise read.
        logger.warning("Canvas API error: %s", exc)
        return render_template(
            "error.html",
            message="Canvas request failed. This is usually temporary; please try again "
                    "in a moment. If it keeps happening, contact the project maintainer.",
        ), 502

    @app.errorhandler(CSRFError)
    def handle_csrf_error(exc):
        flash("Your form session expired. Please try again.", "error")
        return redirect(url_for("auth.index"))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_too_large(exc):
        limit_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_template(
            "error.html", message=f"That file is too large. The maximum upload size is {limit_mb} MB."
        ), 413

    @app.route("/healthz")
    def healthz():
        """Liveness, plus enough configuration to tell what is actually running.

        Diagnosing this deployment has repeatedly meant guessing whether a
        change had shipped and whether a setting had taken effect. Nothing here
        is a secret -- the two keys report only WHETHER they are set, never any
        part of their value -- and all of it is visible to an operator in one
        request instead of an SSH session.
        """
        from app import extract, ocr
        from app.routes import rubric

        try:
            from app._version import VERSION
        except ImportError:
            VERSION = "dev"  # running from a checkout, not a packaged build

        return {
            "status": "ok",
            "version": VERSION,
            "app_env": app.config["APP_ENV"],
            "secret_key_set": bool(os.getenv("FLASK_SECRET_KEY")),
            "canvas_configured": bool(app.config["CANVAS_CLIENT_ID"]),
            "iu_login_configured": bool(app.config["IU_OIDC_CLIENT_ID"]),
            "llm_configured": bool(os.getenv("REALLM_API_KEY")),
            "llm_model": os.getenv("REALLM_MODEL_NAME"),
            "grading_passes": grading.GRADING_PASSES,
            "grading_concurrency": rubric.GRADING_CONCURRENCY,
            "ocr_backend": ocr.backend_name(),
            "ocr_enabled": ocr.is_enabled(),
            "ocr_vision_model": ocr.VISION_MODEL if ocr.is_enabled() else None,
            "ocr_concurrency": ocr.CONCURRENCY,
            "accepted_uploads": list(extract.supported_extensions()),
            "database_path": app.config["DATABASE_PATH"],
        }

    @app.route("/robots.txt")
    def robots():
        # Keep crawlers out of the auth paths. A crawled endpoint that redirects
        # to a university login page is what phishing classifiers look for, and
        # none of this is content anyone should be indexing.
        body = "User-agent: *\nDisallow: /auth/\nDisallow: /oauth/\nDisallow: /courses\n"
        return body, 200, {"Content-Type": "text/plain; charset=utf-8"}

    _register_google_site_verification(app)

    from app.routes.auth import auth_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.rubric import rubric_bp
    from app.routes.review import review_bp
    from app.routes.export import export_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(rubric_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(export_bp)

    return app
