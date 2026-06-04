# mail_sender

Worker **pull** che svuota la coda `message_queue`.

## Flusso

1. Connessione al DB via **ozon-env** (`OzonEnvCoreSettings.from_env()` → stesse
   env `MONGO_*`/`APP_CODE`/`MODELS_FOLDER` dell'app).
2. Poll dei record `message_queue` con `stato = da_inviare`.
3. Per ognuno:
   - carica `mail_template` (per nome) e `mail_server_out` (`template.server`);
   - carica il record correlato `rel_rec_name` dal `template.model`;
   - render Jinja di `subject` / `recipient` / `corpoDellaMail` con context
     `{data, form, user, app}`, corpo wrappato nel base template
     (`mail_sender/templates/mail_base_template.html`, placeholder `{{ html|safe }}`);
   - invio SMTP via **smtplib** mappando i campi `mail_server_out`
     (TLS/SSL/credenziali);
   - update `stato` → `inviato` / `in_errore` e `logs` (traceback in errore).
4. `sleep(MAIL_POLL_INTERVAL)` e ripeti.

Render e invio sono nel service (architettura pull): l'app crea solo i record
`da_inviare` (endpoint `/message_queue/enqueue` o hook automatico su salvataggio).

## Config (env)

| Var | Default | Note |
|-----|---------|------|
| `MAIL_POLL_INTERVAL` | `30` | secondi tra i cicli |
| `OZON_APP_NAME` / `APP_CODE` | `App` | usato come `app_name` nel base template |
| `EXTERNAL_BASE_URL` | `""` | esposto come `app.base_url` nel context |
| `MAIL_BASE_TEMPLATE` | template incluso | override path del base template |
| `MONGO_*`, `APP_CODE`, `MODELS_FOLDER` | — | lette da ozon-env per la connessione |

## Run locale

```bash
uv sync
uv run python -m mail_sender.main
```

## Test

```bash
uv run python -m pytest tests/
```

## Compatibilita'

Porting del vecchio `web-client/core/MailService.py` (che inviava inline al
form-post con fastapi_mail): stessi placeholder e stesso base template, ma
disaccoppiato (pull), senza dipendenze extra (solo `smtplib` stdlib + Jinja2)
e con gestione errori per-record su `stato`/`logs`.
