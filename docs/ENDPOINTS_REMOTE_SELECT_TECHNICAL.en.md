# API Technical Documentation (`app/api/routes.py`)

## General Context
- Router: `APIRouter(dependencies=[Depends(client_session)])`
- Authentication: all endpoints require the `Authorization: Bearer <token>` header.
- Application logger: `logger = logging.getLogger("uvicorn.error")`.
- Standard response format: `ResponseObject` (except NDJSON streams and `GET /get_session`).

## Standard Response Contract (`ResponseObject`)
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

## Endpoints in `routes`

### 1) `GET /`
- Purpose: health/liveness probe.
- Request body: none.
- Response: `{"status": "live"}`.

### 2) `GET /get_session`
- Purpose: returns the current session associated with the active token.
- Request body: none.
- Response: serialized session object (not `ResponseObject` envelope), for example:
```json
{
  "token": "....",
  "user_uid": "...",
  "app_code": "..."
}
```

### 3) `GET /models/distinct`
- Purpose: returns distinct `rec_name` values from the `component` model.
- Request body: none.
- Response: `ResponseObject` with `content.mode = "list"` and `content.data = ["model1", "model2", ...]`.

### 4) `GET /record/{model}`
- Purpose: returns the form schema (`component`) associated with the model.
- Path params:
  - `model` (`str`): model name.
- Response: `ResponseObject` with `content.mode = "form"`.

### 5) `POST /list/{model}`
- Purpose: returns records as NDJSON stream.
- Path params:
  - `model` (`str`): model name.
- Request body: `ListRequest`
  - `query` (`dict`, default `{}`)
  - `order` (`str`, default `""`)
    - supported formats:
      - `field:asc|desc` (native ozon-env format)
      - `-field` (equivalent to `field:desc`)
      - `+field` or `field` (equivalent to `field:asc`)
  - `skip` (`int`, default `0`)
  - `limit` (`int`, default `100`)
- Response:
  - Content-Type: `application/x-ndjson`
  - First packet: `ResponseObject` envelope (with `content.data` cleared)
  - Next packets: one record per line.
- Custom response headers:
  - `X-Order`
  - `X-Skip`
  - `X-Limit`
  - `X-columns`
  - `X-Total-Count` (total records matching query, not limited by page size)

### 6) `GET /record/{model}/{rec_name}`
- Purpose: loads a single record.
- Path params:
  - `model` (`str`)
  - `rec_name` (`str`)
- Response:
  - `200`: `ResponseObject` (`mode = "form"`)
  - `404`: `{"detail": "Record '<rec_name>' not found on model '<model>'"}`

### 7) `POST /record/{model}/{rec_name}`
- Purpose: upsert for the record identified by `rec_name`.
- Path params:
  - `model` (`str`)
  - `rec_name` (`str`)
- Request body: `dict[str, Any]` (free-form record payload).
- Response:
  - `200`: `ResponseObject` (`mode = "form"`)
  - `404`: record not found / invalid alignment.

### 8) `POST /models/distinct`
- Purpose: dual-use endpoint for model list or remote select options.
- Request body: `RemoteSelectRequest`.
- Logic:
  - if `payload.has_properties() == False`: returns model list (`get_models`).
  - if `payload.has_properties() == True` but `key` or `curr_model` is missing: falls back to model list.
  - otherwise: returns select options from `get_select_options`.
- Response: `ResponseObject` with `mode = "list"`.

### 9) `POST /get_remote_data_select`
### 10) `POST /get_remote_select` (alias)
- Purpose: returns remote select options.
- Request body: `RemoteSelectRequest`.
- Logic:
  - if `key` and `curr_model` are provided: uses FormIO configuration (`service.get_select_options`).
    Any remote endpoint is resolved **server-side** from the component
    (`app/services/formio.py` → `_load_remote_url_source`).
  - if `data.url` is provided without `key`/`curr_model`: **400**.
- otherwise: returns an empty list.
- Response: `ResponseObject` with `mode = "list"`.

> **Breaking change (security audit 2026-07).** The branch accepting
> `data.url` from the request body has been removed: it was an SSRF
> (arbitrary URL fetched server-side, response returned to the caller),
> and `data.headerValueKey` was passed to `get_global_param()` — which
> applies no ACL — allowing **any** `global_params` record's value to be
> sent as an HTTP header to a client-chosen host. Clients must pass
> `key` + `curr_model`: url, headers and token live on the component
> definition, not in the payload. These two paths are no longer exempt
> from CSRF validation.

---

## Action Router Integration (`app/api/action_router.py`)

Action/menu/layout/dashboard features were moved to the dedicated router with `/action` prefix.

Main endpoints:
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

Action router response format:
- `ResponseObjectData` (not `ResponseObject`)

Relation with `RemoteSelectRequest`:
- `RemoteSelectRequest` is used only by remote/select endpoints in `routes.py`.
- Action router endpoints do not use `RemoteSelectRequest`.
- Full action router details: `docs/ENDPOINTS_ACTION_ROUTER.it.md`.

---

# `RemoteSelectRequest` Complete Object (including sub-objects)

Defined in `app/services/common.py`.

## Main Schema
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

## Class Details

### `RemoteSelectRequest`
- `key: str = ""`
- `curr_model: str = ""`
- `data: RemoteSelectData`
- `properties: RemoteSelectProperties`
- Extra fields: allowed (`extra = "allow"`).
- Helper method: `has_properties()`
  - ignores empty values (`""`, `None`, `[]`, `{}`, `()`).
  - returns `True` only if at least one meaningful property exists.

### `RemoteSelectData`
- `url: str = ""`
- `path_value: str = ""` with JSON alias `pathValue`
- `headers: list[RemoteSelectHeaderEntry] = []`
- `header_key: str = ""` with JSON alias `headerKey`
- `header_value_key: str = ""` with JSON alias `headerValueKey`
- Extra fields: allowed.
- Supports both Python field names (`path_value`) and JSON aliases (`pathValue`).

### `RemoteSelectHeaderEntry`
- `key: str = ""`
- `value: str = ""`
- Extra fields: allowed.

### `RemoteSelectProperties`
- `model: str = ""`
- `domain: dict[str, Any] = {}`
- `compute_label: str = ""`
- `src: str = ""`
- `label: str = ""`
- `id: str = ""`
- Extra fields: allowed.

## Payload Examples

### A) Select from FormIO (internal schema)
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

### B) Select from remote URL
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
