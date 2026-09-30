FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Nessuna dipendenza da git privati (ozon-env / ozon-env-api): solo PyPI,
# quindi la build NON richiede VPN.
COPY pyproject.toml uv.lock README.md ./
COPY api_gateway /app/api_gateway
# Config (gateway.json + un file per upstream) SOLO da volume: non si
# cuoce nell'immagine la config di un deploy.
RUN mkdir -p /app/config

RUN pip install uv
RUN uv sync --no-dev --frozen

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

RUN adduser --system --uid 1001 --group app && chown -R app /app
USER app

CMD ["uv", "run", "python", "-m", "api_gateway.main"]
