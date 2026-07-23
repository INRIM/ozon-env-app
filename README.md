

<h2 align="center">ozon-env</h2>

<p align="center">
<a href="https://github.com/archetipo/ozon-env"><img alt="Actions Status" src="https://github.com/archetipo/ozon-env/workflows/ci/badge.svg"></a>
<a href="https://coveralls.io/github/archetipo/ozon-env?branch=main"><img alt="Coverage Status" src="https://coveralls.io/repos/github/archetipo/ozon-env/badge.svg?branch=main"></a>
<a href="https://github.com/archetipo/ozon-env/blob/main/LICENSE"><img alt="License: MIT" src="https://black.readthedocs.io/en/stable/_static/license.svg"></a>
<a href="https://github.com/archetipo/ozon-env"><img alt="Code style: black" src="https://img.shields.io/badge/code%20style-black-000000.svg"></a>
</p>

ozon-env lib is a api system to interact with Service App project

For information about the Service App project,
see https://github.com/INRIM/service-app

## Deploy locale

Il deploy locale non richiede Ansible:

```bash
./deploy.sh
```

Lo script preserva il `.env` esistente (se manca lo crea da `.env.example`),
costruisce le immagini con `build_imges.sh`, crea la rete esterna
`ozn-network` se necessaria e avvia `docker compose up -d`. Opzioni:

```bash
./deploy.sh --skip-build       # riusa le immagini presenti
./deploy.sh --bootstrap        # esegue bootstrap.sh dopo l'avvio
./deploy.sh --no-bootstrap     # non propone il bootstrap interattivo
```

Per fermare lo stack resta `./stop.sh` (`docker compose stop`). Il prototipo
Ansible e' conservato solo localmente in `ansible-deploy/`, che e' ignorata da
Git e non e' la sorgente del deploy locale.

## CI (GitLab + GitHub)

Due pipeline parallele, stesso scope immagini, build su branch `master`/`1.0`:

- **GitLab** (`.gitlab-ci.yml`, registry gitlab.ininrim.it) — un job per immagine,
  `IMG_REF` = `${CI_REGISTRY_IMAGE}` (+ sub-path per le non-app), tag
  `${CI_COMMIT_SHORT_SHA}` + `latest`.
- **GitHub** (`.github/workflows/docker-build.yml`, GHCR) — job unico a matrix,
  stesso set di immagini, tag short-sha + `latest`, build multi-platform
  `linux/amd64,linux/arm64` (via `docker/setup-qemu-action`).

| Immagine | Dockerfile | GitLab `IMG_REF` | GHCR path |
|----------|-----------|-------------------|-----------|
| app | `Dockerfile` | `${CI_REGISTRY_IMAGE}` | `ghcr.io/inrim/ozon-env-app` |
| db | `database/Dockerfile-mongo` | `${CI_REGISTRY_IMAGE}/db` | `.../db` |
| mail-sender | `services/mail_sender/Dockerfile` | `${CI_REGISTRY_IMAGE}/mail-sender` | `.../mail-sender` |
| calendar-scheduler | `services/calendar_scheduler/Dockerfile` | `${CI_REGISTRY_IMAGE}/calendar-scheduler` | `.../calendar-scheduler` |
| identity-manager | `services/identity_manager/Dockerfile` | `${CI_REGISTRY_IMAGE}/identity-manager` | `.../identity-manager` |
| keycloak-manager | `manager/keycloak-manager/Dockerfile` | `${CI_REGISTRY_IMAGE}/keycloak-manager` | `.../keycloak-manager` |

