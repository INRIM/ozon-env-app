# Deploy ozon-env-app con Ansible

Wrapper degli script legacy in Ansible, sul modello di `mci_app`.

Primo argomento = target env (`local` | `dev` | `prod`, default `local`):

- `./build.sh [env]` — `uv lock` (opzionale) + build immagini (`ozonapp.db` + `ozonapp.app`)
- `./deploy.sh [env]` — build immagini ozon-env-app + `docker compose up -d`
- `./update.sh [env] [service]` — ricrea un servizio del compose (default `app`)
- `./stop.sh` — ferma lo stack locale

`local` (default) gira **in-place** nel repo (`ozonapp_sync_sources=false`).
`dev`/`prod` usano `ansible/inventories/<env>/hosts.yml` (host remoto, `sync_sources=true`).
`ANSIBLE_INVENTORY` resta come override esplicito dell'inventory.

```bash
./build.sh                 # local
./build.sh dev
./deploy.sh                # local
./deploy.sh prod
./update.sh dev app
```

## Azioni

### build
```bash
./build.sh
# con uv lock (richiede VPN per ozon-env-api):
./build.sh -e ozonapp_uv_lock=true
```
`build_imges.sh` usa `--network host`: serve la **VPN** attiva per scaricare
`ozon-env` (github) e `ozon-env-api` (gitlab interno).

`build` costruisce **solo le immagini**, non avvia lo stack.

### deploy
```bash
./deploy.sh
```
Sequenza: build immagini `ozonapp.db` + `ozonapp.app` → crea rete Docker esterna se manca → `docker compose up -d`.

Per saltare la build nel deploy:
```bash
./deploy.sh -e ozonapp_deploy_build=false
```

### update
```bash
./update.sh            # ricrea il servizio app
./update.sh app -e ozonapp_update_build=true   # rebuild immagini poi ricrea
```

## Deploy remoto

Inventory disponibili:

- `ansible/inventories/dev/hosts.yml`
- `ansible/inventories/prod/hosts.yml`

In remoto `ozonapp_sync_sources=true`: il repo viene clonato/aggiornato in
`ozonapp_project_root` (default `/opt/ozon-env-app`).

```bash
./deploy.sh prod
# override inventory esplicito:
ANSIBLE_INVENTORY=./ansible/inventories/prod/hosts.yml ./deploy.sh prod
```

Oppure i playbook diretti:
```bash
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/build.yml
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/deploy.yml
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/update.yml -e ozonapp_update_service=app
```

## Gestione `.env` (no-clobber)

Tre modi, in ordine di precedenza:

1. `OZONAPP_DOTENV_CONTENT` (raw, da CI) → scrive `.env`
2. `OZONAPP_ENV_VARS` (mappa YAML/JSON, da CI) → template `env.j2` → `.env`
3. nessun contenuto CI → **il `.env` esistente NON viene mai sovrascritto**
   (contiene segreti reali). Se assente, copia il fallback `.env.example`.

La validazione delle chiavi `.env` in deploy e' opt-in:
```bash
./deploy.sh -e ozonapp_validate_deploy_env=true
```

## Note

- Stack single-project: **nessun `-p`** (gli script riusati non lo usano).
- `database/scripts/init_db.js` viene eseguito da MongoDB solo alla **prima**
  inizializzazione del volume `mdbdata` (fresh volume).
- Health check: disabilitato di default. Se attivato con
  `-e ozonapp_verify_endpoint=true`, usa `GET :7999/openapi.json`
  (endpoint pubblico, non `/dashboard` che e sotto auth).
