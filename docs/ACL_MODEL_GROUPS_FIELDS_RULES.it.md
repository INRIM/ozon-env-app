# ACL: model groups, fields rule, record rule

## Obiettivo

Documenta il motore ACL che governa: chi vede un model, chi vede/scrive
un campo, chi vede una riga specifica. Copre 3 livelli distinti che
condividono la stessa fonte di config (`component.properties`) ma
enforcement diverso.

## Attori e gruppi

### `groups` (collection)

Seed base in `app/base/data/groups.json`: `admin`, `user`, `operator`,
`manager`, `dpo`, `technical_operator`, `gdpr`. Ogni riga:

- `implied_groups`: gerarchia (es. `manager` implica `user`+`operator`,
  `dpo` implica `gdpr`), espansa a runtime (`_expand_implied_groups`).
- `rule`: query mongo (stringa JSON) opzionale — appartenenza dinamica
  al gruppo se il record `user` matcha la query, invece che via
  `group_users` (`_groups_from_rules`).

### `group_users` (collection)

Righe `(app_code, group, users[])`. Fonte primaria di membership.

### `apply_session_groups` (`app/ozon_env_acl/__init__.py`)

Chiamata ad ogni request (da `session_auth.py:persist_user_session`),
popola
`session.user.groups`/`session.groups` da zero ogni volta:

1. legge `group_users` per `app_code` corrente,
2. aggiunge gruppi da `groups.rule` match,
3. espande `implied_groups`,
4. `session.is_tech = "technical_operator" in groups`.

Sovrascrive sempre, anche a vuoto — necessario perche' `ozon-env`
puo' aver propagato `groups` dal JWT keycloak su un record `user`
nuovo (fallback in `OzonOrm.build_auth_user`): `group_users` resta
l'unica fonte effettiva.

### Admin

`is_admin` NON viene piu' da `setting_app.admins` ma da
`get_admin_uids()`: uid presenti in `group_users` per il gruppo
`admin`. Keycloak resta responsabile solo di autenticazione, non di
autorizzazione admin.

## Dove si configura l'ACL: `component.properties`

Ogni component (= model runtime) porta due chiavi JSON in
`properties`, editabili da form/builder:

- `models_groups` — permessi CRUD+export a livello di MODEL, per
  gruppo.
- `models_restricted_fields` — permessi a livello di CAMPO/RECORD.

`normalize_component_properties` (`app/core/OzonEnvApp.py`) fa
`setdefault` di entrambe le chiavi al save di un component NON
identity (vedi sotto), cosi ogni model nuovo parte con una baseline
sensata invece che aperto a tutti.

### `models_groups` — formato

```json
{
  "rules": [
    {"groups": ["admin"], "actions": {"read": true, "create": true, "update": true, "delete": true, "export": true}},
    {"groups": ["user"],  "actions": {"read": true, "create": false, "update": false, "delete": false, "export": false}}
  ]
}
```

Default iniettato (`_DEFAULT_MODELS_GROUPS_NON_SYS` /
`_DEFAULT_MODELS_GROUPS_SYS` in `OzonEnvApp.py`):

| model | admin | user | technical_operator | operator/manager/dpo |
|---|---|---|---|---|
| non-sys | full | read | read+export | read/create/update+export |
| sys | full | — | read/create/update+export | — |

