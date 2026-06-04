

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

## Deploy (Ansible)

Build / deploy / update via Ansible. Primo argomento = target env
(`local` | `dev` | `prod`, default `local`). Dettagli in
[`ansible/README.md`](ansible/README.md).

```bash
./build.sh  [local|dev|prod]              # build immagini (default local)
./deploy.sh [local|dev|prod] <admin_uid>  # setup-db + bootstrap + avvia stack
./update.sh [local|dev|prod] [service]    # ricrea un servizio (default app)
./stop.sh                                 # ferma lo stack locale
```

Esempi:

```bash
./build.sh                  # local
./build.sh prod
./deploy.sh a.gerace        # local (admin uid = a.gerace)
./deploy.sh prod a.gerace
./update.sh dev app
```

| Script | Cosa fa | Note |
|--------|---------|------|
| `build.sh` | `uv lock` (opt) + `build_imges.sh` → immagini `ozonapp.db` + `ozonapp.app` + `ozonapp.mail_sender` | **solo immagini, non avvia** lo stack. `uv lock`/build richiedono **VPN** (`--network host`). |
| `deploy.sh` | assert env+admin → `setup_db.sh` → `bootstrap.sh <uid>` → `up -d --force-recreate` → health check | richiede `ozonapp.app:latest` gia buildata (o `-e ozonapp_deploy_build=true`). `bootstrap` idempotente. |
| `update.sh` | ricrea il servizio del compose (default `app`) | `-e ozonapp_update_build=true` per rebuild immagini prima. |
| `stop.sh` | `docker compose stop` (locale) | |

### Target env

- **`local`** (default) → in-place nel repo (`ozonapp_sync_sources=false`).
- **`dev` / `prod`** → `ansible/inventories/<env>/hosts.yml`, host remoto,
  clone/aggiornamento in `ozonapp_project_root` (`ozonapp_sync_sources=true`).
- `ANSIBLE_INVENTORY=...` resta come override esplicito dell'inventory.

Il primo argomento e' interpretato come env solo se ∈ `{local,dev,prod}`:
`./deploy.sh a.gerace` resta local con admin `a.gerace`.

### Opzioni utili

```bash
./build.sh -e ozonapp_uv_lock=true          # esegue uv lock (VPN)
./deploy.sh prod a.gerace -e ozonapp_deploy_build=true   # rebuild prima del deploy
./deploy.sh a.gerace -e ozonapp_verify_endpoint=false    # salta health check
```

### Gestione `.env`

Precedenza: `OZONAPP_DOTENV_CONTENT` (raw CI) → `OZONAPP_ENV_VARS` (mappa CI,
template) → fallback `.env.example`. **Un `.env` esistente non viene mai
sovrascritto** (contiene segreti reali). Chiavi obbligatorie in deploy:
`APP_CODE`, `MONGO_USER`, `MONGO_PASS`, `MONGO_DB`, `SESSION_SECRET`.

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
