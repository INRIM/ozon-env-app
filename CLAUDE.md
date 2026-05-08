# CLAUDE.md — ozon-env-app

## Architettura

FastAPI app che wrappa `ozon-env` ORM + `ozon-env-api` sidecar.

```
app/
  app.py              # FastAPI factory, lifespan, openapi customization
  main.py             # uvicorn entrypoint
  app_settings.py     # EnvSettings (pydantic-settings), letto da .env-local
  api/
    routes.py         # router principale
    action_router.py  # router azioni runtime
  core/
    OzonModelApp.py   # OzonModel override: session_model come attributo di istanza
    session.py        # AppSession(User) — DTO sessione, estende User di ozon-env
    models.py         # User, MailTemplate, AttachmentMetadata, FieldAclPolicy
  deps/
    app_env.py        # FastAPI dependencies: get_ozon_env, client_session, ClientSession
  services/
    session_auth.py   # Auth logic: token mode e keycloak mode, persist_user_session
    service.py        # Service wrapper
  base/               # Built-in plugin sempre caricato per primo (data + schema JSON)
  plugins/
    __init__.py       # discover_plugins(): carica base + /plugins/{APP_CODE,...} con topo-sort su depends
```

## Plugin system

- `app/base/` — plugin built-in embedded nell'immagine, sempre caricato per primo.
- `/plugins/` — folder nel container (volume mount). Ogni subdir con `config.json` è un plugin.
- `APP_CODE` plugin **deve** esistere in `/plugins/` (warning se assente).
- Ordine di caricamento: `base` → topo-sort su campo `depends` di ogni `config.json`.
- `PLUGINS_FOLDER` env var per override del path (default `/plugins`).

## Dipendenze chiave

- **ozon-env** `4.0.0` — da github (`https://github.com/inrim/ozon-env.git`). `Session` rimossa, `User` è il modello base.
- **ozon-env-api** — da gitlab interno (`gitlab.ininrim.it`), richiede **VPN** per `uv lock` e `docker build`.

## Sessioni e auth

- `AppSession` estende `User` (da `ozonenv.core.BaseModels`), NON è standalone.
- Persistenza sessioni su collection `user` (chiave `uid`), NON su `session`.
- Due auth mode: `token` (bearer interno) e `keycloak` (trusted-header da reverse proxy).
- `persist_user_session()` in `session_auth.py` scrive su `user` collection.

## Run test

```bash
uv run python -m pytest tests/
```

NON usare il `pytest` di sistema (Python 3.10). Sempre `uv run`.

## Build docker

Richiede VPN attiva per scaricare `ozon-env-api` da gitlab:

```bash
# 1. VPN attiva
uv lock                  # aggiorna uv.lock con ozon-env-api
./build_imges.sh         # usa --network host → accede gitlab via VPN
docker compose up
```

## Regole importanti

- `OzonModelApp.__init__`: `session_model` NON va passato a `super().__init__()` — va assegnato come `self.session_model = session_model` dopo la chiamata super (`OzonModel` non lo accetta).
- `[tool.uv.sources]` in `pyproject.toml`: usa git source per `ozon-env-api` (gitlab), NON path locale.
- Test pattern: `FakeOzonEnv` / `FakeCollection` con `find_one`/`replace_one` in memoria — no mock/monkeypatch sull'ORM.
- `uv.lock` è montato come volume in docker-compose — deve essere aggiornato prima del build.
