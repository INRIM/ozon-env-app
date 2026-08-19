from __future__ import annotations

import logging
from typing import Annotated
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse

from app.app_settings import EnvSettings
from app.app_settings import get_env_settings
from app.deps.app_env import get_ozon_env
from app.deps.app_env import register_static_models
from app.services.cookie_auth import make_csrf_token
from app.services.cookie_auth import session_cookie_max_age
from app.services.cookie_auth import sign_token
from app.services.cookie_auth import verify_token
from app.services.session_auth import build_keycloak_session_from_tokens
from app.services.session_revocation import BackchannelLogoutError
from app.services.session_revocation import purge_expired_revocations
from app.services.session_revocation import revoke_session
from app.services.session_revocation import verify_logout_token
from ozonenv.OzonEnv import OzonEnv

logger = logging.getLogger("uvicorn.error")

router = APIRouter(tags=["auth"])


def _cookie_kwargs(settings: EnvSettings, token: Any = None) -> dict:
    return dict(
        httponly=True,
        samesite=settings.auth_cookie_samesite,
        secure=settings.cookie_secure,
        # Il cookie non deve vivere piu' del refresh token che lo
        # sostiene, altrimenti il browser continua a mandare una
        # sessione che Keycloak ha gia' chiuso. Vedi
        # app.services.cookie_auth.refresh_token_max_age.
        max_age=session_cookie_max_age(settings.auth_cookie_max_age, token),
        path="/",
    )


def _set_session_cookies(
    response: Response,
    settings: EnvSettings,
    token: dict,
    csrf: str,
) -> None:
    """Sessione + CSRF con la stessa identica scadenza.

    Emetterli separatamente e' esattamente il bug che rendeva le POST
    403 dopo che il cookie CSRF (scadenza fissa dal login) moriva prima
    di quello di sessione (rinnovato ad ogni risposta).
    """
    kwargs = _cookie_kwargs(settings, token)
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=sign_token(token, settings.session_secret),
        **kwargs,
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        **{**kwargs, "httponly": False},
    )


def _clear_session_cookies(response: Response, settings: EnvSettings) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")


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

    response = RedirectResponse(
        url=settings.post_login_redirect_url,
        status_code=status.HTTP_302_FOUND,
    )
    # CSRF nuovo ad ogni login: e' una sessione nuova, il valore
    # eventualmente rimasto nel browser appartiene a quella precedente.
    _set_session_cookies(response, settings, session.token, make_csrf_token())
    response.delete_cookie(key=settings.auth_state_cookie_name, path="/")
    logger.info("auth callback: session created uid=%s", getattr(session, "uid", ""))
    return response


@router.get("/logout")
async def logout(
    request: Request,
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
) -> RedirectResponse:
    """RP-initiated logout.

    Due cose che prima non succedevano:

    1. Il refresh token viene revocato server-side PRIMA del redirect.
       Cancellare i cookie non tocca la sessione su Keycloak: se il
       browser non completava il redirect (tab chiusa, rete che cade),
       la sessione SSO restava viva e il refresh token — che era finito
       nel cookie — restava spendibile.
    2. Si passa `id_token_hint`. Senza, Keycloak >= 18 non sa quale
       sessione chiudere e mostra all'utente una pagina di conferma
       invece di sloggarlo: un logout che sembra rotto.
    """
    token = verify_token(
        request.cookies.get(settings.auth_cookie_name, ""),
        settings.session_secret,
        settings.auth_cookie_max_age,
    )
    id_token = ""
    if isinstance(token, dict):
        id_token = str(token.get("id_token") or "")
        await _revoke_refresh_token(
            settings, str(token.get("refresh_token") or "")
        )

    logout_params: dict[str, str] = {
        "post_logout_redirect_uri": settings.logout_redirect_absolute_url,
    }
    if id_token:
        logout_params["id_token_hint"] = id_token
    else:
        # Senza id_token_hint, `client_id` e' obbligatorio perche'
        # Keycloak possa validare post_logout_redirect_uri.
        logout_params["client_id"] = settings.keycloak_client_id
    keycloak_logout_url = (
        f"{settings.keycloak_logout_endpoint}?{urlencode(logout_params)}"
    )

    response = RedirectResponse(
        url=keycloak_logout_url, status_code=status.HTTP_302_FOUND
    )
    _clear_session_cookies(response, settings)
    logger.info("logout: cookies cleared, sso session revoked=%s", bool(id_token))
    return response


async def _revoke_refresh_token(settings: EnvSettings, refresh_token: str) -> None:
    """Chiude la sessione SSO su Keycloak. Best effort: un logout non deve
    fallire perche' Keycloak non risponde — i cookie vanno via comunque."""
    if not refresh_token:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                settings.keycloak_logout_endpoint_internal,
                data={
                    "client_id": settings.keycloak_client_id,
                    "client_secret": settings.keycloak_client_secret,
                    "refresh_token": refresh_token,
                },
            )
        if response.status_code >= 400:
            logger.warning(
                "keycloak sso session revoke failed status=%s",
                response.status_code,
            )
    except Exception as exc:
        # Si logga il TIPO dell'eccezione, non il suo testo: la richiesta
        # che l'ha generata porta client_secret e refresh token nel body,
        # e alcune eccezioni di trasporto ripetono la richiesta nel
        # messaggio.
        logger.warning(
            "keycloak sso session revoke error: %s", type(exc).__name__
        )


