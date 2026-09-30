from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status

from api_gateway.cache import TTLCache
from api_gateway.config import (
    GatewayConfig,
    ResourceConfig,
    UpstreamConfig,
    load_upstreams,
)
from api_gateway.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger("api_gateway")


def _require_s2s(request: Request) -> None:
    """Auth service-to-service: header condiviso, confronto constant-time.

    Il gateway sta su rete docker interna e non pubblica porte, ma la rete
    e' condivisa con tutti i container dello stack: senza questo controllo
    qualunque service potrebbe usare le credenziali degli upstream.
    """

    config: GatewayConfig = request.app.state.config
    presented = request.headers.get(config.s2s_header, "")
    # Starlette decodifica gli header in latin-1: un byte non-ASCII nel
    # token darebbe TypeError in compare_digest (500 invece di 401). Il
    # confronto va fatto sui bytes.
    presented_raw = presented.encode("latin-1", errors="replace")
    expected_raw = config.s2s_token.encode("utf-8")
    if not presented or not hmac.compare_digest(presented_raw, expected_raw):
        logger.warning(
            "s2s auth fallita path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "?",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid service token",
        )


def create_app(
    config: GatewayConfig | None = None,
    *,
    upstreams: dict[str, UpstreamConfig] | None = None,
    clients: dict[str, UpstreamClient] | None = None,
) -> FastAPI:
    config = config or GatewayConfig.from_env()
    config.validate()
    if upstreams is None:
        upstreams = load_upstreams(config.config_path)
    clients = clients or {
        code: UpstreamClient(code, up, timeout=config.http_timeout)
        for code, up in upstreams.items()
    }
    caches = {code: TTLCache(up.cache_ttl) for code, up in upstreams.items()}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        for client in clients.values():
            await client.aclose()

    app = FastAPI(
        title="ozon-env-app API gateway",
        description=(
            "Gateway service-to-service verso le API esterne dichiarate in "
            "config (upstream/risorse). Risposte inoltrate come arrivano: "
            "la decodifica per le select la fa ozon-env-app."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.config = config

    def _resolve(upstream: str, resource: str) -> tuple[UpstreamClient, ResourceConfig]:
        up = upstreams.get(upstream)
        res = up.resources.get(resource) if up else None
        if up is None or res is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"risorsa sconosciuta: {upstream}/{resource}",
            )
        return clients[upstream], res

    async def _call(
        client: UpstreamClient,
        res: ResourceConfig,
        method: str,
        params: list[tuple[str, str]],
        body: Any = None,
    ) -> Any:
        try:
            return await client.request(
                res, method=method, params=params, body=body
            )
        except UpstreamError as exc:
            logger.warning("upstream non disponibile: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
            ) from exc
        except Exception as exc:  # rete giu', DNS, timeout
            logger.exception("errore upstream=%s path=%s", client.code, res.path)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"errore contattando {client.code}",
            ) from exc

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # Senza auth: la healthcheck del container non ha il token.
        return {"status": "ok"}

    @app.get("/", dependencies=[Depends(_require_s2s)])
    async def index() -> dict[str, Any]:
        return {
            "result": [
                {
                    "path": f"{code}/{name}",
                    "allow_post": res.allow_post,
                    "description": res.description,
                }
                for code, up in upstreams.items()
                for name, res in up.resources.items()
            ]
        }

    @app.get("/{upstream}/{resource}", dependencies=[Depends(_require_s2s)])
    async def get_resource(upstream: str, resource: str, request: Request) -> Any:
        client, res = _resolve(upstream, resource)
        params = sorted(request.query_params.multi_items())
        key = f"{resource}?{params}"
        return await caches[upstream].get_or_set(
            key, lambda: _call(client, res, "GET", params)
        )

    @app.post("/{upstream}/{resource}", dependencies=[Depends(_require_s2s)])
    async def post_resource(upstream: str, resource: str, request: Request) -> Any:
        client, res = _resolve(upstream, resource)
        if not res.allow_post:
            raise HTTPException(
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                detail=f"POST non abilitato su {upstream}/{resource}",
            )
        raw = await request.body()
        try:
            body = await request.json() if raw else None
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="body non JSON",
            ) from exc
        params = list(request.query_params.multi_items())
        return await _call(client, res, "POST", params, body)

    return app
