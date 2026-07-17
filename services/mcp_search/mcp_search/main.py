from __future__ import annotations

import logging
import sys

from .config import SearchConfig
from .gateway import OzonSearchGateway
from .server import build_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp_search")


def main() -> None:
    config = SearchConfig.from_env()
    config.validate()

    gateway = OzonSearchGateway(
        base_url=config.ozon_base_url, http_timeout=config.http_timeout
    )
    mcp = build_server(config, gateway)

    logger.info(
        "mcp_search starting host=%s port=%s path=%s ozon_base_url=%s",
        config.mcp_host,
        config.mcp_port,
        config.mcp_path,
        config.ozon_base_url,
    )
    mcp.run(
        transport="http",
        host=config.mcp_host,
        port=config.mcp_port,
        path=config.mcp_path,
    )


if __name__ == "__main__":
    main()
