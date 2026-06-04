# Action Router (IT) - Guida Client

## Scopo
Questa guida descrive come consumare gli endpoint `/action/*` lato client, con focus su:
- bootstrap dell'app (`layout`, `menu`, `dashboard`)
- struttura dati del menu
- esecuzione delle action (`GET/POST/DELETE /action/{name}`)

## Router
- Prefix: `/action`
- Auth: `Authorization: Bearer <token>`
- Response model: `ResponseObject` (sempre)

## Endpoint disponibili
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

## Contratto base (`ResponseObject`)
Envelope logico comune:

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

Il campo `content` contiene il payload `ResponseObjectData`.

Schema logico di `content`:

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

Regola client:
- fare sempre switch su `mode` (non sul path chiamato)
- negli esempi seguenti viene mostrato principalmente `content` (payload interno)

## Record action: funzione dei campi
L'action e un record nel model `action`. Il client non deve hardcodare logiche per endpoint specifici: deve leggere i metadati action e reagire.

Campi action rilevanti:
- `rec_name`: identificativo action (diventa `{name}` negli endpoint `/action/{name}`)
- `model`: model business target (es. `ordine`, `movimento`, `cliente`)
- `view_name`: override opzionale dello schema UI (non del model dati)
- `mode`: tipo output atteso (`list`, `form`, ecc.)
- `action_type`: semantica operativa (`menu`, `window`, `process_task`, `save`, `copy`, `delete`, ...)
- `component_type`: per action su `component` (es. `form`, `layout`, `resource`)
- `list_query`: filtro base per action di lista
- `action_root_path`: prefisso route action (normalmente `/action`)
- `title`: label UI
- `button_icon`: icona bottone
- `builder_enabled`: flag per UI builder

Come usarli lato client:
- usa `rec_name` per invocare endpoint action
- usa `title`/`button_icon` per rendering
- usa `mode` in risposta per scegliere componente UI (grid/form/card)
- non inferire il `model` dal nome action: e il backend a determinarlo dai metadati

Nota importante:
- `form_form_documento` (come `list_documento`, `submit_documento`, ecc.) e il `rec_name` di un record reale nel model `action`.
- il comportamento del sistema dipende dal record action caricato per `rec_name`, non da alias sintetici.

## Centralita del `model`
Il `model` e il centro del comportamento action:
- determina su quale collezione il backend legge/scrive
- determina il dominio effettivo (`get_domain`) usato per query e count
- determina il conteggio `number` nelle card (`mode=list`)

Regole backend principali:
- model dati = sempre `model`
- schema UI = `view_name` se valorizzato, altrimenti schema di `model`
- action lista:
  - `list_query` + query runtime + filtri di default (`deleted=0`, `active=true`)
- action form:
  - con `rec_name`: carico record specifico
  - senza `rec_name`: apro schema/contesto di creazione

Implicazione client:
- il client deve essere model-agnostic, guidato da `mode` + payload risposta
- la stessa UI lista/form puo riusare componenti comuni per modelli diversi

### Caso reale `model` + `view_name`
Esempio action:
- `rec_name = list_doc_beni_servizi`
- `mode = list`
- `model = documento`
- `view_name = documento_beni_servizi`

Comportamento atteso:
- dati lista letti dal model `documento`
- `response.model = documento`
- schema UI preso da `view_name` (`documento_beni_servizi`)

Stessa regola per form:
- `form_form_doc_bene_servizi` puo leggere/salvare su `model=documento`
- ma usare schema `view_name=documento_beni_servizi`

## Flusso consigliato client
1. `GET /action/layout` (o `/action/layout/{name}`) per bootstrap UI.
2. Render menu usando `response.content.data.menu`.
3. Se serve vista card: `GET /action/dashboard` o `/action/dashboard/{parent}`.
4. Su click di una action:
- `GET /action/{name}` o `GET /action/{name}/{rec_name}` per leggere.
- `POST /action/{name}` o `POST /action/{name}/{rec_name}` per salvare/eseguire.
- `DELETE /action/{name}/{rec_name}` per soft-delete.

