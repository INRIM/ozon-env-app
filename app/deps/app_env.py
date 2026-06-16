from copy import copy
from collections.abc import AsyncGenerator
from typing import Annotated
from typing import Any

import logging
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import Security
from fastapi import status
from fastapi.security import APIKeyHeader
from ozonenv.core.auth import TokenExpiredError
from ozonenv.core.auth import TokenRefreshError
from ozonenv.core.auth import TokenVerificationError

from app.app_settings import AppSettings
from app.app_settings import build_public_db_settings_payload
from app.app_settings import get_env_settings
from app.app_settings import merge_public_db_settings
from app.core.OzonEnvApp import AppOzonEnv
from app.core.OzonModelApp import OzonModelApp
from app.core.models import FieldAclPolicy
from app.core.models import MailTemplate, AppUser
from app.services.cookie_auth import sign_token
from app.services.cookie_auth import verify_token
from app.services.service import Service
from app.services.session_auth import _get_app_admins

logger = logging.getLogger("uvicorn.error")

settings = get_env_settings()
api_key_header = APIKeyHeader(name=settings.token_header, auto_error=False)

_STATIC_MODELS = [
    ("mail_template", MailTemplate),
    ("field_acl_policy", FieldAclPolicy),
]

_READ_ONLY_POST_CSRF_EXEMPT_PATHS = {
    "/models/distinct",
    "/get_remote_data_select",
    "/get_remote_select",
}


async def _register_static_models(env: AppOzonEnv) -> None:
    for name, model_class in _STATIC_MODELS:
        await env.orm.add_static_model(name, model_class)


def _build_ozon_cfg(source_settings: Any = None) -> dict:
    effective_settings = _effective_settings(source_settings)
    cfg = effective_settings.ozon_env_cfg()
    cfg.update({
        "keycloak_jwks_url": effective_settings.keycloak_jwks_url,
        "keycloak_issuer": effective_settings.keycloak_issuer,
        "oauth_url": effective_settings.keycloak_token_endpoint,
        "client_id": effective_settings.keycloak_client_id,
        "client_secret": effective_settings.keycloak_client_secret,
    })
    return cfg


def _effective_settings(source_settings: Any = None) -> Any:
    return source_settings or settings


def _clone_settings_with_app_code(
    source_settings: Any,
    app_code: str,
) -> Any:
    effective_settings = _effective_settings(source_settings)
    normalized_app_code = str(app_code or "").strip()
    current_app_code = str(
        getattr(effective_settings, "app_code", "") or ""
    ).strip()
    if not normalized_app_code or normalized_app_code == current_app_code:
        return effective_settings
    if hasattr(effective_settings, "model_copy"):
        return effective_settings.model_copy(
            update={"app_code": normalized_app_code}
        )
    cloned_settings = copy(effective_settings)
    setattr(cloned_settings, "app_code", normalized_app_code)
    return cloned_settings


def _resolve_request_app_code(
    request: Request,
    source_settings: Any = None,
) -> str:
    effective_settings = _effective_settings(source_settings)
    query_app_code = str(
        request.query_params.get("app_code", "") or ""
    ).strip()
    if query_app_code:
        return query_app_code
    cookie_app_code = str(request.cookies.get("app_code", "") or "").strip()
    if cookie_app_code:
        return cookie_app_code
    return str(getattr(effective_settings, "app_code", "") or "").strip()


def _effective_request_settings(
    request: Request,
    source_settings: Any = None,
) -> Any:
    app_code = _resolve_request_app_code(request, source_settings)
    return _clone_settings_with_app_code(source_settings, app_code)


def _current_env_app_code(
    env: AppOzonEnv,
    source_settings: Any = None,
) -> str:
    runtime_settings = getattr(getattr(env, "orm", None), "app_settings", None)
    for candidate in (
        getattr(runtime_settings, "app_code", ""),
        getattr(getattr(env, "user_session", None), "app_code", ""),
        getattr(_effective_settings(source_settings), "app_code", ""),
    ):
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized
    return ""


