from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal

import httpx
from fastapi import HTTPException, Request, status
from pydantic import BaseModel
from app.app_settings import EnvSettings
from app.app_settings import get_env_settings as get_app_env_settings

try:
    import jwt
    from jwt import PyJWKClient
except ImportError:  # pragma: no cover - optional dependency
    jwt = None
    PyJWKClient = None


# ============================================================
# Settings
# ============================================================

def get_env_settings() -> EnvSettings:
    return get_app_env_settings()


# ============================================================
# Models
# ============================================================

PrincipalKind = Literal["user", "service", "internal"]
TokenSource = Literal["session", "bearer", "token_exchange", "internal", "header"]


@dataclass
class Principal:
    kind: PrincipalKind
    subject: str
    user: dict[str, Any] | None = None
    client_id: str | None = None
    username: str | None = None
    scopes: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    claims: dict[str, Any] | None = None
    delegated_by: str | None = None
    token_source: TokenSource | None = None


class OIDCConfiguration(BaseModel):
    issuer: str
    jwks_uri: str


# ============================================================
# OIDC / Keycloak helpers
# ============================================================

def _build_realm_base_url(settings: EnvSettings) -> str:
    base = settings.keycloak_server_url_internal.rstrip("/")
    realm = settings.keycloak_realm
    return f"{base}/realms/{realm}"


def _build_oidc_discovery_url(settings: EnvSettings) -> str:
    return f"{_build_realm_base_url(settings)}/.well-known/openid-configuration"


@lru_cache(maxsize=1)
def get_oidc_configuration() -> OIDCConfiguration:
    settings = get_env_settings()
    url = _build_oidc_discovery_url(settings)

    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise RuntimeError(
            f"Unable to load OIDC discovery document from {url}: {exc}"
        ) from exc

    issuer = data.get("issuer")
    jwks_uri = data.get("jwks_uri")

    if not issuer or not jwks_uri:
        raise RuntimeError("OIDC discovery document missing issuer or jwks_uri")

    return OIDCConfiguration(issuer=issuer, jwks_uri=jwks_uri)


@lru_cache(maxsize=1)
def get_jwk_client() -> PyJWKClient:
    if PyJWKClient is None:
        raise RuntimeError("pyjwt is required for Keycloak JWT validation")
    cfg = get_oidc_configuration()
    return PyJWKClient(cfg.jwks_uri)


# ============================================================
# Header / token extraction
# ============================================================

def _get_authorization_header(request: Request) -> str:
    settings = get_env_settings()
    header_name = settings.token_header.lower()
    return request.headers.get(header_name, "")


def _extract_bearer_token(request: Request) -> str | None:
    raw = _get_authorization_header(request).strip()
    if not raw:
        return None

    parts = raw.split(" ", 1)
    if len(parts) != 2:
        return None

    scheme, token = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None

    return token


def _extract_remote_user(request: Request) -> str | None:
    settings = get_env_settings()
    header_name = settings.keycloak_remote_user_header.lower()
    value = request.headers.get(header_name, "").strip()
    return value or None


# ============================================================
# Claims helpers
# ============================================================

def _extract_client_id(payload: dict[str, Any]) -> str | None:
    return payload.get("client_id") or payload.get("azp")


def _extract_scopes(payload: dict[str, Any]) -> set[str]:
    raw_scope = payload.get("scope", "")
    if isinstance(raw_scope, str):
        return {item for item in raw_scope.split() if item}
    return set()


def _extract_roles(payload: dict[str, Any], settings: EnvSettings) -> set[str]:
    roles: set[str] = set()

    realm_access = payload.get("realm_access") or {}
    if isinstance(realm_access, dict):
        raw_roles = realm_access.get("roles") or []
        if isinstance(raw_roles, list):
            roles.update(str(r) for r in raw_roles)

    resource_access = payload.get("resource_access") or {}
    if isinstance(resource_access, dict):
        # ruoli assegnati al client web/backend della tua app
        client_block = resource_access.get(settings.keycloak_client_id) or {}
        if isinstance(client_block, dict):
            raw_client_roles = client_block.get("roles") or []
            if isinstance(raw_client_roles, list):
                roles.update(str(r) for r in raw_client_roles)

        # opzionale: ruoli sul client che ha richiesto il token
        requester_client_id = _extract_client_id(payload)
        if requester_client_id:
            requester_block = resource_access.get(requester_client_id) or {}
            if isinstance(requester_block, dict):
                requester_roles = requester_block.get("roles") or []
                if isinstance(requester_roles, list):
                    roles.update(str(r) for r in requester_roles)

    return roles


