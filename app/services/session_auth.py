from __future__ import annotations

import base64
import json
import logging
import uuid
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.BaseModels import BasicReturn, CoreModel

from app.app_settings import EnvSettings
from app.core.OzonModelApp import DateEngineApp
from app.core.session import AppSession

logger = logging.getLogger("uvicorn.error")

AUTH_MODE_TOKEN = "token"
AUTH_MODE_KEYCLOAK = "keycloak"

AUTH_MODE_ALIASES = {
    AUTH_MODE_TOKEN: AUTH_MODE_TOKEN,
    "bearer": AUTH_MODE_TOKEN,
    AUTH_MODE_KEYCLOAK: AUTH_MODE_KEYCLOAK,
    "trusted-header": AUTH_MODE_KEYCLOAK,
    "header": AUTH_MODE_KEYCLOAK,
    "oidc": AUTH_MODE_KEYCLOAK,
}

SSO_ACCESS_HEADERS = (
    "x-auth-request-access-token",
    "x-access-token",
)
SSO_REFRESH_HEADERS = (
    "x-refresh-token",
    "x-keycloak-refresh-token",
    "x-auth-request-refresh-token",
)


def normalize_auth_mode(mode: str | None) -> str:
    raw_mode = str(mode or AUTH_MODE_TOKEN).strip().lower()
    normalized = AUTH_MODE_ALIASES.get(raw_mode)
    if normalized:
        return normalized
    raise ValueError(f"Unsupported AUTH_MODE '{mode}'")


def session_to_app_session(session: Any, app_code: str) -> AppSession:
    payload = _model_to_dict(session)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session payload",
        )
    uid = str(payload.get("uid") or "").strip()
    if uid:
        payload["uid"] = uid
    if app_code:
        payload["app_code"] = app_code
    return AppSession(**payload)


async def build_keycloak_session(
    ozon_env: OzonEnv,
    request: Request,
    settings: EnvSettings,
    app_code: str,
) -> AppSession:
    remote_user = _extract_remote_user(request, settings)
    admins = list(settings.admins or [])
    user_record = await _get_or_create_user(ozon_env, remote_user, admins)
    user_dict = _model_to_dict(user_record)

    access_token, refresh_token = _extract_sso_tokens(request, settings)
    token_expire = _decode_token_expiry(access_token) if access_token else None

    user_snapshot = _make_user_snapshot(user_dict, remote_user)
    created_at, expires_at = _session_window(ozon_env)
    now_ts = datetime.now().timestamp()

    existing_token = str(user_dict.get("token") or "").strip()

    payload = user_dict.copy()
    payload.update(
        {
            "uid": remote_user,
            "rec_name": remote_user,
            "token": existing_token or str(uuid.uuid4()),
            "app_code": app_code,
            "full_name": _full_name(user_snapshot),
            "divisione_uo": str(
                user_snapshot.get("divisione_uo")
                or user_snapshot.get("owner_sector")
                or ""
            ),
            "user_function": str(
                user_snapshot.get("user_function")
                or user_snapshot.get("owner_function")
                or ""
            ),
            "function": str(
                user_snapshot.get("function")
                or user_snapshot.get("owner_job_title")
                or ""
            ),
            "sector": str(
                user_snapshot.get("sector")
                or user_snapshot.get("owner_sector")
                or ""
            ),
            "sector_id": _int_value(
                user_snapshot.get("sector_id")
                or user_snapshot.get("owner_sector_id")
            ),
            "owner_uid": str(user_snapshot.get("uid") or remote_user),
            "owner_name": _full_name(user_snapshot),
            "owner_mail": str(user_snapshot.get("mail") or ""),
            "owner_function": str(
                user_snapshot.get("user_function")
                or user_snapshot.get("owner_function")
                or ""
            ),
            "owner_sector": str(
                user_snapshot.get("sector")
                or user_snapshot.get("owner_sector")
                or ""
            ),
            "owner_sector_id": _int_value(
                user_snapshot.get("sector_id")
                or user_snapshot.get("owner_sector_id")
            ),
            "owner_personal_type": str(
                user_snapshot.get("owner_personal_type") or ""
            ),
            "owner_job_title": str(
                user_snapshot.get("owner_job_title") or ""
            ),
            "is_admin": bool(user_snapshot.get("is_admin", False)),
            "use_auth": True,
            "is_api": True,
            "login_complete": True,
            "last_update": now_ts,
            "create_datetime": payload.get("create_datetime") or created_at,
            "expire_datetime": expires_at,
            "user": user_snapshot,
            "sso_token": access_token or str(user_dict.get("sso_token") or ""),
            "sso_refresh": refresh_token or str(user_dict.get("sso_refresh") or ""),
            "sso_expire": token_expire or user_dict.get("sso_expire"),
        }
    )
    payload.setdefault("app", {"app_code": app_code})
    payload.setdefault("apps", {app_code: {"app_code": app_code}})

    session = AppSession(**payload)
    # Set user_session before persist so ORM owner-tracking has context
    ozon_env.user_session = session
    ozon_env.session_token = session.token
    await persist_user_session(ozon_env, session)

    if not getattr(ozon_env, "upload_folder", ""):
        ozon_env.upload_folder = getattr(
            getattr(ozon_env.orm, "app_settings", None), "upload_folder", ""
        )
    logger.info(
        "keycloak session ready app_code=%s uid=%s",
        app_code,
        remote_user,
    )
    return session


