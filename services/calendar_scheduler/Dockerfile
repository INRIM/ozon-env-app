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

# kc-provision e ozon-env arrivano da git (github), come dep normali.
COPY pyproject.toml uv.lock README.md ./
COPY calendar_scheduler /app/calendar_scheduler

RUN pip install uv
RUN uv sync --no-dev --frozen

# Job persistenti su SQLite: la dir /data va montata come volume.
VOLUME ["/data"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD uv run python -c "from calendar_scheduler.config import SchedulerConfig; from calendar_scheduler.health import is_healthy; import sys; sys.exit(0 if is_healthy(SchedulerConfig.from_env().health_file, 180) else 1)"

CMD ["uv", "run", "python", "-m", "calendar_scheduler.main"]
