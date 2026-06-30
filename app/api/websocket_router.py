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


def _extract_token(websocket: WebSocket) -> Any:
    """Token dalla query `token` (bearer) o dal cookie di sessione (BFF)."""
    cookie_val = websocket.cookies.get(settings.auth_cookie_name, "")
    if cookie_val:
        return verify_token(
            cookie_val, settings.session_secret, settings.auth_cookie_max_age
        )
    raw = str(websocket.query_params.get("token", "") or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw.split(" ", 1)[1].strip()
    return raw


def _allowed_origins() -> set[str]:
    raw = str(getattr(settings, "ws_allowed_origins", "") or "")
    return {o.strip() for o in raw.split(",") if o.strip()}


def _origin_allowed(websocket: WebSocket) -> bool:
    """Difesa CSWSH: se è configurata una allowlist, l'Origin dell'handshake
    deve combaciare. Se vuota, nessun controllo (vedi nota nel setting)."""
    allowlist = _allowed_origins()
    if not allowlist:
        return True
    origin = str(websocket.headers.get("origin", "") or "").strip()
    return origin in allowlist


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
    if not _origin_allowed(websocket):
        origin = websocket.headers.get("origin", "")
        logger.warning("ws origin rejected: %s", origin)
        await websocket.close(code=WS_POLICY_VIOLATION, reason="Origin not allowed")
        return
    token = _extract_token(websocket)
    app_code = _resolve_app_code(websocket)

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
