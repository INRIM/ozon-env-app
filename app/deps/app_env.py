from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

import logging
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import Security
from fastapi import status
from fastapi.security import APIKeyHeader
from ozonenv.OzonEnv import OzonEnv

from app.app_settings import get_env_settings
from app.core.OzonModelApp import OzonModelApp
from app.services.service import Service
from app.services.session_auth import AUTH_MODE_KEYCLOAK
from app.services.session_auth import AUTH_MODE_TOKEN
from app.services.session_auth import build_keycloak_session
from app.services.session_auth import ensure_sso_token_fresh
from app.services.session_auth import normalize_auth_mode
from app.services.session_auth import persist_session
from app.services.session_auth import session_to_app_session

logger = logging.getLogger("uvicorn.error")

settings = get_env_settings()
api_key_header = APIKeyHeader(name=settings.token_header, auto_error=False)


@dataclass(frozen=True)
class ClientSession:
    api_key: str
    session: object
    service:Service
    app_code:str


def _build_ozon_cfg() -> dict:
    return {
        "app_code": settings.app_code,
        "mongo_user": settings.mongo_user,
        "mongo_pass": settings.mongo_pass,
        "mongo_url": settings.mongo_url,
        "mongo_db": settings.mongo_db,
        "mongo_replica": settings.mongo_replica,
        "models_folder": settings.models_folder
    }


async def get_ozon_env() -> AsyncGenerator[OzonEnv, None]:
    from ozonenv.OzonEnv import OzonEnv

    if not settings.app_code:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Missing APP_CODE configuration",
        )

    # Bootstrap env with default config; request token is enforced later.
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
        logger.info("ozon env init completed app_code=%s", settings.app_code)
        yield env
    finally:
        logger.info("ozon env closing app_code=%s", settings.app_code)
        await env.close_env()


def _extract_bearer(value: str | None) -> str:
    if not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing token",
        )
    raw_value = value.strip()
    if raw_value.lower().startswith("bearer "):
        raw_value = raw_value.split(" ", 1)[1].strip()
    return raw_value


def _apply_dominant_request_token(ozon_env: OzonEnv, user_token: str) -> None:
    """Single-token mode: il token in request e l'unica sorgente auth valida."""
    params = ozon_env.params.copy() if isinstance(ozon_env.params, dict) else {}
    params["current_session_token"] = user_token
    params.pop("ozon_admin_token", None)
    ozon_env.params = params

    if isinstance(getattr(ozon_env, "config_system", None), dict):
        ozon_env.config_system.pop("ozon_admin_token", None)


async def client_session(
    token_header_value: Annotated[str | None, Security(api_key_header)],
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
    request: Request,
    response: Response,
) -> ClientSession:
    logger.info("client session request received")
    try:
        cfg = _build_ozon_cfg()
        app_code = cfg["app_code"]
        auth_mode = normalize_auth_mode(settings.auth_mode)

        if auth_mode == AUTH_MODE_TOKEN:
            session = await _token_client_session(
                token_header_value=token_header_value,
                ozon_env=ozon_env,
                app_code=app_code,
            )
        elif auth_mode == AUTH_MODE_KEYCLOAK:
            session = await build_keycloak_session(
                ozon_env=ozon_env,
                request=request,
                settings=settings,
                app_code=app_code,
            )
            session = await ensure_sso_token_fresh(
                ozon_env=ozon_env,
                settings=settings,
                session=session,
                refresh_margin_seconds=int(
                    getattr(settings, "sso_refresh_margin_seconds", 60) or 60
                ),
            )
            _apply_dominant_request_token(ozon_env, session.token)
        else:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Unsupported AUTH_MODE '{auth_mode}'",
            )

        service = Service(ozon_env)
        response.set_cookie(
            key="app_code", value=app_code, httponly=True, samesite="lax"
        )
        logger.info(
            "client session created app_code=%s auth_mode=%s",
            app_code,
            auth_mode,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("client_session error")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid session: {exc}",
        ) from exc
    return ClientSession(
        api_key=session.token, session=session,
        service=service, app_code=app_code
    )


async def _token_client_session(
    token_header_value: str | None,
    ozon_env: OzonEnv,
    app_code: str,
):
    user_token = _extract_bearer(token_header_value)
    _apply_dominant_request_token(ozon_env, user_token)
    auth_result = await ozon_env.session_app()
    if auth_result.fail or not ozon_env.user_session:
        logger.error("Invalid token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=auth_result.msg or "Invalid token",
        )

    original_app_code = getattr(ozon_env.user_session, "app_code", None)
    session = session_to_app_session(ozon_env.user_session, app_code)
    ozon_env.user_session = session
    ozon_env.session_token = session.token
    if not getattr(ozon_env, "upload_folder", ""):
        ozon_env.upload_folder = getattr(
            getattr(ozon_env.orm, "app_settings", None), "upload_folder", ""
        )

    if original_app_code != app_code:
        await persist_session(ozon_env, session)

    return session
