FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Nessuna dipendenza da git privati (aioclamd/ozon-env-api ecc): solo
# fastmcp + httpx da PyPI, nessuna VPN richiesta per la build.
COPY pyproject.toml uv.lock README.md ./
COPY mcp_search /app/mcp_search

RUN pip install uv
RUN uv sync --no-dev --frozen

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8090), timeout=3).close()"

CMD ["uv", "run", "python", "-m", "mcp_search.main"]