def _extract_username(payload: dict[str, Any]) -> str | None:
    return (
        payload.get("preferred_username")
        or payload.get("username")
        or payload.get("email")
    )


def _normalize_audience(aud: Any) -> set[str]:
    if isinstance(aud, str):
        return {aud}
    if isinstance(aud, list):
        return {str(x) for x in aud}
    return set()


def _is_service_subject(subject: str | None) -> bool:
    if not subject:
        return False
    return str(subject).startswith("service-account-")


def _looks_like_user_token(payload: dict[str, Any]) -> bool:
    """
    Euristica pragmatica:
    - se ho preferred_username / email / username -> probabile token utente
    - se sub inizia con service-account- -> token service
    """
    sub = payload.get("sub")
    if _is_service_subject(sub):
        return False

    if payload.get("preferred_username") or payload.get("email") or payload.get("username"):
        return True

    return False


# ============================================================
# JWT validation
# ============================================================

def _allowed_audiences(settings: EnvSettings) -> set[str]:
    allowed = {
        settings.keycloak_client_id,
        settings.app_code,
        settings.app_name,
    }

    return {x for x in allowed if x}


def _decode_jwt_token(token: str) -> dict[str, Any]:
    if jwt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="pyjwt is required for Keycloak JWT validation",
        )
    settings = get_env_settings()
    cfg = get_oidc_configuration()
    jwk_client = get_jwk_client()

    try:
        signing_key = jwk_client.get_signing_key_from_jwt(token).key
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unable to resolve signing key: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    try:
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512"],
            issuer=cfg.issuer,
            options={
                "require": ["exp", "iat", "iss"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": False,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token issuer",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    aud_set = _normalize_audience(payload.get("aud"))
    allowed_aud = _allowed_audiences(settings)

    if allowed_aud and aud_set and aud_set.isdisjoint(allowed_aud):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Audience not allowed: aud={sorted(aud_set)} allowed={sorted(allowed_aud)}",
        )

    return payload


# ============================================================
# Principal builders
# ============================================================

def _principal_from_session(request: Request) -> Principal | None:
    user = request.session.get("user")
    if not user:
        return None

    if not isinstance(user, dict):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session user object",
        )

    subject = str(
        user.get("id")
        or user.get("sub")
        or user.get("username")
        or user.get("email")
        or "unknown"
    )

    roles = set()
    raw_roles = user.get("roles")
    if isinstance(raw_roles, list):
        roles = {str(r) for r in raw_roles}

    username = (
        user.get("username")
        or user.get("preferred_username")
        or user.get("email")
    )

    return Principal(
        kind="user",
        subject=subject,
        user=user,
        username=username,
        roles=roles,
        token_source="session",
    )


def _principal_from_remote_user(request: Request) -> Principal | None:
    remote_user = _extract_remote_user(request)
    if not remote_user:
        return None

    return Principal(
        kind="user",
        subject=remote_user,
        username=remote_user,
        user={"username": remote_user},
        token_source="header",
    )


def _principal_from_internal_token(request: Request) -> Principal | None:
    settings = get_env_settings()
    raw = _get_authorization_header(request).strip()
    expected = settings.runtime_internal_token.strip()

    if not raw or not expected:
        return None

    candidates = {raw}
    parts = raw.split(" ", 1)
    if len(parts) == 2:
        candidates.add(parts[1].strip())

    if expected not in candidates:
        return None

    return Principal(
        kind="internal",
        subject="runtime-internal",
        client_id="runtime-internal",
        scopes={"internal"},
        roles={"internal"},
        claims={"auth_type": "runtime_internal_token"},
        token_source="internal",
    )