async def build_keycloak_session_from_tokens(
    ozon_env: OzonEnv,
    settings: EnvSettings,
    app_code: str,
    token: Any,
) -> CoreModel:
    """BFF callback path: validate Keycloak token dict via session_app()."""
    ozon_env.params["current_token"] = token
    res: BasicReturn = await ozon_env.session_app()

    if res.fail:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot extract user identity from Keycloak token {res.msg}",
        )

    if not getattr(ozon_env, "upload_folder", ""):
        ozon_env.upload_folder = getattr(
            getattr(ozon_env.orm, "app_settings", None), "upload_folder", ""
        )
    logger.info(
        "keycloak session ready (bff) app_code=%s uid=%s",
        app_code,
        ozon_env.user_session.uid,
    )
    return ozon_env.user_session


async def ensure_sso_token_fresh(
    ozon_env: OzonEnv,
    settings: EnvSettings,
    session: AppSession,
    refresh_margin_seconds: int = 60,
) -> AppSession:
    sso_expire = _coerce_datetime_utc(getattr(session, "sso_expire", None))
    if sso_expire is None:
        return session
    session.sso_expire = sso_expire

    now = datetime.now(timezone.utc)
    margin = timedelta(seconds=max(0, int(refresh_margin_seconds)))
    if sso_expire - now > margin:
        return session

    refresh_token = str(getattr(session, "sso_refresh", "") or "").strip()
    if not refresh_token:
        if sso_expire <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="SSO access token expired and refresh token is missing",
            )
        return session

    try:
        refreshed = await _refresh_keycloak_tokens(settings, refresh_token)
    except HTTPException:
        if sso_expire <= now:
            raise
        logger.warning("SSO refresh failed but token still valid")
        return session
    except Exception as exc:
        if sso_expire <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Unable to refresh SSO token: {exc}",
            ) from exc
        logger.warning("SSO refresh failed (%s) but token still valid", exc)
        return session

    session.sso_token = str(refreshed.get("access_token") or "")
    session.sso_refresh = str(
        refreshed.get("refresh_token") or session.sso_refresh or ""
    )
    session.sso_expire = _resolve_expire_datetime(refreshed)
    session.last_update = datetime.now().timestamp()

    await persist_user_session(ozon_env, session)
    ozon_env.user_session = session
    return session


async def load_session_by_token(
    ozon_env: OzonEnv,
    token: str,
    app_code: str,
    settings: EnvSettings,
) -> AppSession:
    """Load session from user collection by internal UUID token (BFF cookie path)."""
    user_model = ozon_env.get("user")
    record = await user_model.load({"token": token, "active": True, "deleted": 0})
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session not found or expired",
        )
    data = _model_to_dict(record)
    data["app_code"] = app_code
    session = AppSession(**data)

    ozon_env.user_session = session
    ozon_env.session_token = session.token
    if not getattr(ozon_env, "upload_folder", ""):
        ozon_env.upload_folder = getattr(
            getattr(ozon_env.orm, "app_settings", None), "upload_folder", ""
        )
    return session


async def persist_user_session(ozon_env: OzonEnv, session: AppSession) -> None:
    """Persist session data to user collection via ORM."""
    user_model = ozon_env.get("user")
    data = session.model_dump(mode="python")
    data.setdefault("rec_name", session.uid)
    await user_model.upsert(data)


async def _refresh_keycloak_tokens(
    settings: EnvSettings,
    refresh_token: str,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.keycloak_token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.keycloak_client_secret,
                "refresh_token": refresh_token,
            },
        )

    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Unable to refresh SSO token ({response.status_code})",
        )

    payload = response.json() if response.content else {}
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh response from Keycloak",
        )
    return payload


