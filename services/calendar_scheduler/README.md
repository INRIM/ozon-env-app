# calendar-scheduler

Worker scheduler leggero che sostituisce il container legacy systemd.

**Agnostico sull'app_code.** Legge i task `calendar` (tipo `task`) di TUTTI gli
app_code via **ozon-env** (data plane), li schedula con APScheduler e li esegue
chiamando l'endpoint run dell'app, passando l'`app_code` del singolo record.
Niente systemd.

## Architettura

- **Dati** (lettura calendar, lock, `next`): model layer ozon-env su un'unica
  Mongo condivisa. I record di tutti gli app_code sono visibili (le query
  calendar non filtrano per app_code). Lock nel campo `data_value`.
- **Esecuzione**: HTTP `POST /client/run/calendar_tasks/{rec_name}?app_code=...`.
  L'azione gira nel runtime app coi plugin dell'app_code del task; `stato`/`last`/
  `active` li scrive quell'endpoint, il worker scrive solo `next` e il lock.

## Flusso

1. sync ogni `SCHEDULER_POLL_INTERVAL`s: `list_tasks()` via ozon-env;
2. per ogni task applica `action` (`add`/`pause`/`resume`/`remove`) sul job;
3. allo scatter: `lock` (ozon-env) -> `run` (HTTP) -> `next` (ozon-env) ->
   rilascio `lock` (ozon-env);
4. nessun retry automatico (decisione): su errore si attende la prossima run;
5. parse error della schedule -> `stato=erroreConfigurazione` (no fallback).

## Auth (keycloak M2M)

L'endpoint run verifica il bearer come **JWT keycloak** (jwks): un token statico
NON passa `jwt.decode` (401). Il worker usa **client_credentials** per ottenere
JWT a vita breve, rinnovati prima della scadenza. Unico segreto: il
`client_secret`.

Requisiti keycloak/app:

- client keycloak con **service account** abilitato;
- l'`uid` del service account (`service-account-<client>`) deve essere negli
  **`admins` di OGNI app_code**, altrimenti l'ACL nega le scritture (403);
- `aud` del token allineato a `OZON_TOKEN_AUDIENCE` dell'app.

## Setup keycloak (service client + audience)

Lo scheduler **consuma** `SCHEDULER_OAUTH_*` a runtime, ma NON configura keycloak.
La configurazione (creare il client M2M + audience verso il client app) si fa col
servizio interattivo dedicato **`manager/keycloak-manager`**: genera un
`kc-env.var` (con prefisso `SCHEDULER`) da incollare in questo `.env`.

Promemoria: il `service-account-<m2m-client-id>` va aggiunto agli `admins` di OGNI
app_code, altrimenti la run va in 403 (ACL). E `OZON_TOKEN_AUDIENCE` (enforcement
aud app-wide) va abilitato **solo dopo** che tutti i client emettono l'aud
(altrimenti 401 sui login). Vedi `manager/keycloak-manager/README.md`.

## Persistenza

Job persistenti su jobstore SQLAlchemy (`SCHEDULER_JOBSTORE_URL`, default
SQLite in `/data`). Sopravvivono ai restart. Montare `/data` come volume.

## Configurazione (solo env, nessun segreto versionato)

Mongo/modelli: gestiti da ozon-env (`MONGO_*`, `MODELS_FOLDER` — vedi
`OzonEnvCoreSettings`). Parametri worker:

| Variabile | Default | Note |
| --- | --- | --- |
| `SCHEDULER_RUN_BASE_URL` | — | richiesto, base url app per l'endpoint run |
| `SCHEDULER_OAUTH_TOKEN_URL` | — | richiesto, token endpoint keycloak (client_credentials) |
| `SCHEDULER_OAUTH_CLIENT_ID` | — | richiesto, client keycloak con service account |
| `SCHEDULER_OAUTH_CLIENT_SECRET` | — | richiesto, **unico segreto** (secret runtime) |
| `SCHEDULER_OAUTH_AUDIENCE` | `""` | `aud` del token; deve combaciare con `OZON_TOKEN_AUDIENCE` dell'app |
| `SCHEDULER_OAUTH_SCOPE` | `""` | scope opzionale |
| `SCHEDULER_POLL_INTERVAL` | `45` | secondi tra i sync |
| `SCHEDULER_LOCK_TTL` | `1800` | TTL lock in secondi |
| `SCHEDULER_HTTP_TIMEOUT` | `30` | timeout HTTP run |
| `SCHEDULER_TZ` | `Europe/Rome` | timezone schedule |
| `SCHEDULER_JOBSTORE_URL` | `sqlite:////data/calendar_scheduler.sqlite` | jobstore APScheduler |
| `SCHEDULER_MISFIRE_GRACE` | `300` | misfire grace time |
| `SCHEDULER_HEALTH_FILE` | `/tmp/calendar_scheduler_health` | heartbeat healthcheck |

## Notazioni schedule supportate (V1)

`now`, data ISO assoluta, crontab 5 campi, alias (`@hourly`/`@daily`/
`@weekly`/`@monthly`), systemd daily `*-*-* HH:MM:00` e `HH:MM:00`.
Notazioni non supportate -> `erroreConfigurazione` (nessun fallback silenzioso).

## Test

```bash
uv run python -m pytest tests/
```
