from __future__ import annotations

import base64
import json
import secrets
import time
import zlib
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


# Il payload va COMPRESSO prima di cifrare. Il bundle keycloak e' fatto
# di JWT (base64 di JSON): comprime ~4x. Senza zlib il cookie passa da
# ~1.2 KB a ~5 KB e sfonda due limiti diversi:
#   - i proxy_buffer_size di default di nginx (4 KB) -> 502 "upstream
#     sent too big header" sul reverse proxy davanti all'app;
#   - il limite di ~4 KB per singolo cookie dei browser.
# `itsdangerous`, usato prima, comprimeva di suo; Fernet no, quindi la
# compressione va fatta a mano qui. Non indebolisce la cifratura: il
# testo cifrato e' comunque autenticato, e il contenuto non e' scelto
# dall'attaccante (nessun oracolo tipo CRIME/BREACH: il cookie non
# mescola dati attaccante-controllati con il segreto, e non viaggia su
# un canale dove l'attaccante ne osserva la lunghezza a ripetizione).
_ZLIB_LEVEL = 9


def sign_token(token: Any, secret: str) -> str:
    """Serializza, COMPRIME e CIFRA il payload per il cookie.

    Il nome resta `sign_token` per non toccare i chiamanti: la firma c'e'
    ancora (Fernet autentica), in piu' ora il contenuto e' cifrato.
    Accetta str (state OAuth2) o dict (bundle token).
    """
    payload = json.dumps(token, separators=(",", ":"), default=str)
    compressed = zlib.compress(payload.encode("utf-8"), _ZLIB_LEVEL)
    return _fernet(secret).encrypt(compressed).decode("ascii")


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
        return json.loads(zlib.decompress(raw).decode("utf-8"))
    except (zlib.error, ValueError, UnicodeDecodeError):
        return None


def make_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def refresh_token_max_age(token: Any) -> int:
    """Vita residua del refresh token nel bundle, in secondi (0 se ignota).

    Serve a NON far vivere il cookie di sessione piu' a lungo della
    sessione SSO che lo sostiene. Con `AUTH_COOKIE_MAX_AGE` a 24h e un
    realm con `ssoSessionMaxLifespan` a 10h il browser continua a
    presentare un cookie che Keycloak ha gia' dimenticato: il refresh
    fallisce e l'utente si becca un 401 a meta' lavoro, senza che nulla
    nel client segnalasse la scadenza. Allineando il cookie al refresh
    token la sessione lato browser finisce quando finisce quella vera.
    """
    if not isinstance(token, dict):
        return 0
    raw = token.get("refresh_expires_in")
    try:
        expires_in = int(raw)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        return expires_in
    refresh_token = str(token.get("refresh_token") or "")
    if refresh_token.count(".") != 2:
        # Keycloak emette refresh token opachi quando la sessione e'
        # offline: nessuna scadenza deducibile, si tiene il configurato.
        return 0
    try:
        chunk = refresh_token.split(".")[1]
        payload = json.loads(
            base64.urlsafe_b64decode(chunk + "=" * (-len(chunk) % 4)).decode(
                "utf-8"
            )
        )
        remaining = int(payload["exp"]) - int(time.time())
    except Exception:
        return 0
    return max(remaining, 0)


def session_cookie_max_age(configured_max_age: int, token: Any) -> int:
    """`max_age` effettivo: il minore tra configurato e vita del refresh."""
    try:
        configured = int(configured_max_age)
    except (TypeError, ValueError):
        return 0
    bound = refresh_token_max_age(token)
    if bound <= 0:
        return configured
    return min(configured, bound)
