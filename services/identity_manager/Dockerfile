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

COPY pyproject.toml README.md ./
COPY identity_manager /app/identity_manager

RUN pip install uv
RUN uv sync --no-dev

# I modelli vengono auto-generati da ozon-env nel MODELS_FOLDER condiviso.
RUN adduser --system --uid 1001 --group app && chown -R app /app
USER app

CMD ["uv", "run", "python", "-m", "identity_manager.main"]