Il formato `{"rules": [...]}` viene flattato in `model_groups_rule`
(vedi sotto, `model_rules_sync.py`) — fonte di verita' del gate CRUD
a livello di MODEL, enforced da `Service._get_model_group_access`
(vedi "Motore model-level" sotto). Il formato legacy (lista/CSV di
nomi gruppo) NON e' piu' supportato: e' stato retirato insieme a
`synth_policies_from_component_properties` (mai popolato dai default
correnti, solo il formato nuovo lo e'). Un component salvato in quel
formato non produce righe `model_groups_rule` — fail-closed, nega
tutto ai non-admin finche' non viene ri-salvato nel formato nuovo.

### `models_restricted_fields` — formato

```json
{
  "fields_rule": {
    "resticted_fields": ["codicefiscale", "iban"],
    "allowed_groups": [
      {"groups": ["gdpr"], "actions": {"read": true, "create": true, "update": true, "delete": false}},
      {"groups": ["dpo"],  "actions": {"read": true, "create": false, "update": false, "delete": false}}
    ]
  },
  "record_rules": [
    {
      "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
      "actions": {"read": true, "create": true, "update": true, "delete": true},
      "resticted_fields": []
    }
  ]
}
```

Nota i typo nelle chiavi (`resticted_fields`, `record_rules`): sono il
formato reale scritto dal builder/seed, non refusi da correggere —
tutto il codice li usa cosi as-is.

Default iniettato: `fields_rule` con `gdpr` (full tranne delete) e
`dpo` (solo read) su `resticted_fields: []`; `record_rules` con UNA
regola `owner_uid == user.uid` → full access sul proprio record.

## Le due tabelle flat: `model_groups_rule` / `model_fields_rule`

`app/ozon_env_acl/model_rules_sync.py`, chiamato da
`AppOzonEnv.insert_update_component` ad ogni save di component
(fail-soft: un errore qui non deve rompere il save):

```
sync_model_rules(env, schema)
  -> model_groups_rows(...)   # flatten models_groups.rules
  -> model_fields_rows(...)   # flatten models_restricted_fields
  -> delete_many + insert_many su model_groups_rule / model_fields_rule
```

Startup: `sync_all_model_rules(env)` rifà il flatten per OGNI
component gia' salvato (self-heal se le tabelle erano stale/vuote).

### `model_groups_rule` — riga

`(app_code, model, group) -> {read, create, update, delete, export}`.
Enforced da `Service._get_model_group_access` (vedi "Motore
model-level" sotto). `ModelGroupsRule` in `app/core/models.py` esiste
solo per validare/normalizzare la riga PRIMA della insert
(`model.new(data=row)` sul model dynamic reale), non e' il model ORM
registrato — quello e' dynamic, derivato dal component
`model_groups_rule` che ha gia' field type/tableView corretti nel
form.

### `model_fields_rule` — riga

Due `rule_type` nella STESSA collection:

- `rule_type="fields"`: una riga per `(model, group)`, da
  `fields_rule.allowed_groups`. `restricted_fields` = lista COMPLETA
  dei campi ristretti per quel model (non filtrata per riga), `read`
  = quel gruppo li vede in chiaro.
- `rule_type="record"`: una riga per elemento di `record_rules`,
  `group` sempre vuoto, `filters` = query mongo VERBATIM (puo'
  contenere `{"var": "user.uid"}` non ancora risolto), scritta come
  stringa JSON (non dict tipizzato: `model_fields_rule.filters` e' un
  campo testo/json-editor nel form, parse difensivo lato lettore).

`ModelFieldsRule` in `app/core/models.py`: stesso ruolo di validazione
pre-insert di `ModelGroupsRule`.

Meccanismo del bug field-type (gia' corretto, spiegato per esteso
perche' e' il motivo per cui il form del component `model_fields_rule`
ha i tipi che ha):

1. `model_fields_rule` e' un model DYNAMIC: i tipi pydantic dei suoi
   campi non sono scritti a mano, li genera `ModelMaker` leggendo il
   `type` di ogni campo nel FORM del component (es. `textarea` →
   pydantic `str`, `json` → `dict`, `select multiple` → `List[Any]`).
2. In Mongo, `filters` contiene un oggetto (`{"owner_uid": ...}`) e
   `restricted_fields` una lista di stringhe — cosi li scrive
   `model_rules_sync.py`.
3. Se il campo form di `filters`/`restricted_fields` e' un semplice
   `textarea`, `ModelMaker` genera un field pydantic tipizzato `str`.
4. Ogni lettura della riga (`model.find(...)`, `model.by_name(...)`,
   la validazione in `_validated_row`) passa il dict/list reale di
   Mongo dentro un model che si aspetta `str` → pydantic rifiuta la
   coercizione e alza `ValidationError` a runtime (non al save, alla
   lettura successiva).
5. Fix: nel form del component impostare `properties.type: "json"` su
   `filters` (`ModelMaker` genera `dict`) e `select multiple` su
   `restricted_fields` (genera `List[Any]`) — i tipi generati
   combaciano con quello che Mongo contiene davvero.

## Motore model-level: `model_group_access` + `Service._get_model_group_access`

`app.ozon_env_acl.model_group_access(rows, actor_groups, is_admin)` —
gate CRUD `(read, create, update, delete, export)` a livello di MODEL
INTERO, indipendente dal field ACL:

- admin → bypassa sempre, full access.
- non-admin: per ogni riga `model_groups_rule` il cui `group` e' tra i
  gruppi dell'attore, fa l'OR delle azioni concesse. **Fail-closed
  totale**: se NESSUNA riga copre un gruppo dell'attore — incluso il
  caso "il model non ha proprio righe" (sync mai avvenuto, o model in
  `IDENTITY_MODEL_NAMES`, escluso dai default) — nega tutto. Questo
  vale anche se il model_groups_rule e' vuoto per pura assenza di
  sync: e' una scelta deliberata (rischio accettato di bloccare un
  model per un sync mancato, a favore di non lasciare mai un model
  senza gate esplicito).

`Service._get_model_group_access(model_key)` (cache per-request via
`_get_model_groups_rule`) legge le righe scoped per `app_code`+model
e chiama `model_group_access` con `session.user.groups`/`is_admin`
correnti.

### Dove viene applicato

- `Service.load_record`: gate `read`/`update` — vedi composizione con
  `record_rules` sotto ("Dove viene applicato" nella sezione Record
  rule).
- `Service.list_records`/`stream_record`: se `read` negato, il
  `domain` mongo viene forzato a `{"rec_name": {"$in": []}}` (stesso
  idioma fail-closed di `record_rule_read_domain`) — lista vuota, non
  errore. `readable`/`editable`/`can_create` sulla response derivano
  da `model_access`.
- `Service.upsert`: gate `create`/`update` (a seconda
  dell'operazione risolta) PRIMA di eseguire l'hook `data.before_write`
  e l'enforcement field-level — nega con `403 {"message": "Model ACL
  denied", "model", "operation"}`. Il CREATE e' gate SOLO da questo
  motore (nessun record esiste ancora da valutare via `record_rules`).
- `ActionRuntime._is_action_allowed`: per le action `admin`/`sys`
  SENZA gruppo esplicito sull'action stessa, la visibilita' e' decisa
  da `model_group_access(read)` sul model target dell'action — vedi
  "Livello menu/action" sotto.
- **Non gated**: `_is_menu_group_allowed` (i `menu_group` sono
  cartelle di navigazione, nessun campo `model` — vedi "Livello
  menu/action"); non esiste un endpoint `export` ne' un endpoint DELETE
  generico per model (il delete passa sempre da un'action, quindi da
  `_is_action_allowed`) — il flag `export`/`delete` della riga resta
  quindi in gran parte inutilizzato salvo che dall'action-delete.

### Composizione con `record_rules` (model-level × record-level)

Le due enforcement sono indipendenti e si combinano in **AND**, non
OR: `model_groups_rule` decide se il VERBO (read/create/update/delete)
e' permesso al gruppo su quel MODEL; `record_rules` puo' solo
RESTRINGERE ulteriormente l'insieme di RIGHE per un verbo gia'
permesso — non concede mai un verbo che il gruppo non ha a livello di
model (stesso schema Odoo-style `ir.model.access` + `ir.rule`: il
primo gate e' sempre binario per model, il secondo filtra le righe).

Conseguenza pratica: se un model concede al gruppo `user` solo
`read` (non `create`), un `record_rules` di tipo owner
(`owner_uid == user.uid`, iniettato di default da
`normalize_component_properties`) NON permette comunque a un utente
`user`-only di creare record su quel model — serve che il model
stesso conceda `create` a quel gruppo (via `model_groups_rule`),
dopodiche' `record_rules` puo' restringere quali righe esistenti puo'
poi leggere/modificare.

## Motore field ACL: `FieldAclPolicy` + `CompiledFieldAcl`

`app/core/models.py: FieldAclPolicy` — model STATICO (registrato in
`_STATIC_MODELS`, `app/deps/app_env.py`), collection `field_acl_policy`.
Riga di policy esplicita, indipendente dal builder component:

```
app_key, form_key, model_key, field_path="*",
operation: read|create|update|delete|export,
actor_selector: dict|str,
workflow_stage, task_key,
effect: allow|deny|obfuscate,
priority: int (piu' basso = piu' prioritario)
```

`field_path` supporta `"*"` (tutto il record) e prefissi `"a.b.*"`
(sotto-albero). `actor_selector` matcha su `uid`, `role`/`roles`,
`group`/`groups`, `exclude_groups`, `sector`/`sectors`, `sector_id`,
`is_admin` — sia come stringa (`"group:gdpr,dpo"`) sia come dict.

### Pipeline di compilazione (`Service._get_compiled_field_acl`)

Ad ogni request, cache su `session.compiled_field_acl` +
`Service._compiled_field_acl`:

```
_load_field_acl_policies()
  = righe field_acl_policy (esplicite, se il model esiste)
  + _load_model_fields_rule_policies()   # rule_type="fields"
```

`compile_field_acl_policies` valuta `actor_selector` contro l'attore
sessione corrente (uid/groups/roles/sector/is_admin) UNA VOLTA, produce
`CompiledFieldAcl` con solo le policy applicabili — poi
`for_operation`/`denied_fields`/`read_masks`/`apply_read` lavorano su
quella lista gia' filtrata, senza rivalutare il selector per record.

### `model_fields_rule` (rule_type="fields") → policy OBFUSCATE

`_load_model_fields_rule_policies`: righe con `read=True` per
`(model, field)` vengono unite in UNA policy `OBFUSCATE` con
`exclude_groups = unione di tutti i gruppi ammessi` — un campo va
oscurato se ALMENO una policy che matcha lo dice, quindi righe separate
per gruppo andrebbero unite altrimenti il gruppo A da solo negherebbe
un attore che e' anche nel gruppo B. **Nessun bypass admin qui**
(diverso dal legacy/`models_groups`): un campo GDPR-style non diventa
visibile solo perche' l'attore e' admin — comportamento confermato
esplicitamente dall'utente dopo un bug osservato (admin vedeva il
campo in chiaro su tutta la lista).

### Enforcement lettura (`CompiledFieldAcl.apply_read` / `read_masks`)

- `read_masks`: separa campi `DENY` (rimossi) da `OBFUSCATE` (settati a
  `None`).
- `apply_read`: clona i dati, per ogni item applica `DENY` (rimuove il
  path, o azzera l'intero record se `field_path == "*"`) poi
  `OBFUSCATE` (`None` sul path).

  Meccanismo del bug (gia' corretto, spiegato per esteso): le funzioni
  che scrivono/cancellano un campo (`_path_set`, `_path_del`,
  `_clear_record`) erano scritte assumendo che `payload` fosse sempre
  un `dict` — `current[part] = value`, `dict.clear()`. Questo perche'
  i test dell'app usano `FakeCollection` con dizionari in memoria
  (vedi CLAUDE.md: pattern di test dell'app), quindi con quei dati
  funzionava. Ma `list_records`/`load_record` in produzione passano
  a `apply_read` le istanze REALI ritornate da `find()`/`by_name()`:
  oggetti pydantic/`CoreModel`, non dizionari — su un'istanza
  pydantic `current[part] = value` non esiste (niente
  `__setitem__`) e `dict.clear()` non esiste. Il risultato era che
  l'oscuramento/DENY non veniva mai applicato ai dati reali (il
  campo restava visibile in chiaro, o il record DENY-`"*"` restava
  visibile per intero) — un bug silenzioso perche' i test (dict-based)
  continuavano a passare mentre solo le richieste reali erano
  compromesse.

  Fix: `_path_set`/`_path_del`/`_clear_record` ora controllano il tipo
  (`isinstance(current, dict)`) e usano `setattr(current, part,
  value)` / iterazione su `model_fields` come fallback per le istanze
  non-dict — funzionano quindi sia sui dati dei test sia sui dati
  reali.

### Enforcement scrittura (`enforce_write_acl`)

`Service.upsert` chiama `enforce_write_acl(acl, ...)` prima di
scrivere: calcola `denied_fields` sui path del payload
(`iter_payload_paths`, ricorsivo su dict annidati), se non vuoto
logga su collection `field_acl_audit` (`audit_denied_fields`) e lancia
`403` con `{"message": "Field ACL denied", "model", "operation",
"fields"}`.

## Record rule (`record_rules`, `rule_type="record"`)

Diverso dal field ACL: valuta filtri mongo contro UN RECORD GIA'
CARICATO (non compilabile in `CompiledFieldAcl`, che e' actor-only,
niente contesto riga). Fonte di verita': `model_fields_rule` (la
collection, popolata dal sync), non `component.properties` — un
identity model come `user` e' escluso dai default ma puo' avere una
regola configurata a mano in passato, il sync l'ha scritta comunque.

### Due viste sulla stessa riga

- `evaluate_record_rule_override` → per il RIVELAMENTO campi: se una
  regola matcha, ritorna lo SCOPE (`restricted_fields`) che quella
  regola sblocca dalla baseline `fields_rule`. `apply_record_rule_
  override` fa la differenza insiemistica `baseline - scope` (NON
  sostituzione): un campo oscurato dalla baseline ma fuori scope resta
  oscurato anche a match. Scope vuoto `[]` = sblocca tutto.
- `evaluate_record_rule_access` / `record_rule_access` → per
  read/create/update/delete SUL RECORD stesso (non sui suoi campi):
  ritorna le azioni della PRIMA regola che matcha. **Fail-closed**: se
  il model ha `record_rules` configurato e nessuna regola matcha
  (non e' il tuo record), nega tutto — a differenza del field masking
  (dove "nessun match" = resta la baseline).

  Il parametro si chiama `bypass_ownership`, NON `is_admin`: **un
  admin puro NON bypassa piu' l'enforcement record-level** su un
  model non-sys (coerente col `fields_rule` GDPR-style, che non
  concede bypass admin — comportamento cambiato deliberatamente:
  prima l'admin bypassava sempre, ora deve matchare una regola come
  chiunque altro, a meno che il model non sia sys). L'unico bypass
  automatico e' per i model sys — vedi `is_sys_model` sotto.

### `is_sys_model` — esenzione per model condivisi

`Service._is_sys_model` (cache per model): true se il component ha
`sys=True`. `record_rule_access` viene chiamato con
`bypass_ownership=is_sys_model` (NON piu' `is_admin or is_sys_model`)
— i model sys (`action`, `menu_group`, `settings`, `user`, ecc.) sono
config applicativa condivisa, non documenti di un singolo utente: la
regola di default `owner_uid == user.uid` (iniettata su OGNI
component da `normalize_component_properties`) nasconderebbe config
condivisa a chiunque non l'abbia creata — questi model sono comunque
gia' regolati per gruppo da `model_groups_rule`. Fail-open a `True`
(sys → enforcement record-level SALTATO) se il lookup del component
fallisce.

### Dove viene applicato

- `Service.load_record`: `final_read = model_access["read"] AND
  record_access["read"]` (vedi "Composizione con record_rules" sopra)
  — false → `404` (non `403`, per non rivelare l'esistenza del
  record); `readable`/`editable` sulla response = `final_read`/
  `final_update` (`model_access["update"] AND record_access["update"]`)
  — enforcement hide/readonly lato UI.
- `Service.list_records`: se `record_rules` e non admin e non
  sys-model, `record_rule_read_domain` restringe il `domain` mongo
  all'OR dei filtri (risolti) delle regole con `read=True` — fail-
  closed: nessuna regola con read → domain `{"rec_name": {"$in":
  []}}` (nessuna riga). Poi per ogni riga risultante,
  `apply_record_rule_override` puo' rivelare campi oscurati dalla
  baseline (owner del record vede i propri campi GDPR).
- `Service.stream_record`: stessa logica, ma se `record_rules` e'
  presente l'oscuramento server-side (query-level `obfuscate_fields`)
  viene SALTATO — il valore reale serve non ancora oscurato per poter
  eventualmente essere rivelato dall'override; l'oscuramento avviene
  riga per riga in Python (`_apply_record_rules_to_stream`).

### `resolve_var` / json-logic

`filters` puo' contenere nodi `{"var": "user.uid"}`: risolti da
`Service._resolve_query_json_logic_vars` (iniettato come callback per
evitare import circolare `ozon_env_acl` ↔ `service.py`) prima del
match contro il record.

## Livello menu/action (gate separato, NON field ACL)

`_is_menu_group_allowed` (menu) e `ActionRuntime._is_action_allowed`
(action/pulsanti, ASYNC) sono un gate a parte, non passano per
`CompiledFieldAcl` — ma da quando l'action ha un model target, il
secondo ora si appoggia a `model_group_access`:

**`ActionRuntime._is_action_allowed(action)`** (async — tutti e 4 i
call site fanno `await`):
- admin → sempre passa.
- gruppi espliciti sull'action (`groups` field) → utente deve stare
  in almeno uno (override manuale, ha sempre precedenza).
- altrimenti, se `admin`/`sys` sull'action: `model_group_access(read)`
  sul model target dell'action (`action.model`) decide — nessun check
  hardcoded `IDENTITY_MODEL_NAMES`/`technical_operator` piu': un model
  identity resta admin-only "gratis" perche' non ha mai righe
  `model_groups_rule` di default (fail-closed le nega tutte ai non-
  admin), niente di dedicato da mantenere qui. Se l'action non ha
  `model` (raro, action senza target), fallback all'euristica
  precedente (`"technical_operator" in user_groups`).

**`Service._is_menu_group_allowed(group)`** (sync, INVARIATO — non usa
`model_group_access`): un `menu_group` e' una cartella di navigazione
che raggruppa action su MODEL DIVERSI (`app/base/schema/
components.json`: `menu_group` non ha campo `model`) — non esiste un
singolo model da controllare a questo livello. Resta quindi il
meccanismo originale, ortogonale all'ACL dati per model:
- admin → sempre passa.
- gruppi espliciti sul menu_group (`groups` field) → utente deve
  stare in almeno uno.
- altrimenti, se `admin` sul menu_group: `rec_name == "identity"` →
  solo admin; altro menu admin/sys → admin + `technical_operator`
  (euristica hardcoded, non da `model_groups_rule`).

## `IDENTITY_MODEL_NAMES` — layer sempre admin-only

`{"user", "groups", "group_users", "model_groups_rule",
"model_fields_rule"}` (`app/core/OzonEnvApp.py`). Esclusi dai default
`models_groups`/`models_restricted_fields` iniettati da
`normalize_component_properties` — le tabelle del motore ACL stesso
(`model_groups_rule`/`model_fields_rule`) devono restare admin-only
per costruzione (deny-by-default), altrimenti un non-admin potrebbe
leggere/derivare le regole ACL di altri model.

## Persistenza: `properties` e' un campo atomico

`preserve_acl_properties_on_partial_save` (`OzonEnvApp.py`): un save
di component che ricostruisce `properties` da zero con solo alcune
chiavi (es. editor report/rheader/rfooter) sovrascriverebbe l'intero
sotto-documento via `$set`, cancellando silenziosamente
`models_groups`/`models_restricted_fields` impostate in precedenza.
`AppOzonEnv.insert_update_component` chiama questa funzione PRIMA di
`normalize_component_properties` (altrimenti il `setdefault` non
toccherebbe piu' nulla): se il payload non porta quelle chiavi, le
ripristina dal component esistente.

Bug reale osservato su `user`: un save di `properties` che ricostruiva
il sotto-documento solo con alcune chiavi (es. da un editor
report/rheader/rfooter) faceva sparire silenziosamente
`models_restricted_fields` gia' configurata. E' lo stesso pattern
generale dell'upsert parziale di `ozon-env` — un `update()` che fa
`$set` sui soli top-level field cambiati tratta `properties` come UN
blocco atomico, non fa merge chiave-per-chiave al suo interno — qui
applicato al sotto-campo `properties` di un record invece che al
record intero.

## Riepilogo: chi enforce cosa

| livello | sorgente config | collection flat | enforcement | fail mode |
|---|---|---|---|---|
| model CRUD (`models_groups`) | `component.properties` | `model_groups_rule` | `model_group_access`, bypass admin | **fail-closed totale** (nessuna riga = nega, non solo "nessun match") |
| campo (`fields_rule`) | `component.properties` | `model_fields_rule` (`rule_type=fields`) | `CompiledFieldAcl` OBFUSCATE, **niente bypass admin** | oscura se nessun gruppo matcha |
| riga (`record_rules`) — rivelamento campi | `component.properties` | `model_fields_rule` (`rule_type=record`) | `apply_record_rule_override`, bypass solo sys | nessun match → baseline invariata |
| riga (`record_rules`) — accesso record | idem | idem | `record_rule_access`, bypass solo sys (**niente bypass admin puro**) | nessun match → **nega tutto** |
| action | field `groups` esplicito, poi `model_group_access(read)` se `admin`/`sys` | `model_groups_rule` (via il model target dell'action) | `ActionRuntime._is_action_allowed` (async) | segue model_group_access |
| menu (folder) | field `groups`/`admin` sul menu_group | — (nessun model target) | `Service._is_menu_group_allowed` (euristica propria, invariata) | passa se non configurato |
| policy esplicite | collection `field_acl_policy` | — (e' gia' la fonte) | `CompiledFieldAcl` (stesso motore del field ACL) | dipende da `effect` |

Formato legacy (CSV/dict-per-campo) RETIRATO: nessuna riga viene piu'
sintetizzata da quel formato, ne' a livello model ne' a livello campo
— un component ancora in quel formato e' fail-closed (model) o
semplicemente senza oscuramento field-level aggiuntivo (era comunque
gia' un dead path, nessun default lo produce).

## File coinvolti

```
app/core/models.py                    FieldAclPolicy, ModelGroupsRule, ModelFieldsRule (validazione)
app/core/OzonEnvApp.py                normalize_component_properties, default ACL, IDENTITY_MODEL_NAMES,
                                       preserve_acl_properties_on_partial_save, insert_update_component (trigger sync)
app/ozon_env_acl/__init__.py          motore: CompiledFieldAcl, apply_read, record_rule_*, session groups/admin
app/ozon_env_acl/model_rules_sync.py  flatten component.properties -> model_groups_rule / model_fields_rule
app/deps/app_env.py                   registrazione statica field_acl_policy; ClientSession/get_ozon_env -> apply_session_allowed_users
app/services/service.py               integrazione: list_records/stream_record/load_record/upsert, cache per-request
app/services/action_runtime.py        _is_action_allowed (async — model_group_access sul model target dell'action)
app/services/session_auth.py          persist_user_session -> apply_session_groups (popola groups per-request)
app/base/data/groups.json             seed gruppi + gerarchia implied_groups
app/base/data/group_users.json        seed membership (vuoto di default)
```
