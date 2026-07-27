# Security audit — ozon-env-app (branch 1.0, 2026-07-27)

## Perimetro effettivo

Revisione manuale. Per non dare falsa assicurazione, ecco cosa e' stato
letto riga per riga e cosa no.

**Esaminato integralmente:** tutto il percorso di autenticazione e
autorizzazione (`app/deps/app_env.py`, `app/services/session_auth.py`,
`app/services/cookie_auth.py`, `app/api/auth_routes.py`, e in `ozon-env`
`core/auth.py` + `OzonOrm.init_auth`/`authenticate_user_token`/
`build_auth_user`/`persist_user_token`); tutti i router
(`routes.py`, `action_router.py`, `filter_router.py`,
`service_registry_router.py`, `message_queue_router.py`,
`websocket_router.py`); il motore ACL (`app/ozon_env_acl/__init__.py`) e
i suoi punti di enforcement in `service.py`; upload e antivirus;
`webhooks.py`; `service_registry.py`; `plugin_installer.py`;
`remote_service.py` + `selectComponentService.py`; `middleware/logging.py`;
`app_settings.py`; `docker-compose.yml`, `Dockerfile`, `deploy.sh`,
`.gitlab-ci.yml`, `.github/workflows/`, `.gitignore` e la storia git.
Scansione mirata su tutto il repo per le primitive di esecuzione codice
(`eval`/`exec`/`pickle`/`shell=True`/estrazione archivi) e per gli
operatori Mongo pericolosi.

**Non esaminato in dettaglio:** `app/services/service.py` oltre ai punti
di enforcement ACL (3208 righe — letti i gate CRUD, non l'intera logica
di business); `app/services/action_runtime.py`; `app/services/camunda.py`
e `app/api/camunda_router.py` (letto solo `app/core/camunda.py` per il
TLS); `app/services/formio.py`; `models/` (definizioni Pydantic,
superficie bassa); `workers/` (letto solo il TLS Camunda);
`services/mail_sender`, `services/people_sync`, `services/identity_manager`
(letti `calendar_scheduler/auth.py` e `mcp_search/gateway.py`);
`bootstrap.py`, `bootstrap.sh`, `db_restore.sh`.

Un secondo passaggio su `action_runtime.py` e sul percorso Camunda e'
la prosecuzione naturale di questo lavoro.

Legenda severita': **ALTA** = sfruttabile oggi da un utente autenticato
qualunque, o credenziali gia' esposte. **MEDIA** = richiede una
condizione di deploy o un contesto aggiuntivo. **BASSA** = difesa in
profondita' / rischio latente.

Ogni finding e' marcato `[confermato]` (letto nel codice, catena
completa) o `[da verificare a runtime]`.

---

## Sommario

| # | Severita' | Titolo | Stato |
|---|-----------|--------|-------|
| 1 | ALTA | Service Registry senza gate admin → spawn subprocess | ✅ risolto |
| 2 | ALTA | SSRF + esfiltrazione arbitraria di `global_params`, CSRF-exempt | ✅ risolto |
| 3 | ALTA | Segreti reali ancora presenti nella storia git | 🔶 storia ripulita — **rotazione ancora necessaria** |
| 4 | ALTA | IDOR sugli allegati di record (nessuna ACL) | ✅ risolto |
| 5 | — | ~~Token SSO inviati in chiaro ai webhook~~ | ❌ **ritirato: falso positivo** |
| 6 | MEDIA | Nessuna verifica `aud` sul JWT + auto-provisioning | ✅ risolto (serve config) |
| 7 | MEDIA | CSWSH: allowlist Origin WebSocket fail-open di default | ✅ risolto |
| 8 | MEDIA | `.env.example` spedisce `CLAMAV_FAIL_CLOSED=false` | ✅ risolto |
| 9 | MEDIA | Bundle token Keycloak dentro il cookie di sessione | ✅ risolto |
| 10 | MEDIA | Mongo pubblicato sull'host con credenziali root | ✅ risolto |
| 11 | BASSA | `Service.by_name` non applica alcuna ACL | aperto |
| 12 | BASSA | `SESSION_SECRET` fallback per-processo, nessun check | aperto |
| 13 | BASSA | `$regex` client-controlled nell'allowlist query ACL | aperto |
| 14 | BASSA | Codice morto: auth via trusted-header `x-remote-user` | aperto |
| 15 | BASSA | `mcp_search` in ascolto su 0.0.0.0 senza identita' | aperto |
| 16 | BASSA | Decode JWT senza verifica firma (solo scadenza) | aperto |
| 17 | INFO | Config di sicurezza dichiarata ma mai letta | aperto |
| 18 | INFO | Documento di sicurezza citato ma inesistente | aperto |

### Stato dei fix (2026-07-27)

ALTE e MEDIE risolte in codice; le BASSE/INFO restano aperte come
manutenzione. Suite: **337 passed, 4 skipped** (baseline 310).

Azioni **non** automatizzabili, a carico dell'operatore:

1. **Finding 3 — ruotare le credenziali.** La storia e' stata ripulita
   (sotto), ma la pulizia **non annulla la fuga**: chiunque avesse
   clonato prima del 2026-07-27 ha ancora i segreti nel proprio `.git`.
   Da ruotare: `MONGO_PASS`, `OZON_ADMIN_TOKEN`, `JWT_SECRET_KEY` (da
   `tests/testapp/.env`) e i client Keycloak.
