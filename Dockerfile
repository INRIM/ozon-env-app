FROM python:3.14-slim

# Impostazioni di base per Python e uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

WORKDIR /app

# Forza apt a usare HTTPS (porta 443) invece di HTTP (porta 80)
# Sostituisce http: con https: nei file di sistema Debian vecchi e nuovi
RUN sed -i 's/http:/https:/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    sed -i 's/http:/https:/g' /etc/apt/sources.list 2>/dev/null || true && \
    apt-get update && \
    # ca-certificates assicura che il server accetti i certificati SSL di debian.org e github
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Copia i file di progetto
COPY pyproject.toml uv.lock README.md bootstrap.py ./

# Copia il codice sorgente
COPY app /app/app

# Installa uv
RUN pip install uv
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Lancia l'applicazione
CMD ["uv", "run", "python", "-m", "app.main"]