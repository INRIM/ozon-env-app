from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("mcp_search")


class MissingAuthorizationError(RuntimeError):
    """Nessun Authorization header inoltrato dal client MCP."""


class OzonSearchGateway:
    """Thin proxy verso `POST /list/{model}?stream=false` di ozon-env-app.

    Non tocca mai Mongo direttamente e non ha una propria identita': ogni
    chiamata porta l'Authorization header del CHIAMANTE reale (bearer
    keycloak dell'utente in App A), cosi' la stessa sessione/ACL
    (model_groups_rule, record_rulse, field_acl_policy, query field-ACL
    gate) che si applicherebbe a quell'utente su App B si applica anche
    qui -- nessun bypass, nessuna scrittura, nessuna pipeline aggregate
    (l'endpoint sotto e' find-style, vedi docs/QUERY_FIELD_ACL_GATE.en.md
    in ozon-env-app).
    """

    def __init__(
        self,
        *,
        base_url: str,
        http_timeout: float,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._http = http_client or httpx.AsyncClient(
            base_url=base_url, timeout=http_timeout
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _envelope(resp: httpx.Response, *, context: str) -> dict[str, Any]:
        payload: dict[str, Any]
        try:
            payload = resp.json()
        except ValueError:
            payload = {"detail": resp.text}
        if resp.status_code >= 400:
            logger.info(
                "%s rejected status=%s detail=%s", context, resp.status_code, payload
            )
            return {
                "ok": False,
                "status_code": resp.status_code,
                "detail": payload.get("detail", payload),
            }
        return {"ok": True, "status_code": resp.status_code, "response": payload}

    async def find(
        self,
        *,
        authorization: str,
        model: str,
        query: dict[str, Any] | None = None,
        order: str = "",
        skip: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not authorization:
            raise MissingAuthorizationError(
                "no Authorization header forwarded by the MCP client"
            )
        resp = await self._http.post(
            f"/list/{model}",
            params={"stream": "false"},
            json={
                "query": query or {},
                "order": order,
                "skip": skip,
                "limit": limit,
            },
            headers={"Authorization": authorization},
        )
        return self._envelope(resp, context=f"find model={model}")

    async def list_models(self, *, authorization: str) -> dict[str, Any]:
        """`GET /models/distinct` di ozon-env-app -- stesso proxy pattern di
        `find`: identita' del chiamante, nessun filtro applicativo aggiunto
        qui. Nota: quell'endpoint NON e' scoped su model_group_access (vedi
        finding separato gia' segnalato) -- la lista puo' includere model
        che poi `find` rifiuta con 403 per quello stesso utente. E'
        comunque solo un elenco di nomi, non dati; il gate reale resta su
        `find`."""
        if not authorization:
            raise MissingAuthorizationError(
                "no Authorization header forwarded by the MCP client"
            )
        resp = await self._http.get(
            "/models/distinct",
            headers={"Authorization": authorization},
        )
        return self._envelope(resp, context="list_models")