def _model_to_dict(record: Any) -> dict[str, Any]:
    if record is None:
        return {}
    if isinstance(record, dict):
        return record.copy()
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="python")
    if hasattr(record, "dict"):
        return record.dict()
    if hasattr(record, "get_dict"):
        return record.get_dict()
    return {}


def _normalize_app_settings_record(
    record: Any,
    app_code: str,
) -> dict[str, Any]:
    data = _model_to_dict(record)
    if not data:
        return {}
    data.pop("_id", None)
    record_name = str(data.get("rec_name") or "").strip()
    record_app_code = str(data.get("app_code") or "").strip()
    if record_name != app_code and record_app_code != app_code:
        return {}
    data.setdefault("rec_name", app_code)
    data["app_code"] = record_app_code or app_code
    return data


def _get_settings_model(env: AppOzonEnv) -> Any:
    settings_model = env.get("settings")
    if settings_model is None:
        raise RuntimeError("settings model not available")
    return settings_model


async def _read_settings_model_record(
    settings_model: Any,
    app_code: str,
) -> dict[str, Any]:
    if hasattr(settings_model, "load"):
        for query in ({"app_code": app_code}, {"rec_name": app_code}):
            record = await settings_model.load(query)
            if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
                continue
            data = _normalize_app_settings_record(record, app_code)
            if data:
                return data
    if hasattr(settings_model, "by_name"):
        record = await settings_model.by_name(app_code)
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            record = None
        data = _normalize_app_settings_record(record, app_code)
        if data:
            return data
        return {}
    raise RuntimeError("settings model does not support by_name/load")


async def _load_settings_model_object(
    settings_model: Any,
    app_code: str,
) -> Any:
    if hasattr(settings_model, "load"):
        for query in ({"app_code": app_code}, {"rec_name": app_code}):
            record = await settings_model.load(query)
            if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
                continue
            if _normalize_app_settings_record(record, app_code):
                return record
    if hasattr(settings_model, "by_name"):
        record = await settings_model.by_name(app_code)
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            record = None
        if _normalize_app_settings_record(record, app_code):
            return record
        return None
    raise RuntimeError("settings model does not support by_name/load")


async def _load_app_settings_record(
    env: AppOzonEnv,
    source_settings: Any = None,
) -> dict[str, Any]:
    effective_settings = _effective_settings(source_settings)
    app_code = str(getattr(effective_settings, "app_code", "") or "").strip()
    if not app_code:
        return {}

    settings_model = _get_settings_model(env)
    return await _read_settings_model_record(
        settings_model,
        app_code,
    )


async def _bootstrap_app_settings_record(
    env: AppOzonEnv,
    source_settings: Any = None,
) -> dict[str, Any]:
    effective_settings = _effective_settings(source_settings)
    payload = build_public_db_settings_payload(effective_settings)
    app_code = payload.get("rec_name", "")
    if not app_code:
        return {}

    settings_model = _get_settings_model(env)
    existing = await _read_settings_model_record(
        settings_model,
        app_code,
    )
    if existing:
        return existing

    record = await settings_model.new(data=payload)
    if record is None:
        raise RuntimeError("cannot build settings record for bootstrap")
    saved = await settings_model.insert(record)
    if saved is None:
        raise RuntimeError("cannot persist settings record for bootstrap")
    return _normalize_app_settings_record(
        saved,
        app_code,
    )


def _apply_runtime_app_settings(
    env: AppOzonEnv, runtime_settings: AppSettings
) -> None:
    env.orm.app_settings = runtime_settings
    if not getattr(env, "upload_folder", ""):
        env.upload_folder = getattr(runtime_settings, "upload_folder", "")
    for model in getattr(env, "models", {}).values():
        try:
            model.setting_app = runtime_settings
        except Exception:
            continue


