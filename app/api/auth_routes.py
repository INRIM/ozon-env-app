from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import RedirectResponse

from app.app_settings import EnvSettings
from app.app_settings import get_env_settings
from app.deps.app_env import get_ozon_env
from app.services.cookie_auth import make_csrf_token
from app.services.cookie_auth import sign_token
from app.services.cookie_auth import verify_token
from app.services.session_auth import build_keycloak_session_from_tokens
from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.auth import KeycloakAuthManager

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["auth"])


def _cookie_kwargs(settings: EnvSettings) -> dict:
    return dict(
        httponly=True,
        samesite=settings.auth_cookie_samesite,
        secure=settings.cookie_secure,
        max_age=settings.auth_cookie_max_age,
        path="/",
    )


@router.get("/login")
async def login(
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
) -> RedirectResponse:
    state = make_csrf_token()
    signed_state = sign_token(state, settings.session_secret)

    params = urlencode({
        "response_type": "code",
        "client_id": settings.keycloak_client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": "openid profile email",
        "state": state,
    })
    keycloak_url = f"{settings.keycloak_authorization_endpoint}?{params}"

    response = RedirectResponse(url=keycloak_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        key=settings.auth_state_cookie_name,
        value=signed_state,
        **{**_cookie_kwargs(settings), "max_age": 600},
    )
    logger.info("login redirect to keycloak")
    return response


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
    code: str = Query(...),
    state: str = Query(...),
) -> RedirectResponse:
    # Validate OAuth2 state (anti-CSRF)
    signed_state = request.cookies.get(settings.auth_state_cookie_name, "")
    stored_state = verify_token(signed_state, settings.session_secret, max_age=600)
    if not stored_state or stored_state != state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth2 state — possible CSRF attack",
        )

    token = await _exchange_code(settings, code)
    app_code = settings.app_code
    session = await build_keycloak_session_from_tokens(
        ozon_env=ozon_env,
        settings=settings,
        app_code=app_code,
        token=token
    )

    signed_session = sign_token(session.token, settings.session_secret)
    csrf = make_csrf_token()

    response = RedirectResponse(
        url=settings.post_login_redirect_url,
        status_code=status.HTTP_302_FOUND,
    )
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=signed_session,
        **_cookie_kwargs(settings),
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        **{**_cookie_kwargs(settings), "httponly": False},
    )
    response.delete_cookie(key=settings.auth_state_cookie_name, path="/")
    logger.info("auth callback: session created uid=%s", getattr(session, "uid", ""))
    return response


@router.get("/logout")
async def logout(
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
) -> RedirectResponse:
    params = urlencode({
        "client_id": settings.keycloak_client_id,
        "post_logout_redirect_uri": settings.logout_redirect_url,
    })
    keycloak_logout_url = f"{settings.keycloak_logout_endpoint}?{params}"

    response = RedirectResponse(url=keycloak_logout_url, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")
    logger.info("logout: cookies cleared")
    return response


async def _exchange_code(settings: EnvSettings, code: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            settings.keycloak_token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.keycloak_client_secret,
                "code": code,
                "redirect_uri": settings.redirect_uri,
            },
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Keycloak token exchange failed ({response.status_code})",
        )
    return response.json() if response.content else {}
