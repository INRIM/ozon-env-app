from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import WebSocket
from fastapi import WebSocketDisconnect

from app.app_settings import get_env_settings
from app.deps.app_env import WsAuthError
from app.deps.app_env import build_authed_env_from_token
from app.services.cookie_auth import verify_token
from app.services.service import Service

logger = logging.getLogger("uvicorn.error")
router = APIRouter()
settings = get_env_settings()

# Close code WS per policy violation (auth/origin).
WS_POLICY_VIOLATION = 1008
# Tempo massimo di attesa del messaggio di auth dopo l'handshake, per i
# client senza cookie (vedi _authenticate_handshake). Evita che una
# connessione accettata ma mai autenticata resti aperta indefinitamente.
WS_AUTH_TIMEOUT_SECONDS = 10.0


@dataclass
class WsActionResult:
    payload: Any
    next_action_url: str = ""


class WsActionRunner:
    """Esegue l'azione richiesta via WS. Iniettabile (Depends) per i test."""

    async def authenticate(self, token: Any, app_code: str) -> str:
        """Valida il token; ritorna lo uid. Solleva WsAuthError se invalido."""
        env = await build_authed_env_from_token(token, app_code)
        try:
            return str(getattr(env.user_session, "uid", "") or "")
        finally:
            await env.close_env()

    async def run(
        self,
        token: Any,
        app_code: str,
        action_name: str,
        rec_name: str,
        data: dict[str, Any],
    ) -> WsActionResult:
        """Costruisce un env autenticato per la singola azione (isolato, come
        una request HTTP), esegue l'action POST, risolve la next_action_url e
        chiude l'env."""
        env = await build_authed_env_from_token(token, app_code)
        try:
            service = Service(env)
            payload = await service.service_handle_action_post(
                action_name=action_name,
                data=data,
                rec_name=rec_name,
            )
            next_url = ""
            # next_action_url solo se l'azione non e' fallita (come fa la UI).
            if _action_status(payload)[0] != "error":
                try:
                    next_url = await service.service_get_next_action_redirect(
                        curr_action=action_name,
                        rec_name=rec_name,
                    ) or ""
                except Exception:
                    logger.exception(
                        "ws next_action resolve failed action=%s", action_name
                    )
            return WsActionResult(payload=payload, next_action_url=next_url)
        finally:
            await env.close_env()


def get_ws_action_runner() -> WsActionRunner:
    return WsActionRunner()


class ConnectionManager:
    """Traccia le connessioni WS attive per uid (push futuri / cleanup)."""

    def __init__(self) -> None:
        self._by_uid: dict[str, set[WebSocket]] = {}

    def add(self, uid: str, websocket: WebSocket) -> None:
        self._by_uid.setdefault(uid, set()).add(websocket)

    def remove(self, uid: str, websocket: WebSocket) -> None:
        conns = self._by_uid.get(uid)
        if not conns:
            return
        conns.discard(websocket)
        if not conns:
            self._by_uid.pop(uid, None)


manager = ConnectionManager()


def _parse_bearer(raw: Any) -> str:
    value = str(raw or "").strip()
    if value.lower().startswith("bearer "):
        value = value.split(" ", 1)[1].strip()
    return value


