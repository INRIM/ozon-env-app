# Action Router (EN) - Client Guide

## Scope
This guide explains how to consume `/action/*` endpoints from the client, with focus on:
- app bootstrap (`layout`, `menu`, `dashboard`)
- menu payload structure
- action execution (`GET/POST/DELETE /action/{name}`)

## Router
- Prefix: `/action`
- Auth: `Authorization: Bearer <token>`
- Response model: `ResponseObject`

## Available endpoints
- `GET /action/menu`
- `GET /action/menu/{parent}`
- `GET /action/dashboard`
- `GET /action/dashboard/{parent}`
- `GET /action/layout`
- `GET /action/layout/{name}`
- `GET /action/next_action/{curr_action}`
- `GET /action/next_action/{curr_action}/{rec_name}`
- `GET /action/{name}`
- `GET /action/{name}/{rec_name}`
- `POST /action/{name}`
- `POST /action/{name}/{rec_name}`
- `DELETE /action/{name}/{rec_name}`

## Base contract (`ResponseObject`)

Envelope:

```json
{
  "fail": false,
  "message": "",
  "content": {
    "mode": "menu|card|layout|form|list|action",
    "data": {}
  }
}
```

`content` holds the `ResponseObjectData` payload.

## Payload contract (`ResponseObjectData`)

```json
{
  "mode": "menu|card|layout|form|list|action",
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
  "batch_size": 0,
  "total_count": 0
}
```

Client rule:
- always switch by `mode` (not by endpoint path)
- examples below mostly show `content` (inner payload)

## Action record: field roles
An action is a record in the `action` model. The client should be metadata-driven.

Relevant fields:
- `rec_name`: action identifier (mapped to `/action/{name}`)
- `model`: target business model
- `view_name`: optional UI schema override (not data model override)
- `mode`: expected output (`list`, `form`, ...)
- `action_type`: operation semantic (`menu`, `window`, `process_task`, `save`, `copy`, `delete`, ...)
- `component_type`: used for component actions
- `list_query`: base filter for list actions
- `action_root_path`: action route prefix (typically `/action`)
- `title`, `button_icon`, `builder_enabled`: UI metadata

Important note:
- `form_form_documento` (and `list_documento`, `submit_documento`, etc.) is a real `action.rec_name`.
- runtime behavior is driven by the action record loaded by `rec_name`.

## Why `model` is central
`model` drives backend behavior:
- target collection for read/write
- domain normalization (`get_domain`) for query/count
- card `number` computation for list buttons

Runtime rule:
- data model = always `model`
- UI schema = `view_name` when set, otherwise `model` schema

## Recommended client flow
1. `GET /action/layout` (or `/action/layout/{name}`) to bootstrap UI.
2. Render navigation from `response.content.data.menu`.
3. Load card view via `GET /action/dashboard` if needed.
4. Execute actions:
- read: `GET /action/{name}` or `GET /action/{name}/{rec_name}`
- write/execute: `POST /action/{name}` or `POST /action/{name}/{rec_name}`
- delete: `DELETE /action/{name}/{rec_name}`

## Dedicated server queries
### Menu
Default filters:
- `deleted = 0`
- `active = true`
- `apps` filter on `menu_group` using current `app_code`

### Layout
Query on `component` with `type = layout` and default filters:
- `deleted = 0`
- `active = true`

### Dashboard
Response is enforced as `mode = "card"`.

Card buttons come from two queries:
- menu buttons:
`action_type = menu` and `component_type in [form,resource,layout]`
- action buttons:
`action_type in [window,process_task]` and `component_type in [form,resource,layout]`

## Menu payload (`mode = "menu"`)
`data` is a list containing one object:
- dynamic key = menu group label
- value = buttons array

Example:

```json
{
  "mode": "menu",
  "data": [
    {
      "Sales": [
        {
          "model": "order",
          "key": "orders_list",
          "type": "button",
          "label": "Orders",
          "leftIcon": "it-list",
          "btn_action_type": false,
          "action_type": "window",
          "url_action": "/action/orders_list",
          "builder": false
        }
      ]
    }
  ]
}
```

## Dashboard payload (`mode = "card"`)
`data` is a cards array:

```json
{
  "mode": "card",
  "model": "action",
  "data": [
    {
      "model": "order",
      "group_id": "sales",
      "title": "Sales",
      "buttons": [
        {
          "model": "order",
          "icon": "it-list",
          "action_type": "window",
          "content": "/action/orders_list",
          "label": "Orders",
          "mode": "list",
          "number": 37
        }
      ]
    }
  ]
}
```

`number` notes:
- mainly relevant for `mode = list` buttons
- computed from action `list_query` + default filters + `_user_*` placeholders + model domain normalization

## Layout payload (`mode = "layout"`)

```json
{
  "mode": "layout",
  "data": {
    "layout": "main_layout",
    "schema": {},
    "menu": [],
    "settings": {
      "module_name": "MCI",
      "version": "1.0.0",
      "logo_img_url": "/static/logo.png"
    }
  }
}
```

## Generic action endpoints
### `GET /action/next_action/{curr_action}` and `GET /action/next_action/{curr_action}/{rec_name}`
- loads current action record (`curr_action`)
- reads and validates `next_action_name`
- when next action exists: JSON response
  - `mode = "redirect"`
  - `data.next_page = "/action/{next_action}"` (or `"/action/{next_action}/{rec_name}"` when `rec_name` is provided)
- when no valid next action exists: `204 No Content`

### `GET /action/{name}` and `GET /action/{name}/{rec_name}`
Query params:
- `query`: JSON string, default `"{}"`
- `order`
- `skip`
- `limit`

Specific rule for `mode = list` actions:
- base query:
  - if action has `list_query`: use it
  - otherwise use `list_query`/`query` from the `component` (`view_name` or `model`)
- base ordering:
  - if action has `list_order` (or `order`): use it
  - otherwise use `list_order`/`order` from the `component`
- runtime HTTP `query` is merged with base query
- runtime HTTP `order` (when provided) overrides base ordering

Typical result:
- `mode = list` for list actions
- `mode = form` for detail/form actions

Additional metadata in `response.fields`:
- `submit_action_name`
- `next_action_name` (only for `mode = form`, explicit alias of submit action)
- `abandon_action_name`
- `action_sequence` (`current_action`, `submit_action`, `submit_next_action`, `abandon_action`)

### `POST /action/{name}` and `POST /action/{name}/{rec_name}`
- body: free JSON payload

### `DELETE /action/{name}/{rec_name}`
- optional body: free JSON payload
- behavior: soft delete (`deleted = 1`)

## Error handling
- `422` when `query` is not valid JSON on `GET /action/{name}*`
- action not found: `mode = action` and `data.status = error`

## Practical patterns: `list_action` and `form_form`
`list_action` and `form_form` are naming conventions, not reserved keywords.

`list_action` typical config:
- `mode = list`
- `model = <target>`
- optional `list_query`

Client usage:
1. `GET /action/list_action?query={...}&order=...&skip=0&limit=50`
2. render table from `mode=list`, `data`, `columns`, `total_count`

`form_form` typical config:
- `mode = form`
- `model = <target>`

Client usage:
1. create: `GET /action/form_form`, then `POST /action/form_form`
2. edit: `GET /action/form_form/{rec_name}`, then `POST /action/form_form/{rec_name}`

Robust rule:
- do not rely on action name pattern
- rely on action metadata + `mode` + response payload