2. **Finding 6 — valorizzare `OZON_TOKEN_AUDIENCE`.** Il codice ora
   propaga l'audience al verificatore, ma finche' la variabile e' vuota
   il controllo resta disattivo (scelta voluta: un valore sbagliato
   produrrebbe 401 su tutte le richieste). Allineare prima l'audience
   lato Keycloak, poi impostarla.
3. **Finding 9 — le sessioni attive cadono al deploy.** Il formato del
   cookie cambia (ora cifrato): i cookie emessi prima non sono piu'
   decifrabili e gli utenti rifanno login una volta.
4. **Finding 7 — verificare `EXTERNAL_BASE_URL`.** E' il fallback della
   allowlist Origin del WebSocket. Se non corrisponde all'origin reale
   del frontend, gli handshake WS via cookie vengono rifiutati (con un
   log esplicito). In alternativa impostare `WS_ALLOWED_ORIGINS`.

### Pulizia della storia git — eseguita il 2026-07-27

**Correzione al finding 3 come scritto sotto:** il leak **non e' mai
stato su GitHub**. `origin` (github.com/INRIM/ozon-env-app) espone solo
il branch `1.0`, che non ha mai contenuto `.env`. Era confinato al
GitLab interno. Nessuna esposizione pubblica.

Secondo file individuato durante la pulizia: **`tests/testapp/.env`**
(commit `5a0345c`, contiene `JWT_SECRET_KEY` e credenziali Mongo di
test), presente anche sul branch `old`. Rimosso nello stesso passaggio.

Distribuzione reale dei file sensibili:

| branch | occorrenze | riscritto |
|--------|-----------|-----------|
| `1.0` (attivo) | 0 | no |
| `main` | 0 | no |
| `master` | 7 | si': `604c49f` → `a664169` |
| `new_release` | 7 | si': `db40ccc` → `2c3fc9e` |
| `old` | 2 | si': `5ce3d84` → `d2b1dfd` |

`1.0` e `main` sono stati lasciati intatti di proposito: erano gia'
puliti, riscriverli avrebbe solo distrutto le firme GPG e costretto a
riallineare il branch di sviluppo attivo.

Procedura: `git filter-repo --invert-paths --path .env --path
tests/testapp/.env` su un mirror clone, poi force-push dei soli 3 branch
sporchi. Backup pre-riscrittura in
`scratchpad/histclean/backup-pre-rewrite.bundle` (12M) con gli SHA
originali in `ORIGINAL_REFS.txt`.

Verifiche superate:

- clone fresco dal server: nessuna occorrenza dei due file su nessun
  branch;
- `git fetch <sha>` dei commit `8bc99d1` / `99c700a` / `5a0345c` →
  `upload-pack: not our ref` (non recuperabili per SHA via protocollo);
- clone locale ripulito (`fetch --prune` + `reflog expire` + `gc
  --prune=now`): blob `6a780da` non piu' presente.

**Costo:** i 3 branch riscritti perdono **40 firme GPG** — nessuna
riscrittura puo' conservarle, la firma copre il contenuto originale.

**Restano da fare:**

1. **Housekeeping su GitLab.** Il protocollo git non serve piu' i vecchi
   commit, ma la UI/API web puo' ancora renderizzare blob per SHA finche'
   il gc lato server non gira: *Project Settings → Repository →
   Housekeeping*.
2. **Avvisare chi ha cloni di `master`/`new_release`/`old`**: devono
   riclonare, non fare `pull` (una merge reintrodurrebbe la storia
   vecchia, segreti inclusi).
3. **Controllare le pipeline CI** che referenziano SHA ora inesistenti.

### Finding 5 ritirato

L'audit lo dava per confermato sulla base del `model_dump()` in
`persist_user_session`. Verifica successiva: `AppSession`
(`app/core/session.py:25-28`) marca gia' `token`, `sso_token`,
`sso_refresh` e `claims` come `Field(..., exclude=True)`, quindi
`model_dump()` **non li emette** — ne' verso il webhook ne' verso
l'`upsert` sulla collection `user`. Il docstring della classe cita
esplicitamente il payload `user.session.persist` come caso coperto, e
`tests/test_session_auth_sso.py:234-235` lo verificava gia'.

Nessun leak: il finding era errato. Resta valido il finding 9, che
riguarda un percorso diverso — `auth_routes.py:92` legge
`session.token` come **attributo**, bypassando `model_dump()` e quindi
`exclude=True`, e su quel percorso l'oggetto e' un `User` di ozon-env,
non un `AppSession`.

---

## 1. ALTA — Service Registry: nessun gate admin, spawn di subprocess

`[confermato]`

`app/api/service_registry_router.py:16-89` — il router monta solo
`dependencies=[Depends(get_authed_env)]`: **autenticazione, zero
autorizzazione**. Nessun `is_admin`, nessun controllo di gruppo.

La catena completa, tutta raggiungibile da un utente autenticato
qualunque:

1. `POST /services/registry/manifests` con `source_path` arbitrario e
   `manifest.compose_file` arbitrario. `ServiceManifest` valida solo che
   `code` sia alfanumerico e che i campi non siano vuoti
   (`app/core/service_registry.py:46-62`) — `source_path` e
   `compose_file` non sono validati affatto.
2. `POST /services/registry/{code}/up` →
   `ServiceRegistryCore._compose_action` →
   `DockerComposeRunner.compose` (`app/core/service_registry.py:102-114`):

```python
argv = ["docker", "compose", "-f", str(compose_file)]
argv.extend(["up", "-d", "--build"])
proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), ...)
```