`workers/ozon_camunda_worker` e `services/people_sync` **non** sono in
nessuna delle due pipeline: hanno repo e CI propri (`services/people_sync`
ha gia' il suo `.gitlab-ci.yml`, single-image, nel suo repo). Quando anche
`mail_sender`/`calendar_scheduler`/`identity_manager` verranno splittati in
repo dedicati, i job corrispondenti vanno tolti da entrambi i file qui.

### Services Registry core

I companion (`mail_sender`, `calendar_scheduler`) non sono piu' nel compose
principale. Ogni companion ha un `manifest.json` e un `docker-compose.yml`
autonomo sotto `services/<name>/`, collegato alla rete esterna `ozn-network`.

Il registro e' una funzione del core applicativo, non un servizio separato:
usa i modelli `service_registry` e `service_registry_repo` ed espone API sotto
`/services/registry`. Il build globale crea solo `ozonapp.db` e `ozonapp.app`;
i companion vengono buildati dal loro compose autonomo quando il registry li
avvia.

### Core Webhooks

`ozon-env-app` puo' chiamare servizi esterni per integrare logiche specifiche
di ACL, gestione utenti e sincronizzazioni senza spostare la competenza dei
dati fuori dal core applicativo.

Variabili:

```bash
CORE_WEBHOOKS_ENABLED=true
CORE_WEBHOOKS_JSON='[{"url":"http://acl-service:8000/webhooks","events":["data.before_write","user.before_create"]}]'
CORE_WEBHOOKS_FAIL_MODE=open
CORE_WEBHOOKS_TIMEOUT_SECONDS=5
CORE_WEBHOOKS_SIGNING_SECRET=...
```

Eventi supportati:

- `data.before_write`: prima di ACL locale e upsert; puo' negare con
  `{"allow": false}` o riscrivere il payload con `{"payload": {...}}`.
- `data.after_write`: dopo upsert.
- `data.after_read`: dopo lettura record.
- `data.after_list`: dopo lettura lista.
- `user.before_create`: prima della creazione utente.
- `user.after_create`: dopo creazione utente.
- `user.session.persist`: dopo persistenza sessione utente.

### Camunda E2E Test

Il test reale Camunda e' opt-in e non gira nella suite default. Usa il BPMN
`attivita/test_request.bpmn`, i job type con typo contrattuali
(`ckeck_user`, `sed_message_approved`, `sed_message_refused`) e pilota il flusso
tramite gli endpoint app `/gateway/camunda/...`.

Il compose di test usa Camunda senza auth (`CAMUNDA_AUTH_ENABLED=false`):
nessun `CAMUNDA_OAUTH_*`, `CAMUNDA_CLIENT_*` o token Camunda e' richiesto.
`APP_CODE` e' il codice app/model (`test_request` di default). `APP_TOKEN` e'
invece la sessione dell'app `ozon-env-app` necessaria per chiamare gli endpoint
applicativi: non e' `APP_CODE` e non viene salvato nel repository.

```bash
APP_TOKEN=<token-sessione-app> ./run_camunda_integration_test.sh
```

Opzioni utili:

```bash
APP_TOKEN=<token-sessione-app> BUILD_IMAGES=1 ./run_camunda_integration_test.sh
./run_camunda_integration_test.sh logs
./run_camunda_integration_test.sh down
```

Lo script genera `tests/camunda_e2e/.env` runtime, avvia lo stack
`docker-compose-test.yml` e lancia il runner pytest nel compose. Il gateway non
aggiunge header OAuth a Tasklist e apre Zeebe senza interceptor bearer nel
profilo di test.
- `calendar.task.completed`: dopo run di un calendar task con esito ok.
- `calendar.task.failed`: dopo run di un calendar task con esito errore.

> `calendar.task.*` sono **notifiche di esito** (post-run), non policy: emessi
> fail-safe (un errore della webhook non blocca la run, a prescindere da
> `FAIL_MODE`). Payload: `{rec_name, task, task_record_name, status, run_id,
> started_at, finished_at, message}`.

Il match degli `events` è esatto (o `*` per tutti): per ricevere la famiglia
calendar elencare entrambi gli eventi, es. `"events":
["calendar.task.completed","calendar.task.failed"]`.

## Installation

The source code is currently hosted on GitHub at:
https://github.com/archetipo/ozon-env

### PyPI - Python Package Index

Binary installers for the latest released version are available at the [Python
Package Index](https://pypi.python.org/pypi/ozon-env)

```sh
pip(3) install ozon-env
```

```sh
poetry install --without dev
```

or

### Source Install with Poetry (recommended)

Convenient for developers. Also useful for running the (unit)tests.

```sh
git clone https://github.com/archetipo/ozon-env.git
```

add virtualenv **env** Pytnon >=3.10

```
pip install poetry
poetry install
```

### Source Install with pip

Optional dependencies need to be installed separately.

```sh
pip(3) install git+https://github.com/archetipo/ozon-env
```

### Tests, Coverage and Code style

```
./run_test.sh
```

## License

[MIT](LICENSE)

## Contributing

All contributions, bug reports, bug fixes, documentation improvements,
enhancements and ideas are welcome.
