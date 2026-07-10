import os

from flask import Flask, flash, redirect, render_template, session, url_for

from app.config import Config
from app.storage import close_db, init_db


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(os.path.dirname(app.config["DATABASE_PATH"]) or ".", exist_ok=True)
    init_db(app)
    app.teardown_appcontext(close_db)

    from app.auth_utils import AuthError
    from app.canvas_client import CanvasAPIError

    @app.errorhandler(AuthError)
    def handle_auth_error(exc):
        session.clear()
        flash(str(exc), "error")
        return redirect(url_for("auth.index"))

    @app.errorhandler(CanvasAPIError)
    def handle_canvas_error(exc):
        # Render directly (no redirect) so a Canvas outage can't cause a redirect loop.
        return render_template("error.html", message=f"Canvas request failed: {exc}"), 502

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
