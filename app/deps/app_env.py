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

logger = logging.getLogger("uvicorn.error")

settings = get_env_settings()
api_key_header = APIKeyHeader(name=settings.token_header, auto_error=False)

_STATIC_MODELS = [
    ("mail_template", MailTemplate),
    ("field_acl_policy", FieldAclPolicy),
]
# model_groups_rule/model_fields_rule NON sono statici: hanno un
# component/form reale (con field type + tableView gia' configurati),
# quindi restano dynamic model normali (env.init_env/init_models li
# costruisce dal component, table_columns arriva da li' invece di essere
# duplicato in Python). Vedi ModelGroupsRule/ModelFieldsRule in
# app/core/models.py (usate solo per validare le righe di sync, non per
# la registrazione ORM).

# POST usate come "read" (la GET non basta: il filtro sta nel body).
# `/get_remote_data_select` e `/get_remote_select` NON sono piu' qui: da
# quando risolvono la config solo server-side restano letture, ma fanno
# comunque partire traffico HTTP in uscita dal server, quindi non hanno
# titolo per l'esenzione.
_READ_ONLY_POST_CSRF_EXEMPT_PATHS = {
    "/models/distinct",
}


async def _register_static_models(env: AppOzonEnv) -> None:
    for name, model_class in _STATIC_MODELS:
        # `env.init_env()` (init_models) puo' aver gia' registrato un model
        # dinamico per questo nome, rigenerato da un .py stale in
        # models_folder. `add_static_model` e' un no-op se il nome e' gia'
        # in `env.models`, quindi va rimosso prima per forzare la
        # registrazione della classe statica corretta, altrimenti il model
        # dinamico stale resta attivo per tutta la vita del processo.
        env.models.pop(name, None)
        await env.orm.add_static_model(name, model_class)
    # Set esplicito dei nomi VERAMENTE statici (per app.core.OzonEnvApp.
    # _RuntimeModelGuardMixin._is_app_static_model): non va confuso con
    # `env.orm.orm_static_models_map`, che ozon-env popola anche per
    # model dynamic con un .py cache in models_folder — quei model
    # devono poter rigenerarsi da un save del component, solo questi
    # (registrati qui sopra) restano fissi sulla classe Pydantic.
    env.orm.app_static_model_names = {name for name, _ in _STATIC_MODELS}