`compose_file` e `cwd` derivano entrambi dal record scritto al passo 1.

**Bypass ACL totale.** `Service.register_service_manifest`
(`app/services/service.py:416`) delega a
`ServiceRegistryCore.register_manifest`, che chiama `model.upsert()`
direttamente sull'ORM — **non** passa da `Service.upsert`, quindi salta
`_get_model_group_access`, `enforce_write_acl` e i webhook. Il motore ACL
fail-closed descritto in `docs/ACL_MODEL_GROUPS_FIELDS_RULES.it.md` non
viene mai interrogato su questo percorso.

**Perche' non e' RCE oggi:** l'immagine (`Dockerfile`) installa solo
`git` + `ca-certificates`, non il CLI docker, e `docker-compose.yml` non
monta `/var/run/docker.sock`. `create_subprocess_exec` fallisce quindi
con `FileNotFoundError`. Il gate e' l'ambiente, non il codice.

**Impatto attuale:** scrittura non autorizzata su `service_registry` e
`service_registry_repo` (defacement della config servizi, DoS sui
servizi registrati) + `stdout`/`stderr` del processo restituiti al
chiamante.

**Impatto se l'immagine acquisisce il CLI docker o il socket** (e' un
registry di servizi: e' la direzione naturale del progetto): esecuzione
di codice arbitrario come root sul demone docker dell'host.

**Fix:**
- Gate `is_admin` (o un gruppo dedicato) su tutto il router — via
  dependency, non per-endpoint.
- Far passare `register_manifest`/`register_repo` da `Service.upsert`
  cosi' che le regole `model_groups_rule` valgano anche qui.
- Vincolare `source_path` a una root fissa (`services/`) con
  `Path.resolve().relative_to(root)`, come gia' fa
  `load_record_attachment_file`.
- Non restituire `stdout`/`stderr` grezzi al client.

---

## 2. ALTA — SSRF + esfiltrazione arbitraria di `global_params`, esente da CSRF

`[confermato]`

`app/api/routes.py:426-454`:

```python
@router.post("/get_remote_data_select")
@router.post("/get_remote_select")
async def post_remote_data_select(payload_raw, service):
    payloadr = _coerce_body_model(RemoteSelectRequest, payload_raw)
    elif payloadr.data.url:
        header_data = build_remote_select_header(_dump_model(payloadr.data))
        data = await remote_data_select_response(url=header_data.url, ...)
```

`url` arriva **dal body della richiesta**. `_fetch_remote_data`
(`app/services/remote_service.py:22-55`) lo passa dritto a
`httpx.AsyncClient(timeout=None).get(url=url, ...)`:

- nessuna allowlist di host, nessun controllo di schema
  (`file://`, `http://169.254.169.254/`, `http://localhost:*` passano),
- **`timeout=None`** — una richiesta verso un host che non risponde
  tiene occupato un worker all'infinito (DoS con poche richieste),
- il corpo della risposta torna al chiamante via `extract_remote_data`
  → **esfiltrazione**, non SSRF cieca.

### 2b. Esfiltrazione arbitraria di `global_params` (piu' grave dell'SSRF)

`build_remote_select_header`
(`app/services/components/selectComponentService.py:16-44`) non risolve
nulla da DB: legge `url`, `header_key` e `header_value_key`
**direttamente dal dict inviato dal client** (o da `headers[0].key` /
`headers[0].value`, sempre dal body). Confermato: `url` e' pass-through.

Poi `remote_data_select_response`
(`app/services/remote_service.py:57-79`):

```python
rec_cfg = await get_global_param(service, header_value_key)
header_val = rec_cfg.get("key") if isinstance(rec_cfg, dict) else rec_cfg
remote_data = await _fetch_remote_data(header_key=header_key,
                                       header_value=header_val, url=remote_url)
```

`header_value_key` e' **scelto dall'attaccante**, e
`get_global_param` (`app/services/common.py:285-296`) fa
`service.by_name("global_params", name)` — che a sua volta
(`app/services/service.py:396-399`) chiama `compo_model.by_name(name)`
**senza alcun controllo ACL** (finding 11).

Quindi: un utente autenticato qualunque nomina un record arbitrario di
`global_params` e ne fa spedire il valore come header HTTP verso un URL
che sceglie lui. `global_params` e' la collection dove risiedono le
chiavi API dei servizi esterni. **Estrazione di segreti applicativi in
una singola richiesta**, senza bisogno di leggere la collection.

Questa e' la parte piu' grave dell'endpoint: l'SSRF e' il veicolo, il
furto di credenziali e' l'effetto.

**Aggravante CSRF.** Entrambi i path sono in
`_READ_ONLY_POST_CSRF_EXEMPT_PATHS` (`app/deps/app_env.py:47-51`), quindi
in modalita' cookie/BFF `_validate_csrf` li salta. Un sito ostile puo'
far partire l'SSRF dal browser di un utente loggato. L'esenzione era
motivata da "read-only", ma questi due endpoint non sono read-only
rispetto alla rete: fanno partire traffico in uscita dal server.

Superficie interna raggiungibile dalla rete `ozon-net` / `camunda`:
Keycloak internal, Camunda REST/Zeebe, gli altri sidecar, e
`http://ozonenv_app_db:27017` (non parla HTTP ma risponde abbastanza da
fare port-scan per differenza di errore/tempo).

