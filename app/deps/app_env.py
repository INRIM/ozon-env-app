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
from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.auth import TokenExpiredError
from ozonenv.core.auth import TokenRefreshError
from ozonenv.core.auth import TokenVerificationError

from app.app_settings import build_public_db_settings_payload
from app.app_settings import get_env_settings
from app.app_settings import merge_public_db_settings
from app.core.OzonModelApp import OzonModelApp
from app.core.models import FieldAclPolicy
from app.core.models import MailTemplate
from app.services.cookie_auth import sign_token
from app.services.cookie_auth import verify_token
from app.services.service import Service

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


async def _register_static_models(env: OzonEnv) -> None:
    for name, model_class in _STATIC_MODELS:
        await env.orm.add_static_model(name, model_class)


def _build_ozon_cfg() -> dict:
    cfg = settings.ozon_env_cfg()
    cfg.update({
        "keycloak_jwks_url": settings.keycloak_jwks_url,
        "keycloak_issuer": settings.keycloak_issuer,
        "oauth_url": settings.keycloak_token_endpoint,
        "client_id": settings.keycloak_client_id,
        "client_secret": settings.keycloak_client_secret,
    })
    return cfg


def _effective_settings(source_settings: Any = None) -> Any:
    return source_settings or settings


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
    record_name = str(
        data.get("rec_name") or data.get("app_code") or ""
    ).strip()
    if record_name != app_code:
        return {}
    data.setdefault("rec_name", app_code)
    return data


def _get_settings_model(env: OzonEnv) -> Any:
    settings_model = env.get("settings")
    if settings_model is None:
        raise RuntimeError("settings model not available")
    return settings_model


async def _read_settings_model_record(
    settings_model: Any,
    app_code: str,
) -> dict[str, Any]:
    if hasattr(settings_model, "by_name"):
        record = await settings_model.by_name(app_code)
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            return {}
        return _normalize_app_settings_record(record, app_code)
    if hasattr(settings_model, "load"):
        record = await settings_model.load({"rec_name": app_code})
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            return {}
        return _normalize_app_settings_record(record, app_code)
    raise RuntimeError("settings model does not support by_name/load")


async def _load_settings_model_object(
    settings_model: Any,
    app_code: str,
) -> Any:
    if hasattr(settings_model, "by_name"):
        record = await settings_model.by_name(app_code)
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            return None
        return record
    if hasattr(settings_model, "load"):
        record = await settings_model.load({"rec_name": app_code})
        if bool(getattr(getattr(settings_model, "status", None), "fail", False)):
            return None
        return record
    raise RuntimeError("settings model does not support by_name/load")


async def _load_app_settings_record(
    env: OzonEnv,
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
    env: OzonEnv,
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


async def _ensure_settings_identity_fields(
    env: OzonEnv,
    source_settings: Any = None,
) -> None:
    effective_settings = _effective_settings(source_settings)
    app_code = str(getattr(effective_settings, "app_code", "") or "").strip()
    if not app_code:
        return

    settings_model = _get_settings_model(env)
    record = await _load_settings_model_object(settings_model, app_code)
    if record is None:
        return

    current_app_code = str(getattr(record, "app_code", "") or "").strip()
    current_admins = list(getattr(record, "admins", []) or [])
    configured_admins = list(getattr(effective_settings, "admins", []) or [])
    needs_update = False

    if current_app_code != app_code:
        setattr(record, "app_code", app_code)
        needs_update = True

    if configured_admins and not current_admins:
        setattr(record, "admins", configured_admins)
        needs_update = True

    if not needs_update:
        return

    updated = await settings_model.update(record)
    if updated is None:
        raise RuntimeError("cannot persist settings identity fields")


def _apply_runtime_app_settings(env: OzonEnv, runtime_settings: Any) -> None:
    env.orm.app_settings = runtime_settings
    if not getattr(env, "upload_folder", ""):
        env.upload_folder = getattr(runtime_settings, "upload_folder", "")
    for model in getattr(env, "models", {}).values():
        try:
            model.setting_app = runtime_settings
        except Exception:
            continue


async def _sync_runtime_app_settings(
    env: OzonEnv,
    source_settings: Any = None,
) -> None:
    effective_settings = _effective_settings(source_settings)
    record = await _load_app_settings_record(env, effective_settings)
    if not record:
        record = await _bootstrap_app_settings_record(env, effective_settings)
    else:
        await _ensure_settings_identity_fields(env, effective_settings)
        record = await _load_app_settings_record(env, effective_settings)
    runtime_settings = merge_public_db_settings(effective_settings, record)
    _apply_runtime_app_settings(env, runtime_settings)


async def sync_app_settings_startup(source_settings: Any = None) -> None:
    effective_settings = _effective_settings(source_settings)
    cfg = _build_ozon_cfg()
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

    env = OzonEnv(cfg=cfg, cls_model=OzonModelApp)
    logger.info(
        "app settings startup sync start app_code=%s",
        effective_settings.app_code,
    )
    env_ready = False
    try:
        await env.init_env()
        env_ready = True
        await _sync_runtime_app_settings(env, effective_settings)
    finally:
        if env_ready:
            await env.close_env()
    logger.info(
        "app settings startup sync completed app_code=%s",
        effective_settings.app_code,
    )


async def get_ozon_env() -> AsyncGenerator[OzonEnv, None]:
    if not settings.app_code:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Missing APP_CODE configuration",
        )

    env = OzonEnv(cfg=_build_ozon_cfg(), cls_model=OzonModelApp)
    logger.info("ozon env init start app_code=%s", settings.app_code)
    try:
        await env.init_env()
    except Exception as exc:
        logger.exception("ozon env init failed app_code=%s", settings.app_code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OzonEnv init failed: {exc}",
        ) from exc
    try:
        try:
            await _sync_runtime_app_settings(env)
        except Exception:
            logger.exception(
                "app settings sync failed app_code=%s",
                settings.app_code,
            )
            _apply_runtime_app_settings(env, settings)
        logger.info("ozon env init completed app_code=%s", settings.app_code)
        yield env
    finally:
        logger.info("ozon env closing app_code=%s", settings.app_code)
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
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
    request: Request,
    response: Response,
) -> OzonEnv:
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
        key="app_code", value=settings.app_code, httponly=True, samesite="lax"
    )
    logger.info(
        "authed env ready app_code=%s uid=%s",
        settings.app_code,
        ozon_env.user_session.uid,
    )
    return ozon_env


async def get_service(
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
) -> Service:
    return Service(ozon_env)