def _build_ozon_cfg(source_settings: Any = None) -> dict:
    effective_settings = _effective_settings(source_settings)
    cfg = effective_settings.ozon_env_cfg()
    cfg.update({
        "keycloak_jwks_url": effective_settings.keycloak_jwks_url,
        "keycloak_issuer": effective_settings.keycloak_issuer,
        "oauth_url": effective_settings.keycloak_token_endpoint,
        "client_id": effective_settings.keycloak_client_id,
        "client_secret": effective_settings.keycloak_client_secret,
        # Senza questo, KeycloakAuthSettings.from_config ripiegava sul
        # solo os.getenv("OZON_TOKEN_AUDIENCE") — mai valorizzato — e
        # `verify_aud` restava False: qualunque token del realm, anche
        # emesso per un altro client, veniva accettato. Vuoto = check
        # disattivo (comportamento invariato); valorizzare
        # OZON_TOKEN_AUDIENCE per attivarlo.
        "token_audience": getattr(
            effective_settings, "token_audience", ""
        ) or "",
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
    """Seed the 'admin' group_users record from env ADMINS, once, if this
    app_code has no admin group yet. Startup-only bridge for first deploy —
    after that, admin membership is managed exclusively via group_users
    (UI), not via setting_app.admins or env vars.
    """
    effective_settings = _effective_settings(source_settings)
    app_code = str(getattr(effective_settings, "app_code", "") or "").strip()
    if not app_code:
        return

    configured_admins = list(getattr(effective_settings, "admins", []) or [])
    if not configured_admins:
        return

    from app.ozon_env_acl import ADMIN_GROUP_NAME
    from app.ozon_env_acl import get_admin_uids

    if await get_admin_uids(env, app_code):
        return

    group_users_model = env.get("group_users")
    if group_users_model is None:
        return

    rec_name = f"{ADMIN_GROUP_NAME}-{app_code}"
    payload = {
        "rec_name": rec_name,
        "label": "Admin",
        "app_code": app_code,
        "group": ADMIN_GROUP_NAME,
        "users": configured_admins,
        "active": True,
        "deleted": 0,
        "default": False,
        "demo": False,
        "list_order": 1,
        "parent": "",
        "process_id": "",
        "process_task_id": "",
        "sys": False,
        "type": "form",
        "data_value": {
            "data_model": "group_users",
            "rec_name": rec_name,
            "label": "Admin",
        },
    }
    record = await group_users_model.new(data=payload)
    if record is None:
        raise RuntimeError("cannot build group_users admin seed record")
    saved = await group_users_model.insert(record)
    if saved is None:
        raise RuntimeError("cannot persist group_users admin seed record")
    logger.info(
        "startup: seeded admin group_users app_code=%s admins=%s",
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
        await _register_static_models(env)
        # Startup-only: seed admin group_users from env if none exists yet.
        await _ensure_startup_identity_fields(env, effective_settings)
        await _sync_runtime_app_settings(env, effective_settings)
        from app.ozon_env_acl.model_rules_sync import sync_all_model_rules

        await sync_all_model_rules(env)
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
    #  - groups/is_admin: sorgente unica group_users (app.ozon_env_acl), non
    #    piu' setting_app.admins ne' claim keycloak. Keycloak resta solo
    #    autenticazione.
    from app.services.session_auth import session_to_app_session
    session_uid = str(getattr(ozon_env.user_session, "uid", "") or "").strip()
    current_app_code = _current_env_app_code(ozon_env, settings)
    try:
        session = session_to_app_session(ozon_env.user_session, current_app_code)
        ozon_env.user_session = session
    except Exception:
        logger.exception("failed to convert session to AppSession uid=%s", session_uid)
        session = ozon_env.user_session

    from app.ozon_env_acl import apply_session_groups
    from app.ozon_env_acl import get_admin_uids

    try:
        groups = await apply_session_groups(ozon_env, session)
        logger.info("session groups uid=%s groups=%s", session_uid, groups)
    except Exception:
        logger.exception("failed to set session groups uid=%s", session_uid)

    try:
        admin_uids = await get_admin_uids(ozon_env, current_app_code)
    except Exception:
        logger.exception("failed to resolve admin group uid=%s", session_uid)
        admin_uids = []
    session_is_admin = session_uid in admin_uids
    try:
        session.is_admin = session_is_admin
    except Exception:
        logger.exception("failed to patch session is_admin uid=%s", session_uid)
    logger.info(
        "session patched uid=%s app_code=%s is_admin=%s admins=%s",
        session_uid,
        current_app_code,
        session_is_admin,
        admin_uids,
    )

    # ACL row-level: allowed_users al login (admin -> admins di default; altrimenti
    # dalle ACL). Vedi app/ozon_env_acl.apply_session_allowed_users.
    try:
        from app.ozon_env_acl import apply_session_allowed_users

        allowed_users = apply_session_allowed_users(session, admin_uids)
        logger.info(
            "session allowed_users uid=%s count=%d",
            session_uid,
            len(allowed_users),
        )
    except Exception:
        logger.exception(
            "failed to set allowed_users uid=%s", session_uid
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
        key="app_code", value=current_app_code, httponly=False, samesite="lax"
    )
    logger.info(
        "authed env ready app_code=%s uid=%s",
        current_app_code,
        ozon_env.user_session.uid,
    )
    return ozon_env


async def require_admin_env(
    ozon_env: Annotated[AppOzonEnv, Depends(get_authed_env)],
) -> AppOzonEnv:
    """Come `get_authed_env`, ma richiede anche `is_admin`.

    Serve per i router che scrivono configurazione di piattaforma senza
    passare da `Service.upsert` (quindi senza il gate `model_groups_rule`):
    li' `get_authed_env` da solo significa "qualunque utente autenticato",
    che non e' un'autorizzazione. `is_admin` e' gia' stato ricalcolato da
    `group_users` dentro `get_authed_env` — qui si legge soltanto.
    """
    session = getattr(ozon_env, "user_session", None)
    if not bool(getattr(session, "is_admin", False)):
        logger.warning(
            "admin-only endpoint denied uid=%s",
            getattr(session, "uid", ""),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return ozon_env


class WsAuthError(Exception):
    """Auth fallita in contesto WebSocket (niente HTTPException sul WS)."""


async def build_authed_env_from_token(
    token: Any,
    app_code: str = "",
) -> AppOzonEnv:
    """Costruisce un AppOzonEnv autenticato a partire da un token, fuori dal
    ciclo HTTP (usato dal router WebSocket).

    Replica il nucleo di `get_authed_env` senza Request/Response/CSRF: init env,
    sync settings, `current_token`, `session_app()`, patch di
    `app_code`/`is_admin`. In caso di fallimento chiude l'env e solleva
    `WsAuthError`. Il chiamante è responsabile di chiudere l'env restituito.
    """
    if not token:
        raise WsAuthError("Missing token")
    effective_settings = _clone_settings_with_app_code(settings, app_code)
    current_app_code = str(
        getattr(effective_settings, "app_code", "") or ""
    ).strip()
    if not current_app_code:
        raise WsAuthError("Missing APP_CODE configuration")

    env = AppOzonEnv(
        cfg=_build_ozon_cfg(effective_settings),
        cls_model=OzonModelApp,
    )
    await env.init_env(local_model={"user": AppUser})
    try:
        try:
            await _sync_runtime_app_settings(env, effective_settings)
        except Exception:
            logger.exception(
                "ws app settings sync failed app_code=%s", current_app_code
            )
            _apply_runtime_app_settings(env, effective_settings)
        await _register_static_models(env)

        params = dict(env.params) if isinstance(env.params, dict) else {}
        params["current_token"] = token
        params.pop("ozon_admin_token", None)
        env.params = params
        if isinstance(getattr(env, "config_system", None), dict):
            env.config_system.pop("ozon_admin_token", None)

        try:
            result = await env.session_app()
        except (
            TokenExpiredError,
            TokenRefreshError,
            TokenVerificationError,
        ) as exc:
            raise WsAuthError(str(exc) or "Token expired or invalid") from exc
        if result.fail or not env.user_session:
            raise WsAuthError(result.msg or "Invalid session")

        from app.services.session_auth import session_to_app_session
        session_uid = str(getattr(env.user_session, "uid", "") or "").strip()
        resolved_app_code = _current_env_app_code(env, settings)
        try:
            session = session_to_app_session(env.user_session, resolved_app_code)
            env.user_session = session
        except Exception:
            logger.exception(
                "failed to convert ws session to AppSession uid=%s", session_uid
            )
            session = env.user_session

        from app.ozon_env_acl import apply_session_allowed_users
        from app.ozon_env_acl import apply_session_groups
        from app.ozon_env_acl import get_admin_uids

        try:
            await apply_session_groups(env, session)
        except Exception:
            logger.exception("failed to set ws session groups uid=%s", session_uid)

        try:
            admin_uids = await get_admin_uids(env, resolved_app_code)
        except Exception:
            logger.exception(
                "failed to resolve ws admin group uid=%s", session_uid
            )
            admin_uids = []
        try:
            session.is_admin = session_uid in admin_uids
            apply_session_allowed_users(session, admin_uids)
        except Exception:
            logger.exception(
                "failed to patch ws session is_admin/allowed_users uid=%s",
                session_uid,
            )
        return env
    except Exception:
        await env.close_env()
        raise


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