**Fix (in ordine di urgenza):**
- **`header_value_key` non deve mai arrivare dal client.** Risolvere il
  nome del `global_params` server-side dalla config del component, mai
  dal body. Questo chiude l'esfiltrazione anche prima di sistemare
  l'SSRF.
- Allowlist di host/base-URL da configurazione, non dal body. Il caso
  d'uso legittimo parte da una config di component: risolvere l'URL
  server-side e accettare dal client solo un identificatore.
- Se l'URL deve restare client-controlled: forzare `https`, risolvere il
  DNS e rifiutare gli IP privati/link-local **prima** della connessione,
  `follow_redirects=False`.
- Timeout esplicito (es. 10s), mai `None`.
- Togliere questi due path dall'esenzione CSRF.

---

## 3. ALTA — Segreti reali ancora recuperabili dalla storia git

`[confermato]`

`.env` e' stato committato con valori di produzione ed e' stato tolto
solo da `HEAD` in `99c700a` ("[FIX] gitignore", 2026-04-28). Il contenuto
resta integralmente leggibile:

```
git show 8bc99d1:.env
```

Chiavi presenti con valore reale (valori non riportati qui):
`MONGO_PASS`, `MONGO_USER`, `KEYCLOAK_CLIENT_SECRET`,
`KEYCLOAK_WORKER_CLIENT_SECRET`, `OZON_ADMIN_TOKEN`, piu' gli hostname
interni di Keycloak e Camunda.

`.gitignore` copre `.env` **da adesso**; non ha alcun effetto sui commit
gia' fatti. Chiunque abbia (o abbia avuto) un clone del repo ha questi
segreti.

**Fix — nell'ordine:**
1. **Ruotare tutte le credenziali sopra.** E' il passo obbligatorio: la
   riscrittura della storia non aiuta se qualcuno ha gia' clonato.
2. Poi eventualmente ripulire la storia (`git filter-repo --path .env
   --invert-paths`), coordinando il force-push con chi ha cloni attivi.
3. Aggiungere un hook/CI di scansione segreti (gitleaks) per impedire
   la ricomparsa.

Nota: `services/mcp_search/service.env` e' tracciato ma **non** contiene
credenziali (verificato) — e' solo config di rete. Nessuna azione.

---

## 4. ALTA — IDOR sugli allegati di record

`[confermato]`

`app/api/routes.py:232-249`:

```python
@router.get("/client/attachment/{model}/{rec_name}/{filename}")
async def get_client_record_attachment(model: str, rec_name: str, filename: str):
    file_path, metadata = load_record_attachment_file(...)
    return FileResponse(file_path, ...)
```

La firma non prende ne' `ozon_env` ne' `service`: l'unico controllo e' la
dependency di router `Depends(get_authed_env)`, cioe' **"sei
autenticato"**. Non c'e':

- scoping per `app_code` (a differenza di
  `/client/attachment/{attachment_id}`, che lo applica),
- verifica `model_groups_rule` sul model,
- verifica `record_rulse` sul record,
- alcun field ACL.

`load_record_attachment_file` (`app/services/attachments.py:200-240`)
blocca correttamente il path traversal (`Path(filename).name` +
`resolve().relative_to(root)`), ma il traversal non serve:
`{model}/{rec_name}` **sono** l'indirizzo del file. E `rec_name` e' un
identificativo leggibile e prevedibile (non un uuid), non una capability.

Un utente qualunque enumera `modulo_dati_persona/<nome>/<file>` e legge
allegati di record che l'ACL gli nega in lettura via `/record/{model}`.

**Fix:** risolvere il record via `Service.load_record(model, rec_name)`
— che applica l'intera catena ACL — e servire il file solo se la load
riesce. Aggiungere lo scoping `app_code` alla root come fa
`_attachment_dir`.

---

## 5. ~~ALTA — Token SSO spediti in chiaro ai webhook~~ — RITIRATO

> **Falso positivo.** `AppSession` (`app/core/session.py:25-28`) marca
> gia' `token`, `sso_token`, `sso_refresh` e `claims` con
> `Field(..., exclude=True)`: `model_dump()` non li emette, quindi ne'
> il payload del webhook ne' l'`upsert` sulla collection `user` li
> contengono. Il testo sotto e' conservato solo come traccia dell'analisi
> errata — **non c'e' nulla da correggere**. Vedi la sezione "Finding 5
> ritirato" in testa al documento.

`app/services/session_auth.py:280-290`:

```python
async def persist_user_session(ozon_env, session):
    user_model = ozon_env.get("user")
    data = session.model_dump(mode="python")
    data.setdefault("rec_name", session.uid)
    await user_model.upsert(data)
    await _user_webhooks().emit("user.session.persist",
                                context=..., payload=data)
```

`data` e' il dump **integrale** di `AppSession`. Verificato che contiene
credenziali vive:

- `token` — `OzonOrm.build_auth_user` (riga 1109) assegna
  `"token": copy.deepcopy(verified.token_data)`, e `token_data` include
  `access_token` **e** `refresh_token`
  (`authenticate_user_token:1170-1173`);
- `claims` — il payload JWT completo;
- `sso_token` / `sso_refresh` — valorizzati in `build_keycloak_session`.

Due problemi distinti.

### 5a. Egress dei token verso i webhook

`WebhookDispatcher._send` (`app/core/webhooks.py:136-171`) serializza
`payload` cosi' com'e' e lo fa POST agli endpoint configurati.
**Nessuna redazione, nessuna allowlist di campi.** La firma HMAC
(`x-ozon-webhook-signature`) e' opzionale — se `core_webhooks_signing_secret`
e' vuoto l'header non viene nemmeno aggiunto, e comunque la firma
autentica il mittente, non protegge la riservatezza del corpo.

