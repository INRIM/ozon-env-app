# Documentazione Tecnica API (`app/api/routes.py`)

## Contesto generale
- Router: `APIRouter(dependencies=[Depends(client_session)])`
- Autenticazione: tutti gli endpoint richiedono header `Authorization: Bearer <token>`.
- Logger applicativo: `logger = logging.getLogger("uvicorn.error")`.
- Formato risposta standard: `ResponseObject` (eccetto stream NDJSON e `GET /get_session`).

## Contratto risposta standard (`ResponseObject`)
```json
{
  "content": {
    "mode": "form|list|list_stream",
    "data": {},
    "readable": true,
    "editable": true,
    "can_create": true,
    "model": "",
    "query": {},
    "obfucated_fields": [],
    "editable_fields": [],
    "schema": [],
    "rec_name": "",
    "fields": {},
    "columns": {},
    "filter_kyes": {},
    "batch_size": 0
  },
  "fail": false,
  "message": ""
}
```

## Endpoint in `routes`

### 1) `GET /`
- Scopo: health/liveness probe.
- Request body: nessuno.
- Response: `{"status": "live"}`.

### 2) `GET /get_session`
- Scopo: restituisce la sessione corrente associata al token attivo.
- Request body: nessuno.
- Response: oggetto sessione serializzato (non envelope `ResponseObject`), ad esempio:
```json
{
  "token": "....",
  "user_uid": "...",
  "app_code": "..."
}
```

### 3) `GET /models/distinct`
- Scopo: recupera i `rec_name` distinti del modello `component`.
- Request body: nessuno.
- Response: `ResponseObject` con `content.mode = "list"` e `content.data = ["model1", "model2", ...]`.

### 4) `GET /record/{model}`
- Scopo: recupera lo schema form (`component`) associato al modello.
- Path params:
  - `model` (`str`): nome modello.
- Response: `ResponseObject` con `content.mode = "form"`.

### 5) `POST /list/{model}`
- Scopo: lista record in streaming NDJSON.
- Path params:
  - `model` (`str`): nome modello.
- Request body: `ListRequest`
  - `query` (`dict`, default `{}`)
  - `order` (`str`, default `""`)
    - formati supportati:
      - `campo:asc|desc` (nativo ozon-env)
      - `-campo` (equivalente a `campo:desc`)
      - `+campo` o `campo` (equivalente a `campo:asc`)
  - `skip` (`int`, default `0`)
  - `limit` (`int`, default `100`)
- Response:
  - Content-Type: `application/x-ndjson`
  - Primo pacchetto: envelope `ResponseObject` (con `content.data` svuotato)
  - Pacchetti successivi: record riga per riga.
- Header custom risposta:
  - `X-Order`
  - `X-Skip`
  - `X-Limit`
  - `X-columns`
  - `X-Total-Count` (totale record matching la query, non limitato dalla pagina)

### 6) `GET /record/{model}/{rec_name}`
- Scopo: carica un record puntuale.
- Path params:
  - `model` (`str`)
  - `rec_name` (`str`)
- Response:
  - `200`: `ResponseObject` (`mode = "form"`)
  - `404`: `{"detail": "Record '<rec_name>' not found on model '<model>'"}`

### 7) `POST /record/{model}/{rec_name}`
- Scopo: upsert del record identificato da `rec_name`.
- Path params:
  - `model` (`str`)
  - `rec_name` (`str`)
- Request body: `dict[str, Any]` (payload libero del record).
- Response:
  - `200`: `ResponseObject` (`mode = "form"`)
  - `404`: record non trovato/allineamento non valido.

### 8) `POST /models/distinct`
- Scopo: endpoint dual-use per lista modelli o opzioni select remote.
- Request body: `RemoteSelectRequest`.
- Logica:
  - se `payload.has_properties() == False`: ritorna lista modelli (`get_models`).
  - se `payload.has_properties() == True` ma manca `key` o `curr_model`: fallback lista modelli.
  - altrimenti: ritorna opzioni select da `get_select_options`.
- Response: `ResponseObject` con `mode = "list"`.