## Query dedicate (server-side)
### Menu
Query dedicata con defaults:
- `deleted = 0` se non presente
- `active = true` se non presente
- filtro `apps` su `menu_group` basato su `app_code`

### Layout
Ricerca su `component` tipo `layout`, con defaults:
- `deleted = 0`
- `active = true`

### Dashboard
Risposta forzata con `mode = "card"`.

Le card usano due query distinte:
- menu button:
`action_type = "menu"` e `component_type in ["form","resource","layout"]`
- action button:
`action_type in ["window","process_task"]` e `component_type in ["form","resource","layout"]`

## Struttura menu (`mode = "menu"`)
Il payload `data` e una lista con un oggetto di gruppi:
- chiave dinamica = label gruppo menu
- valore = lista bottoni

Esempio:

```json
{
  "mode": "menu",
  "query": {
    "admin": true,
    "deleted": 0,
    "active": true
  },
  "data": [
    {
      "Vendite": [
        {
          "model": "ordine",
          "key": "ordini_list",
          "type": "button",
          "label": "Ordini",
          "leftIcon": "it-list",
          "btn_action_type": false,
          "action_type": "window",
          "url_action": "/action/ordini_list",
          "builder": false
        }
      ],
      "Magazzino": [
        {
          "model": "movimento",
          "key": "movimenti_list",
          "type": "button",
          "label": "Movimenti",
          "leftIcon": "it-folder",
          "btn_action_type": false,
          "action_type": "window",
          "url_action": "/action/movimenti_list",
          "builder": false
        }
      ]
    }
  ]
}
```

Note campi bottone menu:
- `url_action`: rotta da invocare lato client
- `btn_action_type`: metodo suggerito (`post` per save/copy/delete, altrimenti `false/null`)
- `action_type`: semantica action (`menu`, `window`, `process_task`, ecc.)

## Struttura dashboard (`mode = "card"`)
`data` contiene card di gruppo, ciascuna con bottoni:

```json
{
  "mode": "card",
  "model": "action",
  "query": {
    "parent": ""
  },
  "data": [
    {
      "model": "ordine",
      "group_id": "vendite",
      "title": "Vendite",
      "buttons": [
        {
          "model": "ordine",
          "icon": "it-list",
          "action_type": "window",
          "content": "/action/ordini_list",
          "label": "Ordini",
          "mode": "list",
          "number": 37
        },
        {
          "model": "ordine",
          "icon": "it-plus",
          "action_type": "menu",
          "content": "/action/ordine_new",
          "label": "Nuovo ordine",
          "mode": "form",
          "number": 0
        }
      ]
    }
  ]
}
```

Regole importanti su `number`:
- valorizzato soprattutto per bottoni con `mode = "list"`
- calcolato dal backend con:
  - `action.list_query` (se presente)
  - default query (`deleted = 0`, `active = true`)
  - risoluzione placeholder `_user_<campo_sessione>`
  - normalizzazione dominio del modello (`get_domain`)

## Struttura layout (`mode = "layout"`)

```json
{
  "mode": "layout",
  "query": {
    "type": "layout",
    "deleted": 0,
    "active": true
  },
  "data": {
    "layout": "main_layout",
    "schema": {
      "rec_name": "main_layout",
      "type": "layout",
      "components": []
    },
    "menu": [
      {
        "Vendite": []
      }
    ],
    "settings": {
      "module_name": "MCI",
      "version": "1.0.0",
      "logo_img_url": "/static/logo.png"
    }
  }
}
```

Uso client:
- `schema` per composizione pagina/layout
- `menu` per navigation
- `settings` per header/footer branding

