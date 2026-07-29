import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-only-insecure-key")

    APP_ENV = os.getenv("APP_ENV", "development")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = APP_ENV == "production"

    IU_OIDC_DISCOVERY_URL = os.getenv("IU_OIDC_DISCOVERY_URL", "")
    IU_OIDC_CLIENT_ID = os.getenv("IU_OIDC_CLIENT_ID")
    IU_OIDC_CLIENT_SECRET = os.getenv("IU_OIDC_CLIENT_SECRET")
    IU_OIDC_REDIRECT_URI = os.getenv("IU_OIDC_REDIRECT_URI", "http://localhost:8000/auth/iu/callback")

    CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "").rstrip("/")
    CANVAS_CLIENT_ID = os.getenv("CANVAS_CLIENT_ID")
    CANVAS_CLIENT_SECRET = os.getenv("CANVAS_CLIENT_SECRET")
    CANVAS_REDIRECT_URI = os.getenv("CANVAS_REDIRECT_URI", "http://localhost:8000/oauth/callback")
    CANVAS_OAUTH_SCOPES = os.getenv("CANVAS_OAUTH_SCOPES", "").split()

    REALLM_API_KEY = os.getenv("REALLM_API_KEY")
    REALLM_URL = os.getenv("REALLM_URL")
    REALLM_MODEL_NAME = os.getenv("REALLM_MODEL_NAME")

    DATABASE_PATH = os.getenv("DATABASE_PATH", "instance/autograder.sqlite3")