@router.api_route("/auth/refresh", methods=["GET", "POST"])
@router.api_route("/refresh", methods=["GET", "POST"])
async def auth_refresh(
    request: Request,
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
) -> JSONResponse:
    """Rinnova la sessione BFF dal solo cookie.

    Il client (`AUTH_REFRESH_PATH` nella config runtime del frontend)
    chiamava questo path e prendeva 404: nessun modo di rinnovare, e la
    sessione "spariva" senza spiegazione. Esposto su entrambi i path
    perche' il reverse proxy del web client li mappa tutti e due.

    Nessun controllo CSRF: l'unico effetto e' rinnovare i token della
    sessione gia' presente nel cookie: un attaccante cross-site puo'
    farlo scattare ma non puo' leggere la risposta ne' ottenere una
    sessione, e senza il cookie non ottiene nulla.
    """
    token = verify_token(
        request.cookies.get(settings.auth_cookie_name, ""),
        settings.session_secret,
        settings.auth_cookie_max_age,
    )
    refresh_token = ""
    if isinstance(token, dict):
        refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        return JSONResponse(
            {"status": "unauthenticated", "detail": "No session to refresh"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    async with httpx.AsyncClient(timeout=20.0) as client:
        kc_response = await client.post(
            settings.keycloak_token_endpoint,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.keycloak_client_id,
                "client_secret": settings.keycloak_client_secret,
                "refresh_token": refresh_token,
            },
        )

    if kc_response.status_code >= 400:
        error, description = _oauth_error(kc_response)
        logger.info(  # nosemgrep
            "session refresh rejected error=%s description=%s", error, description
        )
        # La sessione SSO non c'e' piu': i cookie vanno rimossi, altrimenti
        # il client continua a ritentare con credenziali morte.
        response = JSONResponse(
            {"status": "expired", "error": error, "login_url": "/login"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
        _clear_session_cookies(response, settings)
        return response

    fresh_token = kc_response.json() if kc_response.content else {}
    if not isinstance(fresh_token, dict) or not fresh_token.get("access_token"):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid refresh response from Keycloak",
        )
    # Keycloak non rimanda il refresh token se non ruota: tenere il vecchio.
    fresh_token.setdefault("refresh_token", refresh_token)
    fresh_token.setdefault("id_token", str(token.get("id_token") or ""))

    csrf = request.cookies.get(settings.csrf_cookie_name, "") or make_csrf_token()
    response = JSONResponse(
        {
            "status": "ok",
            "expires_in": fresh_token.get("expires_in", 0),
            "refresh_expires_in": fresh_token.get("refresh_expires_in", 0),
        }
    )
    _set_session_cookies(response, settings, fresh_token, csrf)
    return response


@router.post("/auth/backchannel-logout")
async def backchannel_logout(
    settings: Annotated[EnvSettings, Depends(get_env_settings)],
    ozon_env: Annotated[OzonEnv, Depends(get_ozon_env)],
    logout_token: str = Form(...),
) -> JSONResponse:
    """OIDC Back-Channel Logout: Keycloak notifica qui la fine di una
    sessione (logout utente, logout amministrativo, scadenza idle).

    Va registrato sul client Keycloak come `backchannel.logout.url` e
    deve essere raggiungibile DA Keycloak (rete interna), non dal
    browser. Nessun cookie, nessun CSRF: l'autenticazione e' la firma
    del `logout_token` stesso.
    """
    # `get_ozon_env` non registra i model statici (lo fa `get_authed_env`,
    # che qui non si puo' usare: la richiesta arriva da Keycloak, senza
    # sessione). Senza questa riga `env.get("revoked_session")` e' None.
    await register_static_models(ozon_env)

    try:
        claims = await verify_logout_token(logout_token, settings)
    except BackchannelLogoutError as exc:
        logger.warning("backchannel logout rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    await revoke_session(ozon_env, claims)
    try:
        await purge_expired_revocations(ozon_env)
    except Exception:
        # La purge e' manutenzione: se fallisce, la revoca resta valida.
        logger.exception("revocation purge failed")

    # Cache-Control obbligatorio da spec (§2.8).
    return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})


def _oauth_error(response: httpx.Response) -> tuple[str, str]:
    """Estrae `error`/`error_description` da una risposta OAuth2.

    Il corpo di errore di Keycloak non contiene segreti (sono codici
    standard RFC 6749 piu' una descrizione), quindi puo' essere loggato e
    restituito: e' l'unica cosa che distingue un redirect_uri sbagliato
    (`invalid_grant`) da un secret sbagliato (`invalid_client`) o da un
    client non abilitato al flusso (`unauthorized_client`).
    """
    try:
        payload = response.json()
    except ValueError:
        return "", response.text[:200]
    if not isinstance(payload, dict):
        return "", ""
    return (
        str(payload.get("error", "") or ""),
        str(payload.get("error_description", "") or "")[:200],
    )


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
        error, description = _oauth_error(response)
        # Endpoint e redirect_uri nel log: la causa piu' frequente e' che
        # non combaciano con quelli usati nella richiesta /authorize (es.
        # .env che punta a un realm o a un host keycloak diverso da
        # quello con cui l'utente ha fatto login). Senza questi due
        # valori il 502 non e' diagnosticabile. Nessun segreto: il
        # client_secret non viene mai loggato.
        logger.error(  # nosemgrep
            "keycloak token exchange failed status=%s error=%s "
            "description=%s token_endpoint=%s redirect_uri=%s client_id=%s",
            response.status_code,
            error,
            description,
            settings.keycloak_token_endpoint,
            settings.redirect_uri,
            settings.keycloak_client_id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Keycloak token exchange failed",
                "status": response.status_code,
                "error": error,
                "error_description": description,
            },
        )
    return response.json() if response.content else {}
