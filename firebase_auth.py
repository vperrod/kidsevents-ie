"""Firebase token verification and public web configuration helpers."""

import os

import firebase_admin
from firebase_admin import auth


class AuthenticationError(Exception):
    """A client did not provide an acceptable verified Firebase identity."""


def public_config():
    keys = {
        "apiKey": "FIREBASE_API_KEY",
        "authDomain": "FIREBASE_AUTH_DOMAIN",
        "projectId": "FIREBASE_PROJECT_ID",
        "appId": "FIREBASE_APP_ID",
    }
    config = {name: os.environ.get(environment_name, "").strip() for name, environment_name in keys.items()}
    return config if all(config.values()) else None


def _firebase_app():
    config = public_config()
    if config is None:
        raise AuthenticationError("Authentication is not configured")
    try:
        return firebase_admin.get_app()
    except ValueError:
        return firebase_admin.initialize_app(options={"projectId": config["projectId"]})


def verified_member(authorization_header):
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise AuthenticationError("Sign in is required")
    token = authorization_header.removeprefix("Bearer ").strip()
    if not token:
        raise AuthenticationError("Sign in is required")
    try:
        decoded = auth.verify_id_token(token, app=_firebase_app(), check_revoked=True)
    except Exception as error:
        raise AuthenticationError("Your sign-in could not be verified") from error
    if not decoded.get("email") or not decoded.get("email_verified"):
        raise AuthenticationError("A verified email address is required")
    return {
        "provider_subject": f"firebase:{decoded['uid']}",
        "email": decoded["email"],
    }