Il confronto e' netto: `routes.py:68-69` rimuove con cura
`token`/`sso_token`/`sso_refresh`/`claims` prima di rispondere al client,
mentre lo stesso identico oggetto viene spedito integro a un terzo su
HTTP. Chiunque possa configurare un endpoint webhook riceve access e
refresh token Keycloak di **ogni utente ad ogni richiesta autenticata**.

### 5b. Regressione su una fix di sicurezza upstream

`ozon-env` ha deliberatamente smesso di persistere i token: il docstring
di `persist_user_token` (`OzonOrm.py:1011-1021`) dice *"Update login
metadata without persisting authentication tokens"* e la query fa
esplicitamente `"$unset": {"token": ""}` per ripulire anche i residui
delle release precedenti la 4.0.2.

`persist_user_session` **riscrive** quel campo con `user_model.upsert(data)`,
annullando la fix ad ogni richiesta autenticata. I refresh token
Keycloak tornano a riposo nella collection `user`.

Nota collaterale: il commento a `session_auth.py:93-98` — che spiega come
gestire `user.token` "scritto da `persist_user_token` ad ogni richiesta
autenticata" — descrive un comportamento che upstream **non ha piu'**.
E' stale e va aggiornato, perche' e' la giustificazione di quella logica.

**Fix:**
- Introdurre una allowlist di campi per il payload del webhook (o
  quantomeno una denylist esplicita per `token`, `claims`, `sso_token`,
  `sso_refresh`), riusando la stessa lista di `_session_response_data`.
- In `persist_user_session`, eliminare quei campi da `data` prima
  dell'`upsert`, allineandosi alla scelta upstream.
- Rendere obbligatorio `core_webhooks_signing_secret` quando i webhook
  sono abilitati.

---

## 6. MEDIA — Nessuna verifica `aud` sul JWT, con auto-provisioning

`[confermato]`

La verifica del token e' per il resto **corretta**: firma verificata via
JWKS, algoritmi vincolati a RS256, issuer verificato
(`ozon-env/ozonenv/core/auth.py:139-170`). Ma:

```python
audience=self.settings.audience or None,
options={"verify_aud": bool(self.settings.audience), ...}
```

Il campo `token_audience` **esiste gia'** in `OzonEnvCoreSettings`
(`ozon-env/ozonenv/core/BaseModels.py:2393`, popolato da
`os.getenv("OZON_TOKEN_AUDIENCE")` alla riga 2424) — quindi
`EnvSettings` lo eredita. Ma non arriva mai al verificatore:

- `ozon_env_cfg()` (`BaseModels.py:2478-2489`) emette solo `app_code`,
  `mongo_*`, `models_folder`, `backend_interface` — **non**
  `token_audience`;
- `_build_ozon_cfg` (`app/deps/app_env.py:73-83`) aggiunge
  `keycloak_jwks_url`, `keycloak_issuer`, `oauth_url`, `client_id`,
  `client_secret` — **nemmeno lui** `token_audience`.

`KeycloakAuthSettings.from_config` ripiega quindi sul solo
`os.getenv("OZON_TOKEN_AUDIENCE", "")`, che ne' `.env.example` ne' il
template ansible definiscono. Risultato di default: `verify_aud=False`.

Conseguenza: **qualunque token del realm e' accettato**, incluso uno
emesso per un client completamente diverso (un'altra app, un client
pubblico, un service account a bassi privilegi). E `OzonOrm.init_auth`
(riga 1176-1190) fa auto-provisioning:

```python
user_record = await self.load_user_by_uid(verified.user_id)
if not user_record:
    user_record = await self.provision_auth_user(verified)
```

