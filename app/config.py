import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-only-insecure-key")

    APP_ENV = os.getenv("APP_ENV", "development")

    # The session cookie is what proves who a request belongs to, and every
    # stored rubric and grade is scoped by the IU id inside it. Signed with a
    # key that is published in this repository, anyone can mint a cookie naming
    # any faculty member and read or change their students' grades. Refusing to
    # start is the only safe answer; the alternative is a quiet auth bypass.
    if APP_ENV == "production" and not os.getenv("FLASK_SECRET_KEY"):
        raise RuntimeError(
            "FLASK_SECRET_KEY is not set and APP_ENV=production. Set it as an "
            "Application Setting to any long random string (for example the "
            "output of `python3 -c \"import secrets; print(secrets.token_hex(32))\"`). "
            "Changing it signs everyone out once; leaving it unset lets session "
            "cookies be forged."
        )
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = APP_ENV == "production"

    # Rubrics are short documents. Without a cap, a single large upload can
    # exhaust memory on the 1.75 GB Basic B1 instance and take the app down.
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "10")) * 1024 * 1024

    # Stripped: credentials pasted into a portal or .env very easily pick up a
    # trailing space or newline, which fails authentication with an error that
    # says nothing about whitespace. No legitimate value has leading/trailing space.
    IU_OIDC_DISCOVERY_URL = os.getenv("IU_OIDC_DISCOVERY_URL", "").strip()
    IU_OIDC_CLIENT_ID = (os.getenv("IU_OIDC_CLIENT_ID") or "").strip() or None
    IU_OIDC_CLIENT_SECRET = (os.getenv("IU_OIDC_CLIENT_SECRET") or "").strip() or None
    # https, not http: IU Login only accepts HTTPS redirect URIs, and this exact
    # string is what is registered with IMS. Run locally with USE_HTTPS=1.
    IU_OIDC_REDIRECT_URI = os.getenv("IU_OIDC_REDIRECT_URI", "https://localhost:8000/auth/iu/callback")

    # How the app authenticates itself at the token endpoint. Must match what IMS
    # configured for this client. client_secret_basic base64-encodes the credentials
    # in an Authorization header (and URL-encodes them first, per RFC 6749, which can
    # mismatch servers expecting raw bytes when the secret contains +, /, = or %);
    # client_secret_post sends them as plain form fields instead. A mismatch here
    # surfaces as "Client authentication failed" after an otherwise successful login.
    IU_OIDC_TOKEN_AUTH_METHOD = os.getenv("IU_OIDC_TOKEN_AUTH_METHOD", "client_secret_basic")

    # PKCE hardens the code exchange and is normally desirable, but the IdP has to
    # be provisioned for it. Shibboleth can answer a code_verifier it was not
    # expecting with an opaque "InvalidEvent", so allow turning it off to isolate
    # that. Leave enabled unless IU confirms the client is registered without PKCE.
    IU_OIDC_USE_PKCE = os.getenv("IU_OIDC_USE_PKCE", "1") == "1"

    CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "").rstrip("/")
    CANVAS_CLIENT_ID = os.getenv("CANVAS_CLIENT_ID")
    CANVAS_CLIENT_SECRET = os.getenv("CANVAS_CLIENT_SECRET")
    CANVAS_REDIRECT_URI = os.getenv("CANVAS_REDIRECT_URI", "http://localhost:8000/oauth/callback")
    CANVAS_OAUTH_SCOPES = os.getenv("CANVAS_OAUTH_SCOPES", "").split()

    # Canvas enrollment roles treated as "may use this tool". IU Login authenticates
    # every IU account, so this -- not the login itself -- is what keeps students out.
    # Canvas is the authority on who teaches what, which avoids needing IU's
    # restricted affiliation scopes. Drop "ta" here to restrict to teachers only.
    INSTRUCTOR_ENROLLMENT_TYPES = [
        t.strip() for t in os.getenv("INSTRUCTOR_ENROLLMENT_TYPES", "teacher,ta").split(",") if t.strip()
    ]

    REALLM_API_KEY = os.getenv("REALLM_API_KEY")
    REALLM_URL = os.getenv("REALLM_URL")
    REALLM_MODEL_NAME = os.getenv("REALLM_MODEL_NAME")

    # On Azure App Service the app runs from a package extracted into /tmp, so a
    # RELATIVE path puts the database on ephemeral storage: every container
    # recycle silently destroys the Canvas tokens, rubrics, grades and approvals.
    # Only /home is persisted, so that is the default there. WEBSITE_INSTANCE_ID
    # is set by App Service and by nothing else.
    DATABASE_PATH = os.getenv("DATABASE_PATH") or (
        "/home/site/data/autograder.sqlite3" if os.getenv("WEBSITE_INSTANCE_ID")
        else "instance/autograder.sqlite3"
    )
