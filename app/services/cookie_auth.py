from __future__ import annotations

import secrets

from itsdangerous import BadSignature
from itsdangerous import SignatureExpired
from itsdangerous import URLSafeTimedSerializer


def _make_signer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="ozon-session")


def sign_token(token: str, secret: str) -> str:
    return _make_signer(secret).dumps(token)


def verify_token(
    value: str, secret: str, max_age: int = 86400
) -> str | None:
    try:
        return _make_signer(secret).loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)