quindi un principal del realm senza alcuna relazione con questa app
ottiene un account creato al volo. Non e' escalation ad admin
(`is_admin` viene ricalcolato da `group_users`, cfr.
`get_authed_env:528-537` — questa parte e' fatta bene), ma e' accesso
autenticato non previsto, e da li' i finding 1, 2 e 4 sono raggiungibili.

**Fix:** il campo esiste gia', va solo propagato — aggiungere
`"token_audience": effective_settings.token_audience` in
`_build_ozon_cfg`, e valorizzare `OZON_TOKEN_AUDIENCE` in `.env.example`
e nel template ansible (nell'immediato basta la sola variabile
d'ambiente, che passa dal fallback `os.getenv`). Valutare inoltre di
rendere `provision_auth_user` condizionale a un claim/gruppo invece che
incondizionato.

---

## 7. MEDIA — CSWSH: allowlist Origin WebSocket fail-open di default

`[confermato]`

`app/api/websocket_router.py:152-158`:

```python
def _origin_allowed(websocket):
    allowlist = _allowed_origins()
    if not allowlist:
        return True   # nessun controllo
    return origin in allowlist
```

`ws_allowed_origins` ha default `""` (`app/app_settings.py:278-280`) e
**`WS_ALLOWED_ORIGINS` non compare in `.env.example`**: nessun deploy la
imposta, quindi in pratica il controllo e' sempre disattivo.

L'handshake WS si autentica dal cookie di sessione
(`_authenticate_handshake:117-131`), e l'endpoint esegue **azioni di
scrittura** (`service_handle_action_post`).

**Precisazione sull'exploitabilita' attuale.** Con
`AUTH_COOKIE_SAMESITE=lax` (il default) i browser moderni **non**
allegano il cookie all'handshake WS partito da una pagina cross-site:
`new WebSocket()` non e' una navigazione top-level, quindi Lax lo
esclude. Oggi, con la configurazione di default, l'attacco non passa.

Il problema e' che l'unica difesa attiva e' un default di un *altro*
setting, e il controllo dedicato e' fail-open:

- `AUTH_COOKIE_SAMESITE` e' configurabile: chi lo porta a `none` (caso
  d'uso plausibile — frontend su dominio diverso) apre la CSWSH senza
  accorgersene, perche' l'allowlist Origin che dovrebbe coprirlo e'
  vuota;
- `WS_ALLOWED_ORIGINS` non essendo in `.env.example` non viene mai
  impostata, quindi il controllo previsto *dagli autori stessi* e'
  disattivo in ogni deploy.

E' un rischio di configurazione, non un exploit pronto: MEDIA per la
combinazione fail-open + dipendenza da un default non correlato.

Il resto della gestione WS e' buono: niente token in query string
(commento esplicito), timeout di auth, chiusura con 1008.

**Fix:** rendere il controllo fail-closed quando il cookie e' presente
(se non c'e' allowlist e c'e' un cookie → rifiuta), e popolare
`WS_ALLOWED_ORIGINS` in `.env.example` e nel template ansible.

---

## 8. MEDIA — `.env.example` spedisce `CLAMAV_FAIL_CLOSED=false`

`[confermato]`

Il default nel codice e' **sicuro**: `app/app_settings.py:466-468` ha
`clamav_fail_closed: bool = Field(default=True, ...)`. Ma `.env.example`
lo sovrascrive con `CLAMAV_FAIL_CLOSED=false`, e i deploy partono da
quel file (`deploy.sh:59-67` copia `.env.example` in `.env` se manca).

Con fail-closed disattivato, `save_formio_attachment`
(`app/services/attachments.py:125-132`) salta il `raise` e **memorizza il
file non scansionato** quando lo scan restituisce `ERROR`. Casi che
producono `ERROR` (`antivirus.py:98-117`):

- ClamAV giu' o irraggiungibile,
- timeout di scansione,
- **file piu' grande di `CLAMAV_MAX_STREAM_MB`** (`antivirus.py:71` alza
  `AntivirusUnavailableError`), che l'attaccante controlla direttamente
  gonfiando il file.

L'utente non riceve alcun errore; i metadati registrano
`status: ERROR`, ma il file e' scaricabile.

Il validator a `app_settings.py:586-588` che impedisce
`clamav_max_stream_mb < max_upload_size_mb` gira **solo** se
`clamav_fail_closed` e' vero — quindi proprio nella configurazione
spedita non protegge nulla.

**Fix:** `CLAMAV_FAIL_CLOSED=true` in `.env.example`. Se serve tollerare
l'outage di ClamAV, distinguere "scanner giu'" (tollerabile) da "file
oltre il limite di stream" (mai tollerabile — l'attaccante lo controlla).

---

## 9. MEDIA — Bundle token Keycloak dentro il cookie di sessione

`[confermato]`

`app/api/auth_routes.py:92`: `sign_token(session.token, settings.session_secret)`.

Cosa sia `session.token` e' tracciabile con certezza:
`OzonOrm.build_auth_user` (riga 1109) assegna
`"token": copy.deepcopy(verified.token_data)`, e `token_data` viene
popolato da `authenticate_user_token` (righe 1170-1173) con
`access_token` **e** `refresh_token`. Il campo `User.token` e' tipizzato
`dict[str, Any] | str` (`BaseModels.py:860`), quindi accoglie il bundle
senza degradare a stringa. **E' il bundle completo.**

`sign_token` usa `itsdangerous.URLSafeTimedSerializer`
(`app/services/cookie_auth.py:10-15`): **firma, non cifra**. Il cookie e'
JSON in base64, decodificabile da chiunque lo ottenga.

`httponly=True` e `secure` (default `True`) limitano il rischio, ma
qualunque canale che espone il cookie senza esporre la sessione — dump
di proxy, backup del browser, log di un intermediario mal configurato,
estensione — regala un refresh token Keycloak a lunga vita.

**Fix:** tenere in cookie solo un identificativo di sessione opaco e
conservare il bundle token server-side, oppure cifrare il payload del
cookie (Fernet / `itsdangerous` + cifratura) invece di limitarsi alla
firma. Nota: il finding 5b sconsiglia di riappoggiarsi alla collection
`user` per lo storage — upstream ha smesso apposta di tenerli li'.

---

## 10. MEDIA — Mongo pubblicato sull'host con credenziali root

`[confermato]`

`docker-compose.yml:52-53`:

```yaml
ports:
  - "22222:27017"
```

Bind su `0.0.0.0` (nessun `127.0.0.1:` davanti), con
`MONGO_INITDB_ROOT_USERNAME`/`PASSWORD`. Il DB e' esposto a tutta la rete
raggiungibile dall'host. Le credenziali di quel DB sono anche quelle
finite nella storia git (finding 3).

Analogamente `app` pubblica `7999:8000` su tutte le interfacce; e'
intenzionale (il reverse proxy sta davanti), ma va verificato che il
firewall dell'host non lasci raggiungere 7999 direttamente scavalcando
il proxy.

**Fix:** `127.0.0.1:22222:27017`, o rimuovere del tutto la
pubblicazione (i servizi si parlano sulla rete `ozon-net`). Stessa cosa
per `7999` se il proxy e' co-locato.

---

## 11. BASSA — `Service.by_name` non applica alcuna ACL

`[confermato]`

`app/services/service.py:396-399`:

```python
async def by_name(self, model: str, name: str):
    compo_model = self.env.get(model)
    return await compo_model.by_name(name)
```

Nessun `_get_model_group_access`, nessun `record_rulse`, nessun field
ACL — a differenza di `load_record` e `list_records`, che li applicano
tutti. E' la scorciatoia che rende possibile il finding 2b.

Severita' BASSA in quanto tale perche' `by_name` non e' esposto
direttamente su una route: e' un helper interno. Ma e' un'arma carica —
ogni nuovo chiamante che gli passi un `model`/`name` influenzato dal
client eredita un bypass ACL completo, come gia' successo con
`get_global_param`.

**Fix:** far passare `by_name` dagli stessi gate di `load_record`, o
rinominarlo `_by_name_unchecked` perche' il costo sia evidente al
prossimo chiamante.

---

## 12. BASSA — `SESSION_SECRET`: fallback per-processo senza controllo

`app/app_settings.py:249-260`. Il commento documenta bene il problema
(ogni processo genera un fallback proprio → i cookie firmati da un worker
sono rifiutati dagli altri), ma niente lo **impedisce**: non c'e' un
check di avvio che alzi un errore se `SESSION_SECRET` e' assente con
`web_concurrency > 1`.

Il fallimento e' fail-closed dal punto di vista della sicurezza (auth
negata, non concessa), quindi il rischio e' di disponibilita' e di
"funziona in dev, si rompe in prod in modo confuso".

