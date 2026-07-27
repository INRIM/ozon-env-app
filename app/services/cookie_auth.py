from __future__ import annotations

import base64
import json
import secrets
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Il cookie di sessione trasporta il bundle token keycloak
# (`access_token` + `refresh_token`, vedi OzonOrm.build_auth_user). Prima
# veniva solo FIRMATO con itsdangerous: la firma impedisce di forgiarlo,
# ma il contenuto resta base64 in chiaro, quindi chiunque intercetti il
# cookie (dump di proxy, backup del browser, log di un intermediario) si
# porta via un refresh token keycloak a vita lunga — utilizzabile
# direttamente contro keycloak, fuori dal controllo di questa app.
#
# Fernet fornisce cifratura autenticata (AES-128-CBC + HMAC-SHA256) con
# timestamp incorporato: il contenuto non e' piu' leggibile, la
# manomissione resta rilevabile come prima, e il TTL sostituisce
# `max_age` di URLSafeTimedSerializer.
_HKDF_INFO = b"ozon-session-cookie-v1"


@lru_cache(maxsize=8)
def _fernet(secret: str) -> Fernet:
    """Fernet derivato da SESSION_SECRET.

    HKDF e non PBKDF2: il segreto in ingresso e' gia' ad alta entropia
    (non una password umana), e la derivazione gira ad ogni richiesta —
    uno stretch costoso qui sarebbe solo un DoS su noi stessi. La cache
    evita comunque di riderivare ad ogni chiamata.
    """
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"ozon-session",
        info=_HKDF_INFO,
    ).derive(secret.encode("utf-8"))
    return Fernet(base64.urlsafe_b64encode(key_material))


def sign_token(token: Any, secret: str) -> str:
    """Serializza e CIFRA il payload per il cookie.

    Il nome resta `sign_token` per non toccare i chiamanti: la firma c'e'
    ancora (Fernet autentica), in piu' ora il contenuto e' cifrato.
    Accetta str (state OAuth2) o dict (bundle token).
    """
    payload = json.dumps(token, separators=(",", ":"), default=str)
    return _fernet(secret).encrypt(payload.encode("utf-8")).decode("ascii")


def verify_token(
    value: str, secret: str, max_age: int = 86400
) -> Any | None:
    """Decifra e valida il cookie. `None` se invalido, manomesso o scaduto."""
    if not value:
        return None
    try:
        ttl = int(max_age)
    except (TypeError, ValueError):
        return None
    # Fernet con ttl=0 accetta un token appena emesso (l'eta' e' 0 e il
    # confronto non e' stretto). Qui "max_age <= 0" significa "nessuna
    # sessione valida", non "nessun controllo": esplicitato per non
    # trasformare un AUTH_COOKIE_MAX_AGE=0 in un controllo disattivato.
    if ttl <= 0:
        return None
    try:
        raw = _fernet(secret).decrypt(value.encode("ascii"), ttl=ttl)
    except (InvalidToken, UnicodeEncodeError, ValueError, TypeError):
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)
