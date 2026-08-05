import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    # IU Login only accepts HTTPS redirect URIs, including for localhost, so local
    # testing of the login flow needs TLS. USE_HTTPS=1 serves with an on-the-fly
    # self-signed certificate (pip install cryptography). The browser will warn
    # about the certificate once -- accept it and the OIDC round trip works.
    # Production runs under gunicorn behind Azure's TLS, so this never applies there.
    ssl_context = "adhoc" if os.getenv("USE_HTTPS") == "1" else None
    app.run(host="0.0.0.0", port=8000, debug=True, ssl_context=ssl_context)