**Fix:** validator che alza un errore all'avvio se `SESSION_SECRET` non
e' impostato e non si e' in modalita' dev esplicita.

---

## 13. BASSA — `$regex` client-controlled nell'allowlist query ACL

`app/ozon_env_acl/__init__.py:697-718`. L'impostazione allowlist e'
ottima (vedi "Punti solidi"), ma `$regex` e `$options` sono ammessi e il
valore arriva dal body di `POST /list/{model}`. Una regex patologica su
una collection grande consuma CPU lato Mongo. Non e' catastrofica come il
backtracking PCRE, ma e' un vettore di DoS a basso costo.

**Fix:** limitare lunghezza della regex e vietare i quantificatori
annidati, oppure ancorare le regex a un prefisso (`^...`) per i campi
dove serve la ricerca.

---

## 14. BASSA — Codice morto: autenticazione via header `x-remote-user`

`[confermato — non raggiungibile oggi]`

`app/services/session_auth.py:73-196` (`build_keycloak_session`) e
`:401-410` (`_extract_remote_user`) implementano l'auth trusted-header:
`x-remote-user` viene preso dalla richiesta **cosi' com'e'**,
`_get_or_create_user` crea l'utente se non esiste, e `is_admin` viene
assegnato da `remote_user in admin_uids`.

Ho verificato con grep su tutto `app/`: **nessuna route lo invoca**.
L'unico percorso Keycloak attivo e' `build_keycloak_session_from_tokens`
(`auth_routes.py:85`), che valida il token via `session_app()` → JWKS.
Quindi oggi non e' sfruttabile.

Resta pero' una mina: se qualcuno lo ricollega a una dependency, e
`7999` e' raggiungibile scavalcando il reverse proxy (finding 9),
`curl -H "x-remote-user: <admin>"` da' accesso admin immediato. Non
c'e' nessun controllo che la richiesta provenga davvero dal proxy (IP
allowlist, segreto condiviso, mTLS).

**Fix:** rimuovere le due funzioni. Se la modalita' trusted-header serve
davvero, aggiungere prima la verifica dell'identita' del proxy.

---

## 15. BASSA — `mcp_search` in ascolto su 0.0.0.0 senza identita' propria

`services/mcp_search/service.env`: `MCP_SEARCH_HOST=0.0.0.0`, porta 8090.
Il gateway (`gateway.py:20-30`) non ha credenziali proprie e inoltra
l'`Authorization` del chiamante — scelta **corretta**: l'ACL a valle
(`model_groups_rule`, `record_rulse`, field ACL, query gate) si applica
integralmente, nessun bypass.

L'osservazione e' di esposizione: il server e' un relay non autenticato
verso `/list/{model}` per chiunque sulla rete abbia un bearer valido.
Assicurarsi che 8090 non sia pubblicato fuori da `ozn-network`.

---

## 16. BASSA — Decode JWT senza verifica firma (solo per la scadenza)

`app/services/session_auth.py:369-380` (`_decode_token_expiry`)
decodifica il payload JWT in base64 senza verificare la firma. E' usata
solo per calcolare `sso_expire` in `build_keycloak_session` (codice
morto, finding 12) e in `_resolve_expire_datetime` come fallback quando
`expires_in` manca — **mai** per una decisione di autorizzazione.

Analogamente `KeycloakAuthManager.decode_unverified`
(`ozon-env/ozonenv/core/auth.py:113`) esiste ma non e' chiamata da
nessuna parte (verificato con grep su entrambi i repo).

Nessun impatto oggi. Segnalato perche' resti cosi': se un giorno una di
queste finisce a monte di un controllo di accesso, diventa un bypass
completo. Vale la pena rinominarle con prefisso `_unsafe_`.

---

## 17. INFO — Config di sicurezza dichiarata ma mai letta

`[confermato]`

