from __future__ import annotations

import logging
import os

import uvicorn

from api_gateway.app import create_app

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",  # nosec B104 - container interno, nessuna porta pubblicata
        port=int(os.getenv("API_GATEWAY_PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
