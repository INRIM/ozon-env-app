from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SearchConfig:
    """Config del server MCP. Nessuna identita' propria verso ozon-env-app:
    ogni tool call inoltra l'Authorization header del chiamante (vedi
    server.py) -- l'ACL applicata e' sempre quella del vero utente, non un
    account di servizio. Vedi docs/QUERY_FIELD_ACL_GATE.en.md nel repo
    ozon-env-app per il contratto query/order lato backend."""

    ozon_base_url: str
    http_timeout: float
    mcp_host: str
    mcp_port: int
    mcp_path: str
    default_limit: int
    max_limit: int

    @classmethod
    def from_env(cls) -> "SearchConfig":
        return cls(
            ozon_base_url=os.getenv(
                "MCP_SEARCH_OZON_BASE_URL", "http://ozon-env-app:8000"
            ).rstrip("/"),
            http_timeout=float(os.getenv("MCP_SEARCH_HTTP_TIMEOUT", "30")),
            mcp_host=os.getenv("MCP_SEARCH_HOST", "0.0.0.0"),
            mcp_port=int(os.getenv("MCP_SEARCH_PORT", "8090")),
            mcp_path=os.getenv("MCP_SEARCH_PATH", "/mcp"),
            default_limit=int(os.getenv("MCP_SEARCH_DEFAULT_LIMIT", "50")),
            max_limit=int(os.getenv("MCP_SEARCH_MAX_LIMIT", "200")),
        )

    def validate(self) -> None:
        if not self.ozon_base_url:
            raise ValueError("MCP_SEARCH_OZON_BASE_URL is required")
