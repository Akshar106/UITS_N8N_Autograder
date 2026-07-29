import os

from flask import Flask, flash, redirect, render_template, session, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect
from werkzeug.middleware.proxy_fix import ProxyFix

from app.config import Config
from app.storage import close_db, init_db

csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    # Azure (and most PaaS) terminates TLS at a proxy; trust its forwarding
    # headers so url_for builds https:// redirect URIs.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    csrf.init_app(app)

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
        # Render directly (no redirect) so a Canvas outage can't cause a redirect loop.
        return render_template("error.html", message=f"Canvas request failed: {exc}"), 502

    @app.errorhandler(CSRFError)
    def handle_csrf_error(exc):
        flash("Your form session expired. Please try again.", "error")
        return redirect(url_for("auth.index"))

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