def _detect_token_exchange(
    payload: dict[str, Any],
    settings: EnvSettings,
    client_id: str | None,
    is_user_token: bool,
) -> tuple[bool, str | None]:
    """
    Non esiste una regola universale perfetta.
    Facciamo una euristica robusta:

    - token exchange tipico:
      - il token rappresenta un utente
      - azp/client_id è il worker confidential client
      - il client che ha ottenuto il token NON è il frontend/browser client standard

    Restituisce:
      (is_token_exchange, delegated_by)
    """
    if not is_user_token:
        return False, None

    if not client_id:
        return False, None

    # token utente "normale" emesso per il client web standard della tua app
    if client_id == settings.keycloak_client_id:
        return False, None

    # se il token è utente ma il requester client è un altro client,
    # lo consideriamo delegated / exchanged
    return True, client_id


def _principal_from_bearer_token(request: Request) -> Principal | None:
    token = _extract_bearer_token(request)
    if not token:
        return None

    payload = _decode_jwt_token(token)
    settings = get_env_settings()

    client_id = _extract_client_id(payload)
    username = _extract_username(payload)
    scopes = _extract_scopes(payload)
    roles = _extract_roles(payload, settings)

    subject = str(
        payload.get("sub")
        or username
        or client_id
        or "unknown"
    )

    is_user_token = _looks_like_user_token(payload)
    is_token_exchange, delegated_by = _detect_token_exchange(
        payload=payload,
        settings=settings,
        client_id=client_id,
        is_user_token=is_user_token,
    )

    if is_user_token:
        return Principal(
            kind="user",
            subject=subject,
            username=username,
            client_id=client_id,
            scopes=scopes,
            roles=roles,
            claims=payload,
            delegated_by=delegated_by,
            token_source="token_exchange" if is_token_exchange else "bearer",
        )

    return Principal(
        kind="service",
        subject=subject,
        username=username,
        client_id=client_id,
        scopes=scopes,
        roles=roles,
        claims=payload,
        token_source="bearer",
    )


# ============================================================
# Public API
# ============================================================

def get_current_principal(request: Request) -> Principal:
    """
    Ordine di risoluzione:
    1. internal token
    2. bearer JWT
    3. remote user header
    4. session user
    """
    settings = get_env_settings()

    if settings.auth_mode.lower() == "header":
        principal = _principal_from_remote_user(request)
        if principal:
            return principal

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing remote user header",
        )

    principal = _principal_from_internal_token(request)
    if principal:
        return principal

    principal = _principal_from_bearer_token(request)
    if principal:
        return principal

    principal = _principal_from_remote_user(request)
    if principal:
        return principal

    principal = _principal_from_session(request)
    if principal:
        return principal

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(request: Request) -> dict[str, Any]:
    principal = get_current_principal(request)

    if principal.kind != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User principal required",
        )

    # utente da sessione
    if principal.user:
        return principal.user

    # utente da token
    claims = principal.claims or {}
    return {
        "sub": principal.subject,
        "username": principal.username,
        "preferred_username": claims.get("preferred_username"),
        "email": claims.get("email"),
        "roles": sorted(principal.roles),
        "scopes": sorted(principal.scopes),
        "delegated_by": principal.delegated_by,
        "token_source": principal.token_source,
    }


def get_current_service(request: Request) -> Principal:
    principal = get_current_principal(request)

    if principal.kind not in {"service", "internal"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service token required",
        )

    return principal


def require_scope(principal: Principal, required_scope: str) -> None:
    if required_scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required scope: {required_scope}",
        )


def require_any_role(
    principal: Principal, allowed_roles: set[str] | list[str]
) -> None:
    allowed = set(allowed_roles)
    if principal.roles.isdisjoint(allowed):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required role. Required one of: {sorted(allowed)}",
        )


def require_user_role(
    request: Request, allowed_roles: set[str] | list[str]
) -> dict[str, Any]:
    principal = get_current_principal(request)

    if principal.kind != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User principal required",
        )

    require_any_role(principal, set(allowed_roles))
    return get_current_user(request)


def is_delegated_user(principal: Principal) -> bool:
    return principal.kind == "user" and principal.token_source == "token_exchange"


def require_delegated_user(principal: Principal) -> None:
    if not is_delegated_user(principal):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Delegated user token required",
        )


def require_direct_user(principal: Principal) -> None:
    if principal.kind != "user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User principal required",
        )

    if principal.token_source == "token_exchange":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Direct user token/session required",
        )
