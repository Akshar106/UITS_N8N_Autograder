import logging
import os

from flask import Flask, flash, redirect, render_template, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.storage import close_db, init_db

csrf = CSRFProtect()

logger = logging.getLogger(__name__)


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
        return {"status": "ok"}

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
