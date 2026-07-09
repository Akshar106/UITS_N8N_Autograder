import os

from flask import Flask

from app.config import Config
from app.storage import close_db, init_db


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(os.path.dirname(app.config["DATABASE_PATH"]) or ".", exist_ok=True)
    init_db(app)
    app.teardown_appcontext(close_db)

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
