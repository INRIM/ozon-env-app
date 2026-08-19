"""Revoca sessioni via OIDC Back-Channel Logout.

Il problema che risolve: `OzonOrm.authenticate_user_token` verifica
l'access token solo localmente (firma via JWKS, `exp`, `iss`, `aud`).
Nessuna di queste verifiche sa che la sessione SSO e' stata terminata
lato Keycloak (logout dell'utente, logout amministrativo, scadenza
idle): finche' il JWT non scade, l'app continua ad accettarlo. Con
`accessTokenLifespan` a 300s significa 5 minuti di sessione zombie —
di piu' se il realm alza quel valore.

L'alternativa sarebbe introspection (RFC 7662) ad ogni richiesta: esatta
ma costa un round-trip HTTP verso Keycloak sul path caldo di OGNI
richiesta autenticata, e rende Keycloak una dipendenza hard runtime.
Qui si usa invece il push: Keycloak notifica la terminazione su
`/auth/backchannel-logout`, l'app registra `sid` (o `sub`) revocato e
ogni richiesta successiva fa una sola lookup su db gia' aperto.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ozonenv.core.auth import KeycloakAuthManager
from ozonenv.core.auth import KeycloakAuthSettings

from app.app_settings import EnvSettings

logger = logging.getLogger("uvicorn.error")

REVOKED_MODEL = "revoked_session"

# Evento obbligatorio nel logout token (OpenID Connect Back-Channel
# Logout 1.0, §2.4). Senza questo controllo un normale ID token
# — stessa firma, stesso issuer, stessa audience — verrebbe accettato
# come ordine di logout: chiunque possa fare login potrebbe sloggare
# chiunque altro presentando l'ID token di un'altra sessione.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

# Quanto tenere una riga di revoca dopo l'ultimo istante in cui un token
# emesso prima della revoca puo' ancora essere valido. Il bound reale e'
# `accessTokenLifespan`, ignoto qui: un'ora copre qualunque
# configurazione ragionevole restando una tabella minuscola.
REVOCATION_RETENTION_SECONDS = 3600


class BackchannelLogoutError(ValueError):
    """Logout token non valido: non deve mai revocare nulla."""


def _logout_auth_manager(settings: EnvSettings) -> KeycloakAuthManager:
    """Auth manager con `audience` = client_id.

    NON si puo' riusare `env.get_user_auth_manager()`: quello ha
    `audience` = `OZON_TOKEN_AUDIENCE` (l'audience custom iniettata dal
    protocol mapper negli ACCESS token). Il logout token non passa da
    quel mapper — la sua `aud` e' il client_id — quindi verificarlo con
    l'altro manager fallirebbe sempre con "Audience doesn't match".
    """
    return KeycloakAuthManager(
        KeycloakAuthSettings(
            jwks_url=settings.keycloak_jwks_url,
            issuer=settings.keycloak_issuer,
            audience=settings.keycloak_client_id,
            oauth_url=settings.keycloak_token_endpoint,
            client_id=settings.keycloak_client_id,
            client_secret=settings.keycloak_client_secret,
        )
    )


async def verify_logout_token(
    logout_token: str,
    settings: EnvSettings,
) -> dict[str, Any]:
    """Valida il logout token e ritorna le sue claims.

    Solleva `BackchannelLogoutError` su qualunque violazione: il
    chiamante deve rispondere 400 senza toccare lo store.
    """
    token = str(logout_token or "").strip()
    if not token:
        raise BackchannelLogoutError("Missing logout_token")

    try:
        verified = await _logout_auth_manager(settings).verify(token)
    except Exception as exc:
        raise BackchannelLogoutError(f"Invalid logout token: {exc}") from exc

    claims = verified.claims

    events = claims.get("events")
    if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
        raise BackchannelLogoutError("Logout token missing backchannel event")

    # §2.6: un logout token con `nonce` e' un ID token riciclato.
    if claims.get("nonce"):
        raise BackchannelLogoutError("Logout token must not contain nonce")

    if not str(claims.get("sid") or "").strip() and not str(
        claims.get("sub") or ""
    ).strip():
        raise BackchannelLogoutError("Logout token missing both sid and sub")

    return claims


def _revocation_model(ozon_env: Any) -> Any:
    return ozon_env.get(REVOKED_MODEL)


async def revoke_session(ozon_env: Any, claims: dict[str, Any]) -> str:
    """Registra la revoca descritta dalle claims del logout token.

    Ritorna il `rec_name` scritto (utile solo per i log/test).
    """
    sid = str(claims.get("sid") or "").strip()
    sub = str(claims.get("sub") or "").strip()
    now = time.time()
    # rec_name deterministico sul sid: Keycloak puo' rispedire lo stesso
    # logout token (retry su timeout), e riscrivere la stessa riga e'
    # esattamente il comportamento voluto — la revoca e' idempotente.
    # Sul solo `sub` invece la riga NON e' unica: revoche successive
    # dello stesso utente hanno `revoked_at` diversi e devono coesistere
    # solo finche' servono, quindi il timestamp entra nel nome.
    rec_name = f"sid-{sid}" if sid else f"sub-{sub}-{int(now * 1000)}"

    model = _revocation_model(ozon_env)
    saved = await model.upsert(
        {
            "rec_name": rec_name,
            "sid": sid,
            "sub": sub,
            "uid": str(claims.get("preferred_username") or sub or ""),
            "revoked_at": now,
            "expire_at": now + REVOCATION_RETENTION_SECONDS,
        }
    )
    if saved is None:
        raise RuntimeError(
            f"cannot persist session revocation: {model.status}"
        )
    logger.info("session revoked sid=%s sub=%s", sid or "-", sub or "-")
    return rec_name


async def is_session_revoked(
    ozon_env: Any,
    claims: dict[str, Any],
) -> bool:
    """True se il token descritto da `claims` appartiene a una sessione
    revocata.

    Due match distinti:
      - `sid` uguale: revoca della singola sessione;
      - `sub` uguale con `revoked_at` successivo alla `iat` del token:
        revoca "tutte le sessioni dell'utente". Il confronto su `iat`
        evita che una vecchia revoca invalidi il login successivo dello
        stesso utente.
    """
    sid = str(claims.get("sid") or "").strip()
    sub = str(claims.get("sub") or "").strip()
    if not sid and not sub:
        return False

    try:
        issued_at = float(claims.get("iat") or 0)
    except (TypeError, ValueError):
        issued_at = 0.0

    conditions: list[dict[str, Any]] = []
    if sid:
        conditions.append({"sid": sid})
    if sub:
        conditions.append({"sid": "", "sub": sub, "revoked_at": {"$gt": issued_at}})

    model = _revocation_model(ozon_env)
    found = await model.count({"$or": conditions})
    return bool(found)


async def purge_expired_revocations(ozon_env: Any) -> int:
    """Cancella le righe che non possono piu' invalidare nessun token.

    Chiamata dall'endpoint di back-channel logout (poche volte al
    giorno), NON dal path di autenticazione: li' costerebbe una scrittura
    per richiesta per tenere pulita una tabella da poche decine di righe.
    """
    model = _revocation_model(ozon_env)
    expired = await model.find({"expire_at": {"$lt": time.time()}})
    removed = 0
    for record in expired or []:
        if await model.remove(record):
            removed += 1
    if removed:
        logger.info("purged %d expired session revocations", removed)
    return removed