async def _authenticate_handshake(websocket: WebSocket) -> Any:
    """Token dal cookie di sessione (BFF) se presente, altrimenti dal
    PRIMO messaggio WS (client bearer-only) — mai dalla query string.

    Un token in `?token=...` finisce in log di proxy/load balancer,
    access log applicativi, cronologia browser (vedi
    docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md, finding #8): la query
    string non e' un canale sicuro per una credenziale. Il client
    bearer-only si connette senza token nell'URL e manda come primo
    messaggio `{"type": "auth", "token": "<bearer>"}` prima di qualunque
    action_name; un timeout chiude la connessione se non arriva.
    """
    cookie_val = websocket.cookies.get(settings.auth_cookie_name, "")
    if cookie_val:
        return verify_token(
            cookie_val, settings.session_secret, settings.auth_cookie_max_age
        )
    try:
        message = await asyncio.wait_for(
            websocket.receive_json(), timeout=WS_AUTH_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if not isinstance(message, dict) or message.get("type") != "auth":
        return None
    return _parse_bearer(message.get("token"))


def _allowed_origins() -> set[str]:
    """Allowlist Origin per l'handshake WS.

    Se `WS_ALLOWED_ORIGINS` non e' configurata si ripiega su
    `EXTERNAL_BASE_URL`, cioe' l'origin da cui l'app e' servita: e' il
    solo Origin legittimo per un client che si autentica col cookie di
    sessione. Prima il fallback era "nessun controllo".
    """
    raw = str(getattr(settings, "ws_allowed_origins", "") or "")
    configured = {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}
    if configured:
        return configured
    external = str(getattr(settings, "external_base_url", "") or "").strip()
    return {external.rstrip("/")} if external else set()


def _origin_allowed(websocket: WebSocket, *, cookie_auth: bool) -> bool:
    """Difesa CSWSH.

    Il controllo si applica solo alle connessioni autenticate col cookie:
    sono le uniche che il browser puo' aprire per conto dell'utente da una
    pagina di terze parti. I client bearer-only (worker, CLI, mobile) non
    hanno un Origin e non sono soggetti al problema, quindi passano.
    """
    if not cookie_auth:
        return True
    allowlist = _allowed_origins()
    if not allowlist:
        logger.error(
            "ws origin allowlist vuota e EXTERNAL_BASE_URL non impostata: "
            "handshake con cookie rifiutato. Configurare WS_ALLOWED_ORIGINS."
        )
        return False
    origin = str(websocket.headers.get("origin", "") or "").strip().rstrip("/")
    if origin in allowlist:
        return True
    logger.warning(
        "ws origin '%s' non in allowlist %s (EXTERNAL_BASE_URL/"
        "WS_ALLOWED_ORIGINS)",
        origin,
        sorted(allowlist),
    )
    return False


def _resolve_app_code(websocket: WebSocket) -> str:
    query_app_code = str(
        websocket.query_params.get("app_code", "") or ""
    ).strip()
    if query_app_code:
        return query_app_code
    cookie_app_code = str(websocket.cookies.get("app_code", "") or "").strip()
    if cookie_app_code:
        return cookie_app_code
    return str(getattr(settings, "app_code", "") or "").strip()


def _action_status(payload: Any) -> tuple[str, str]:
    """Deriva (status, message) dal ResponseObjectData dell'azione."""
    data = getattr(payload, "data", None)
    if isinstance(data, dict):
        status_value = str(data.get("status", "")).lower()
        if status_value == "error" or bool(data.get("fail", False)):
            message = str(data.get("message", "") or data.get("msg", ""))
            return "error", message
    return "completed", ""


def _serialize(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    return payload


@router.websocket("/ws/actions")
async def ws_actions(
    websocket: WebSocket,
    runner: Annotated[WsActionRunner, Depends(get_ws_action_runner)],
) -> None:
    await websocket.accept()
    # Il cookie di sessione e' cio' che un sito ostile puo' far allegare
    # dal browser: solo quelle connessioni vanno filtrate per Origin.
    cookie_auth = bool(websocket.cookies.get(settings.auth_cookie_name, ""))
    if not _origin_allowed(websocket, cookie_auth=cookie_auth):
        origin = websocket.headers.get("origin", "")
        logger.warning("ws origin rejected: %s", origin)
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Origin not allowed")
        return
    app_code = _resolve_app_code(websocket)
    token = await _authenticate_handshake(websocket)
    if not token:
        logger.warning("ws auth handshake failed: no/invalid token")
        await websocket.close(
            code=WS_POLICY_VIOLATION, reason="Missing or invalid auth"
        )
        return

    try:
        uid = await runner.authenticate(token, app_code)
    except WsAuthError as exc:
        logger.warning("ws auth failed: %s", exc)
        await websocket.close(code=WS_POLICY_VIOLATION, reason=str(exc))
        return

    manager.add(uid, websocket)
    tasks: set[asyncio.Task] = set()
    logger.info("ws connected uid=%s app_code=%s", uid, app_code)
    try:
        while True:
            message = await websocket.receive_json()
            request_id = str(message.get("request_id", "") or "")
            action_name = str(message.get("action_name", "") or "").strip()
            rec_name = str(message.get("rec_name", "") or "").strip()
            data = message.get("data") or {}
            if not action_name:
                await websocket.send_json(
                    {
                        "request_id": request_id,
                        "type": "action_status",
                        "status": "error",
                        "message": "action_name mancante",
                        "data": {},
                    }
                )
                continue

            # Presa in carico immediata: la UI blocca la form.
            await websocket.send_json(
                {
                    "request_id": request_id,
                    "type": "action_status",
                    "status": "running",
                    "message": "",
                    "data": {},
                }
            )

            task = asyncio.create_task(
                _execute_action(
                    websocket,
                    runner,
                    token,
                    app_code,
                    request_id,
                    action_name,
                    rec_name,
                    data,
                )
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except WebSocketDisconnect:
        logger.info("ws disconnected uid=%s", uid)
    finally:
        for task in tasks:
            task.cancel()
        manager.remove(uid, websocket)


async def _execute_action(
    websocket: WebSocket,
    runner: WsActionRunner,
    token: Any,
    app_code: str,
    request_id: str,
    action_name: str,
    rec_name: str,
    data: dict[str, Any],
) -> None:
    try:
        result = await runner.run(
            token, app_code, action_name, rec_name, data
        )
        status_value, message = _action_status(result.payload)
        body = {
            "request_id": request_id,
            "type": "action_status",
            "status": status_value,
            "message": message,
            "data": {
                "next_action_url": result.next_action_url,
                "result": _serialize(result.payload),
            },
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - errore -> notifica, non crash
        logger.exception("ws action failed name=%s", action_name)
        body = {
            "request_id": request_id,
            "type": "action_status",
            "status": "error",
            "message": str(exc),
            "data": {},
        }
    try:
        await websocket.send_json(body)
    except Exception:
        logger.warning(
            "ws send failed (client disconnesso?) request_id=%s", request_id
        )
