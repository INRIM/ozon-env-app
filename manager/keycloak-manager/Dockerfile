FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

RUN sed -i 's/http:/https:/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    sed -i 's/http:/https:/g' /etc/apt/sources.list 2>/dev/null || true && \
    apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# kc-provision arriva da git (github), come dep normale.
COPY pyproject.toml uv.lock README.md ./
COPY keycloak_manager /app/keycloak_manager

RUN pip install uv
RUN uv sync --no-dev --frozen

# Output kc-env.var in /out (montare come volume).
RUN adduser --system --uid 1001 --group app && \
    mkdir -p /out && chown -R app /app /out
VOLUME ["/out"]
USER app

CMD ["uv", "run", "python", "-m", "keycloak_manager"]