def _resolve_expire_datetime(tokens: dict[str, Any]) -> datetime | None:
    raw_expires_in = tokens.get("expires_in")
    try:
        expires_in = int(raw_expires_in)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    return _decode_token_expiry(str(tokens.get("access_token") or ""))


def _extract_sso_tokens(
    request: Request,
    settings: EnvSettings,
) -> tuple[str, str]:
    return _extract_access_token(request, settings), _extract_refresh_token(request)


def _extract_access_token(request: Request, settings: EnvSettings) -> str:
    raw_authorization = request.headers.get(settings.token_header, "").strip()
    if raw_authorization:
        lower = raw_authorization.lower()
        if lower.startswith("bearer "):
            token = raw_authorization.split(" ", 1)[1].strip()
            if token:
                return token
        if raw_authorization.count(".") == 2:
            return raw_authorization

    for header in SSO_ACCESS_HEADERS:
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return ""


def _extract_refresh_token(request: Request) -> str:
    for header in SSO_REFRESH_HEADERS:
        value = request.headers.get(header, "").strip()
        if value:
            return value
    return ""


def _decode_token_expiry(token: str) -> datetime | None:
    if not token or token.count(".") != 2:
        return None
    try:
        payload_chunk = token.split(".")[1]
        padding = "=" * (-len(payload_chunk) % 4)
        payload_data = base64.urlsafe_b64decode(payload_chunk + padding)
        payload = json.loads(payload_data.decode("utf-8"))
        exp = int(payload.get("exp"))
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


def _coerce_datetime_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _extract_remote_user(request: Request, settings: EnvSettings) -> str:
    header_name = settings.keycloak_remote_user_header
    raw_value = request.headers.get(header_name, "")
    remote_user = raw_value.strip()
    if remote_user:
        return remote_user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=f"Missing trusted user header '{header_name}'",
    )


async def _get_or_create_user(
    ozon_env: OzonEnv, uid: str, admins: list[str]
) -> Any:
    """Return existing user CoreModel, or create a new one with is_admin set."""
    user_model = ozon_env.get("user")
    query = {
        "$and": [
            {"active": True},
            {"deleted": 0},
            {"$or": [{"uid": uid}, {"rec_name": uid}]},
        ]
    }
    user_record = await user_model.load(query)
    if user_record:
        return user_record

    new_user = {
        "uid": uid,
        "rec_name": uid,
        "is_admin": uid in admins,
        "active": True,
        "deleted": 0,
    }
    created = await user_model.upsert(new_user)
    if created is None:
        logger.error("failed to create user '%s': %s", uid, user_model.status)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create user '{uid}'",
        )
    logger.info("created new user uid=%s is_admin=%s", uid, new_user["is_admin"])
    return created


def _make_user_snapshot(
    user_record: Any,
    fallback_uid: str = "",
) -> dict[str, Any]:
    snapshot = _model_to_dict(user_record)
    if "_id" in snapshot:
        snapshot["id"] = str(snapshot.pop("_id"))
    elif "id" in snapshot:
        snapshot["id"] = str(snapshot["id"])
    if not snapshot.get("uid") and fallback_uid:
        snapshot["uid"] = fallback_uid
    snapshot["full_name"] = _full_name(snapshot)
    return snapshot


def _full_name(user_snapshot: dict[str, Any]) -> str:
    full_name = str(user_snapshot.get("full_name") or "").strip()
    if full_name:
        return full_name
    nome = str(user_snapshot.get("nome") or "").strip()
    cognome = str(user_snapshot.get("cognome") or "").strip()
    return " ".join(part for part in [nome, cognome] if part).strip()


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _model_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record.copy()
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="python")
    if hasattr(record, "dict"):
        return record.dict()
    return {}


def _session_window(ozon_env: OzonEnv) -> tuple[datetime, datetime]:
    app_settings = getattr(ozon_env.orm, "app_settings", None)
    session_expire_hours = getattr(app_settings, "session_expire_hours", 12)
    tz = getattr(app_settings, "tz", "Europe/Rome")
    try:
        expire_hours = int(session_expire_hours or 12)
    except (TypeError, ValueError):
        expire_hours = 12
    dte = DateEngineApp(TZ=tz)
    return dte.gen_datetime_min_max_hours(
        max_hours_delata_date_to=expire_hours
    )