Due setting rilevanti per la sicurezza sono definiti in `EnvSettings` e
documentati in `.env.example`, ma **nessun codice li legge**
(verificato con grep su `app/` e `workers/`):

- **`runtime_internal_token`** (`app/app_settings.py:416-419`,
  `RUNTIME_INTERNAL_TOKEN` in `.env.example`) — non esiste alcun
  percorso di autenticazione interna che lo consumi. E' config morta:
  chi la valorizza crede di aver protetto un canale che non esiste.
- **`camunda_verify_tls`** (`app/app_settings.py:421-423`) — l'app non
  lo usa mai. La verifica TLS Camunda e' implementata altrove:
  `workers/ozon_camunda_worker/config.py:48` legge `CAMUNDA_VERIFY_TLS`
  direttamente da `os.environ` (default `True`) e lo passa a
  `httpx` (`camunda.py:46`); `app/core/camunda.py:76-78` onora un
  parametro `verify_tls` che pero' nessun chiamante in `app/` gli passa.

Nessun impatto diretto — l'esito e' fail-safe (httpx verifica di
default). Il rischio e' di **falsa assicurazione**: un operatore che
legge `.env.example` conclude che quei controlli sono attivi e
configurabili dall'app, mentre uno non esiste e l'altro vive solo nel
worker.

**Fix:** rimuovere i setting morti, oppure collegarli. Se
`RUNTIME_INTERNAL_TOKEN` serviva a proteggere l'endpoint `run` dello
scheduler, verificare che quella protezione esista davvero da qualche
altra parte (il commento in `.env.example` dice che il gate e' il JWT
Keycloak — in tal caso il setting va solo cancellato).

---

## 18. INFO — Documento di sicurezza citato ma inesistente

`.env.example` (2 volte) e tre commenti nel codice
(`session_auth.py:95`, `websocket_router.py:~118`) rimandano a
`docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md`, citando "finding #5", "#7"
e "#8". Il file **non esiste** in `docs/` e non compare in nessun commit
(`git log --all --full-history -- '*SECURITY*'` → vuoto).

I finding citati risultano comunque affrontati nel codice (il fallback
`SESSION_SECRET`, la gestione di `user.token` come dict, il divieto di
token in query string sul WS), quindi il lavoro e' stato fatto — manca
solo il documento. Recuperarlo o togliere i riferimenti pendenti.

---

## Punti solidi (verificati, nessuna azione)

Vale la pena registrarli perche' sono le parti che reggono il resto:

- **Verifica JWT corretta.** Firma via JWKS (`PyJWKClient`), algoritmi
  vincolati a RS256, issuer verificato. Nessun `verify_signature=False`
  nel percorso di autenticazione. (`ozon-env/ozonenv/core/auth.py:139-170`)
- **ACL fail-closed.** `model_group_access`
  (`app/ozon_env_acl/__init__.py:958-989`) nega tutto se nessuna riga
  copre un gruppo dell'attore — model senza regole configurate incluso.
- **Query field-ACL gate ben progettato.** Allowlist di operatori
  (`:697-718`) invece di blocklist: `$where`, `$expr`, `$function`,
  `$accumulator` sono strutturalmente impossibili, non "dimenticati". In
  piu' `assert_order_field_acl` chiude anche l'oracle via `sort` —
  dettaglio che sfugge quasi sempre.
- **Nessuna primitiva di esecuzione codice.** Nessun `eval`/`exec`,
  nessun `pickle.load`, nessun `shell=True`, nessuna estrazione di
  archivi (quindi nessuna superficie zip-slip). L'unico subprocess usa
  `create_subprocess_exec` con argv esplicito — niente iniezione di
  comando (il problema del finding 1 e' l'autorizzazione, non
  l'iniezione).
- **Upload robusto.** Limite di dimensione applicato **durante** lo
  streaming, prima del buffering (`attachments.py:71-87`);
  sanitizzazione del filename; nomi di storage uuid4; guardia
  `relative_to(root)` contro il traversal.
- **Igiene dei log.** L'header `Authorization` e' mascherato
  (`app/middleware/logging.py:9-17`); `_session_response_data`
  (`routes.py:68-69`) rimuove `token`, `sso_token`, `sso_refresh`,
  `claims` dalla risposta di sessione.
- **OAuth2 `state` validato** con cookie firmato e TTL 600s
  (`auth_routes.py:74-81`); CSRF double-submit sulle scritture in
  modalita' cookie (`app/deps/app_env.py:429-444`).
- **CI pulita.** Nessun segreto nei workflow, nessuna injection da input
  non fidati in `run:`.

---

## Ordine di intervento consigliato

Le ALTE e le MEDIE sono state applicate (vedi "Stato dei fix"). Resta a
carico dell'operatore, in quest'ordine:

1. **Ruotare le credenziali del finding 3.** Nessuna modifica al codice
   le invalida. Se qualche record `global_params` contiene chiavi di
   servizi esterni, ruotare anche quelle: prima del fix del finding 2b
   erano estraibili da qualunque utente autenticato.
2. **Valorizzare `OZON_TOKEN_AUDIENCE`** dopo aver allineato l'audience
   su Keycloak (finding 6).
3. **Verificare `EXTERNAL_BASE_URL`** o impostare `WS_ALLOWED_ORIGINS`
   prima del deploy, altrimenti i WebSocket via cookie vengono rifiutati
   (finding 7).
4. **Mettere in conto un logout di massa** al primo deploy: il cookie di
   sessione cambia formato (finding 9).

Poi, come manutenzione, i finding 11-18.