async def _ensure_startup_identity_fields(
    env: AppOzonEnv,
    source_settings: Any = None,
) -> None:
    """Backfill admins from env into DB record if DB admins is empty.
    Called only at startup, not per-request — DB is authoritative for requests.
    """
    effective_settings = _effective_settings(source_settings)
    app_code = str(getattr(effective_settings, "app_code", "") or "").strip()
    if not app_code:
        return

    configured_admins = list(getattr(effective_settings, "admins", []) or [])
    if not configured_admins:
        return

    settings_model = _get_settings_model(env)
    record = await _load_settings_model_object(settings_model, app_code)
    if record is None:
        return

    current_admins = list(getattr(record, "admins", []) or [])
    if current_admins:
        return

    setattr(record, "admins", configured_admins)
    updated = await settings_model.update(record)
    if updated is None:
        raise RuntimeError("cannot persist admins backfill at startup")
    logger.info(
        "startup: backfilled admins from env app_code=%s admins=%s",
        app_code,
        configured_admins,
    )


async def _sync_runtime_app_settings(
    env: AppOzonEnv,
    source_settings: Any = None,
) -> None:
    effective_settings = _effective_settings(source_settings)
    record = await _load_app_settings_record(env, effective_settings)
    if not record:
        # No DB record yet — create from env so subsequent requests find it.
        record = await _bootstrap_app_settings_record(env, effective_settings)
    runtime_settings = merge_public_db_settings(effective_settings, record)
    _apply_runtime_app_settings(env, runtime_settings)


async def sync_app_settings_startup(source_settings: Any = None) -> None:
    effective_settings = _effective_settings(source_settings)
    cfg = _build_ozon_cfg(effective_settings)
    cfg["app_code"] = effective_settings.app_code
    if getattr(effective_settings, "mongo_url", ""):
        cfg["mongo_url"] = effective_settings.mongo_url
    if getattr(effective_settings, "mongo_db", ""):
        cfg["mongo_db"] = effective_settings.mongo_db
    if getattr(effective_settings, "mongo_user", ""):
        cfg["mongo_user"] = effective_settings.mongo_user
    if getattr(effective_settings, "mongo_pass", ""):
        cfg["mongo_pass"] = effective_settings.mongo_pass
    if getattr(effective_settings, "mongo_replica", "") is not None:
        cfg["mongo_replica"] = effective_settings.mongo_replica
    if getattr(effective_settings, "models_folder", ""):
        cfg["models_folder"] = str(effective_settings.models_folder)

    env = AppOzonEnv(cfg=cfg, cls_model=OzonModelApp)
    logger.info(
        "app settings startup sync start app_code=%s",
        effective_settings.app_code,
    )
    env_ready = False
    try:
        await env.init_env(local_model={"user":AppUser})
        env_ready = True
        # Startup-only: backfill admins from env if DB record has none.
        await _ensure_startup_identity_fields(env, effective_settings)
        await _sync_runtime_app_settings(env, effective_settings)
    finally:
        if env_ready:
            await env.close_env()
    logger.info(
        "app settings startup sync completed app_code=%s",
        effective_settings.app_code,
    )


async def get_ozon_env(request: Request) -> AsyncGenerator[AppOzonEnv, None]:
    effective_settings = _effective_request_settings(request)
    current_app_code = str(
        getattr(effective_settings, "app_code", "") or ""
    ).strip()
    if not current_app_code:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Missing APP_CODE configuration",
        )

    env = AppOzonEnv(
        cfg=_build_ozon_cfg(effective_settings),
        cls_model=OzonModelApp,
    )
    logger.info("ozon env init start app_code=%s", current_app_code)
    try:
        await env.init_env(local_model={"user":AppUser})
    except Exception as exc:
        logger.exception("ozon env init failed app_code=%s", current_app_code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OzonEnv init failed: {exc}",
        ) from exc
    try:
        try:
            await _sync_runtime_app_settings(env, effective_settings)
        except Exception:
            logger.exception(
                "app settings sync failed app_code=%s",
                current_app_code,
            )
            _apply_runtime_app_settings(env, effective_settings)
        logger.info("ozon env init completed app_code=%s", current_app_code)
        yield env
    finally:
        logger.info("ozon env closing app_code=%s", current_app_code)
        await env.close_env()