### 9) `POST /get_remote_data_select`
### 10) `POST /get_remote_select` (alias)
- Scopo: recupera opzioni select remote.
- Request body: `RemoteSelectRequest`.
- Logica:
  - se presenti `key` e `curr_model`: usa configurazione FormIO (`service.get_select_options`).
    L'eventuale endpoint remoto viene risolto **server-side** dal component
    (`app/services/formio.py` → `_load_remote_url_source`).
  - se presente `data.url` senza `key`/`curr_model`: **400**.
  - altrimenti: ritorna lista vuota.
- Response: `ResponseObject` con `mode = "list"`.

> **Breaking change (audit sicurezza 2026-07).** Il ramo che accettava
> `data.url` dal body e' stato rimosso: era una SSRF (URL arbitrario
> fetchato dal server con la risposta restituita al chiamante) e
> `data.headerValueKey` finiva in `get_global_param()` — che non applica
> ACL — permettendo di far spedire il valore di **qualunque** record
> `global_params` come header HTTP verso un host scelto dal client.
> I client devono passare `key` + `curr_model`: url, header e token
> vivono sulla definizione del component, non nel payload.
> Questi due path non sono piu' esenti da CSRF.

---

## Integrazione con Action Router (`app/api/action_router.py`)

Le funzionalita di action/menu/layout/dashboard sono state spostate nel router con prefix `/action`.

Endpoint principali:
- `GET /action/menu`
- `GET /action/menu/{parent}`
- `GET /action/dashboard`
- `GET /action/dashboard/{parent}`
- `GET /action/layout`
- `GET /action/layout/{name}`
- `GET /action/{name}`
- `GET /action/{name}/{rec_name}`
- `POST /action/{name}`
- `POST /action/{name}/{rec_name}`
- `DELETE /action/{name}/{rec_name}`

Formato risposta Action Router:
- `ResponseObjectData` (non `ResponseObject`)

Relazione con `RemoteSelectRequest`:
- `RemoteSelectRequest` e usato solo dagli endpoint remote/select in `routes.py`.
- Gli endpoint dell'action router non usano `RemoteSelectRequest`.
- Per dettagli completi action router: `docs/ENDPOINTS_ACTION_ROUTER.it.md`.

---

# Oggetto `RemoteSelectRequest` completo (con sotto-oggetti)

Definito in `app/services/common.py`.

## Schema principale
```json
{
  "key": "string",
  "curr_model": "string",
  "data": {
    "url": "string",
    "pathValue": "string",
    "headers": [
      {
        "key": "string",
        "value": "string"
      }
    ],
    "headerKey": "string",
    "headerValueKey": "string"
  },
  "properties": {
    "model": "string",
    "domain": {},
    "compute_label": "string",
    "src": "string",
    "label": "string",
    "id": "string"
  }
}
```

## Dettaglio classi

### `RemoteSelectRequest`
- `key: str = ""`
- `curr_model: str = ""`
- `data: RemoteSelectData`
- `properties: RemoteSelectProperties`
- Extra fields: consentiti (`extra = "allow"`).
- Metodo helper: `has_properties()`
  - ignora valori vuoti (`""`, `None`, `[]`, `{}`, `()`).
  - ritorna `True` solo se esiste almeno una proprietà significativa.

### `RemoteSelectData`
- `url: str = ""`
- `path_value: str = ""` con alias JSON `pathValue`
- `headers: list[RemoteSelectHeaderEntry] = []`
- `header_key: str = ""` con alias JSON `headerKey`
- `header_value_key: str = ""` con alias JSON `headerValueKey`
- Extra fields: consentiti.
- Popolamento supportato sia con nome Python (`path_value`) che alias JSON (`pathValue`).

### `RemoteSelectHeaderEntry`
- `key: str = ""`
- `value: str = ""`
- Extra fields: consentiti.

### `RemoteSelectProperties`
- `model: str = ""`
- `domain: dict[str, Any] = {}`
- `compute_label: str = ""`
- `src: str = ""`
- `label: str = ""`
- `id: str = ""`
- Extra fields: consentiti.

## Esempi payload

### A) Select da FormIO (schema interno)
```json
{
  "key": "customer_id",
  "curr_model": "ordine",
  "data": {},
  "properties": {
    "src": "url"
  }
}
```

### B) Select da URL remoto
```json
{
  "key": "",
  "curr_model": "",
  "data": {
    "url": "https://api.example.org/options",
    "pathValue": "v1/select",
    "headers": [
      {
        "key": "X-API-KEY",
        "value": "my_global_param_name"
      }
    ]
  },
  "properties": {}
}
```
