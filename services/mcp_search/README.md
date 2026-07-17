# mcp_search

MCP server con **un tool, read-only**: `find_records`, un proxy sottile
verso `POST /list/{model}?stream=false` di `ozon-env-app`, piu' una
**resource**, `ozon://models`, che elenca i model disponibili cosi'
l'agente sa quali `model` passare a `find_records` senza indovinare.

## Perche' esiste (contesto)

Un agente in App A deve poter cercare dati in App B (`ozon-env-app`) **a
nome dell'utente reale** che lo sta usando, non con un'identita' propria.
Questo servizio non ha una sessione ozon-env, non tocca Mongo, non ha un
account M2M: ogni chiamata al tool inoltra l'`Authorization` bearer (JWT
keycloak) del chiamante cosi' come arriva, e ozon-env-app applica la
stessa ACL (model/riga/campo) che si applicherebbe a quell'utente su
qualunque altro client autenticato.

Niente aggregate, niente `$merge`/`$out`/`$where`/`$expr`: l'endpoint sotto
e' find-style e valida `query`/`order` server-side (vedi
`docs/QUERY_FIELD_ACL_GATE.en.md` nel repo `ozon-env-app`). Questo servizio
non aggiunge una propria sanitizzazione perche' non ne ha bisogno -- il gate
vive gia' nel backend e si applica a chiunque chiami quell'endpoint,
compreso questo proxy.

## Design

- **Nessuna identita' propria.** Il tool legge l'header `Authorization` in
  arrivo sulla richiesta HTTP MCP (`fastmcp.server.dependencies.get_http_headers`)
  e lo rigira intatto verso `ozon-env-app`. Se manca, il tool fallisce
  esplicitamente (nessun fallback silenzioso su un token proprio).
- **Un solo tool.** `find_records(model, query, order, skip, limit)` --
  niente `write`/`delete`/`aggregate` esposti, per costruzione, non per
  filtro applicativo.
- **Una resource**, `ozon://models` -- catalogo dei nomi model (via
  `GET /models/distinct`, stesso inoltro del bearer del chiamante).
  Nota: quell'endpoint non e' scoped su `model_group_access` (stesso
  finding gia' segnalato su `Service.get_models`/`get_distinct`), quindi
  la lista NON e' pre-filtrata sui model che il chiamante puo' davvero
  interrogare -- e' solo un catalogo di nomi, il gate reale resta su
  `find_records`. Documentato nella description della resource stessa
  cosi' l'agente non lo assume come garanzia di accesso.
- **Transport HTTP** (non stdio): il chiamante e' un agente remoto in
  un'altra app, non un client desktop locale.

## Config (`service.env`)

Vedi `service.env.example`. Variabili:

- `MCP_SEARCH_OZON_BASE_URL` -- base URL di `ozon-env-app` (rete interna).
- `MCP_SEARCH_HOST` / `MCP_SEARCH_PORT` / `MCP_SEARCH_PATH` -- bind del
  server MCP stesso.
- `MCP_SEARCH_HTTP_TIMEOUT` -- timeout verso ozon-env-app.
- `MCP_SEARCH_DEFAULT_LIMIT` / `MCP_SEARCH_MAX_LIMIT` -- clamp sul `limit`
  richiesto dal chiamante MCP.

## Run locale

```bash
uv sync
uv run python -m mcp_search.main
```

## Test

```bash
uv run python -m pytest tests/
```

I test coprono `OzonSearchGateway` (mock HTTP via `httpx.MockTransport`) e
la business logic di tool e resource (`find_records_core`, `list_models_core`),
non il protocollo MCP end-to-end -- l'estrazione header via
`get_http_headers()` dipende da un vero contesto di richiesta HTTP fastmcp,
non riproducibile a basso costo in unit test (verificata a mano con un
handshake MCP reale su transport HTTP, vedi commit history).

## Docker

```bash
./run.sh
```

Vedi `manifest.json` per network/env richieste dallo stack.