## Endpoint action generiche
### `GET /action/next_action/{curr_action}` e `GET /action/next_action/{curr_action}/{rec_name}`
- carica il record action corrente (`curr_action`)
- legge e valida `next_action_name`
- se il next action esiste: risposta JSON
  - `mode = "redirect"`
  - `data.next_page = "/action/{next_action}"` (o `"/action/{next_action}/{rec_name}"` se `rec_name` presente)
- se non esiste un next action valido: `204 No Content`

### `GET /action/{name}` e `GET /action/{name}/{rec_name}`
Parametri query:
- `query`: stringa JSON (default `"{}"`)
- `order`: stringa ordinamento
- `skip`: int
- `limit`: int

Regola specifica per action `mode = "list"`:
- query base:
  - se action ha `list_query` valorizzata: usa quella
  - altrimenti usa `list_query`/`query` del `component` (`view_name` o `model`)
- ordinamento base:
  - se action ha `list_order` (o `order`) valorizzato: usa quello
  - altrimenti usa `list_order`/`order` del `component`
- query runtime (`query` HTTP) viene fusa con la query base
- `order` runtime (se passato) ha precedenza sull'ordinamento base

Ritorna tipicamente:
- `mode = "list"` per action di lista
- `mode = "form"` per action form/dettaglio

Metadati aggiunti in `response.fields`:
- `action_name`
- `action_model`
- `action_type`
- `component_type`
- `submit_action_name`
- `next_action_name` (solo `mode = "form"`, alias esplicito della submit action)
- `cancel_button` (solo `mode = "form"`, `true` se il form deve mostrare il pulsante abbandona; derivato da `component.no_cancel`)
- `abandon_action_name`
- `action_sequence`:
  - `current_action`
  - `submit_action`
  - `submit_next_action`
  - `abandon_action`

### `POST /action/{name}` e `POST /action/{name}/{rec_name}`
Body:
- JSON libero, usato come payload upsert/esecuzione action

### `DELETE /action/{name}/{rec_name}`
Body opzionale:
- JSON libero

Semantica:
- soft-delete via action (`deleted = 1`), non hard delete

## Pattern pratici: `list_action` e `form_form`
`list_action` e `form_form` non sono keyword speciali: sono nomi action (record `action.rec_name`).
Per funzionare, il relativo record action deve esistere.

### Pattern `list_action`
Configurazione tipica action:
- `rec_name = "list_action"`
- `mode = "list"`
- `model = "<model_target>"`
- `action_type = "window"`
- `list_query = {...}` (opzionale)

Uso client:
1. `GET /action/list_action?query={...}&order=...&skip=0&limit=50`
2. backend risponde `mode="list"` con `data`, `columns`, `total_count`
3. render tabella/paginazione

### Pattern `form_form`
Configurazione tipica action:
- `rec_name = "form_form"`
- `mode = "form"`
- `model = "<model_target>"`
- `action_type = "window"` o `save`

Uso client (create):
1. `GET /action/form_form`
2. backend risponde `mode="form"` con schema/contesto
3. `POST /action/form_form` con payload record

Uso client (edit):
1. `GET /action/form_form/{rec_name}`
2. backend risponde `mode="form"` con dati record
3. `POST /action/form_form/{rec_name}` con payload aggiornato

Regola robusta:
- il naming (`list_action`, `form_form`) puo cambiare per progetto
- la logica client deve basarsi su metadati action + `mode` + campi risposta
- per i bottoni form:
  - submit: usa `fields.submit_action_name`
  - abbandona: usa `fields.abandon_action_name`

## Error handling
- `422` se `query` non e JSON valido su endpoint `GET /action/{name}*`
- payload action non trovato: `mode = "action"` con `data.status = "error"`

## Checklist implementazione client
- chiamare sempre endpoint con prefix `/action`
- gestire rendering tramite `mode`
- per `mode=menu`: iterare oggetto gruppi (chiavi dinamiche)
- per `mode=card`: renderizzare `data[].buttons[]`
- usare `url_action`/`content` come destinazione navigazione/azione
- non assumere `number > 0`: puo essere `0` per action non-lista
