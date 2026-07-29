"""IU Login (OIDC) client.

The IU Login team registers this app and provides the discovery URL,
client id, and client secret. Authlib handles the rest: authorization
redirect (with PKCE + state + nonce), token exchange, and ID-token
signature validation against the issuer's published keys. Duo happens
entirely on IU's login pages -- this app never touches credentials.
"""
from authlib.integrations.flask_client import OAuth

oauth = OAuth()


def init_oidc(app):
    oauth.init_app(app)
    if app.config["IU_OIDC_DISCOVERY_URL"] and app.config["IU_OIDC_CLIENT_ID"]:
        oauth.register(
            name="iu",
            client_id=app.config["IU_OIDC_CLIENT_ID"],
            client_secret=app.config["IU_OIDC_CLIENT_SECRET"],
            server_metadata_url=app.config["IU_OIDC_DISCOVERY_URL"],
            client_kwargs={
                "scope": "openid profile email",
                "code_challenge_method": "S256",
            },
        )


def is_configured():
    return oauth.create_client("iu") is not None