def _validate_csrf(request: Request) -> None:
    method = str(request.method or "").upper()
    if method not in {"POST", "PUT", "DELETE", "PATCH"}:
        return
    path = str(request.scope.get("path", "") or "")
    if method == "POST" and (
        path.startswith("/list/") or path in _READ_ONLY_POST_CSRF_EXEMPT_PATHS
    ):
        return
    csrf_cookie = request.cookies.get(settings.csrf_cookie_name, "")
    csrf_header = request.headers.get("X-CSRF-Token", "")
    if not csrf_cookie or not csrf_header or csrf_cookie != csrf_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed",
        )


def _extract_bearer(value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )
    raw = value.strip()
    if raw.lower().startswith("bearer "):
        raw = raw.split(" ", 1)[1].strip()
    return raw


async def get_authed_env(
    token_header_value: Annotated[str | None, Security(api_key_header)],
    ozon_env: Annotated[AppOzonEnv, Depends(get_ozon_env)],
    request: Request,
    response: Response,
) -> AppOzonEnv:
    await _register_static_models(ozon_env)

    cookie_val = request.cookies.get(settings.auth_cookie_name, "")
    if cookie_val:
        _validate_csrf(request)
        token = verify_token(
            cookie_val, settings.session_secret, settings.auth_cookie_max_age
        )
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session cookie",
            )
    else:
        token = _extract_bearer(token_header_value)

    params = dict(ozon_env.params) if isinstance(ozon_env.params, dict) else {}
    params["current_token"] = token
    params.pop("ozon_admin_token", None)
    ozon_env.params = params
    if isinstance(getattr(ozon_env, "config_system", None), dict):
        ozon_env.config_system.pop("ozon_admin_token", None)

    try:
        result = await ozon_env.session_app()
    except (TokenExpiredError, TokenRefreshError, TokenVerificationError) as exc:
        logger.warning("session_app token error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc) or "Token expired or invalid",
        ) from exc
    if result.fail or not ozon_env.user_session:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=result.msg or "Invalid session",
        )

    # ozon-env session_app() builds a plain User without app_code and with
    # is_admin frozen at whatever was persisted in the `user` collection.
    # Patch the live session object in-memory (Service holds it by reference):
    #  - inject app_code from settings
    #  - re-evaluate is_admin against app_settings.admins (authoritative)
    session = ozon_env.user_session
    admins = _get_app_admins(ozon_env)
    session_uid = str(getattr(session, "uid", "") or "").strip()
    session_is_admin = session_uid in admins
    current_app_code = _current_env_app_code(ozon_env, settings)
    try:
        session.app_code = current_app_code
        session.is_admin = session_is_admin
    except Exception:
        logger.exception(
            "failed to patch session app_code/is_admin uid=%s", session_uid
        )
    logger.info(
        "session patched uid=%s app_code=%s is_admin=%s admins=%s",
        session_uid,
        current_app_code,
        session_is_admin,
        admins,
    )

    # BFF cookie mode: refresh cookie if ozon-env rotated tokens internally
    if cookie_val:
        fresh_token_data = getattr(ozon_env, "current_token_data", None)
        if isinstance(fresh_token_data, dict) and fresh_token_data.get("access_token"):
            response.set_cookie(
                key=settings.auth_cookie_name,
                value=sign_token(fresh_token_data, settings.session_secret),
                httponly=True,
                samesite=settings.auth_cookie_samesite,
                secure=settings.cookie_secure,
                max_age=settings.auth_cookie_max_age,
                path="/",
            )

    response.set_cookie(
        key="app_code", value=current_app_code, httponly=True, samesite="lax"
    )
    logger.info(
        "authed env ready app_code=%s uid=%s",
        current_app_code,
        ozon_env.user_session.uid,
    )
    return ozon_env


async def get_service(
    ozon_env: Annotated[AppOzonEnv, Depends(get_ozon_env)],
) -> Service:
    session = getattr(ozon_env, "user_session", None)
    logger.info(
        "get_service: building Service app_code=%s uid=%s is_admin=%s models=%d",
        _current_env_app_code(ozon_env, settings),
        getattr(session, "uid", None),
        getattr(session, "is_admin", None),
        len(getattr(ozon_env, "models", {}) or {}),
    )
    service = Service(ozon_env)
    logger.info(
        "get_service: Service ready app_code=%s",
        getattr(getattr(service, "session", None), "app_code", None),
    )
    return service
