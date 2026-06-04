# Deploy ozon-env-app con Ansible

Wrapper degli script legacy in Ansible, sul modello di `mci_app`.

Primo argomento = target env (`local` | `dev` | `prod`, default `local`):

- `./build.sh [env]` — `uv lock` (opzionale) + build immagini (`ozonapp.db` + `ozonapp.app`)
- `./deploy.sh [env] <admin_uid>` — setup-db + bootstrap (plugin + seed settings/admin) + avvia stack
- `./update.sh [env] [service]` — ricrea un servizio del compose (default `app`)
- `./stop.sh` — ferma lo stack locale

`local` (default) gira **in-place** nel repo (`ozonapp_sync_sources=false`).
`dev`/`prod` usano `ansible/inventories/<env>/hosts.yml` (host remoto, `sync_sources=true`).
`ANSIBLE_INVENTORY` resta come override esplicito dell'inventory.

```bash
./build.sh                 # local
./build.sh dev
./deploy.sh a.gerace       # local
./deploy.sh prod a.gerace
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

`build` costruisce **solo le immagini**, non avvia lo stack: `database/scripts/init_db.js`
e generato da `setup_db.sh` in fase di deploy, e un `up` su checkout fresco creerebbe
una dir vuota al posto del bind-mount. Lo stack viene avviato da `deploy`.

### deploy (setup-db + bootstrap)
```bash
./deploy.sh a.gerace
```
Sequenza: assert env+admin → `setup_db.sh` (immagine db + `database/scripts/init_db.js`)
→ `bootstrap.sh <uid>` (avvia db, attende ping, `bootstrap.py --admin`) → `up -d --force-recreate`.

Richiede l'immagine `ozonapp.app:latest` gia costruita (esegui prima `./build.sh`),
oppure forza la build nel deploy:
```bash
./deploy.sh a.gerace -e ozonapp_deploy_build=true
```
`bootstrap.py` e idempotente (merge admins create-if-missing, plugin upsert):
ri-eseguire un deploy e sicuro.

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
./deploy.sh prod a.gerace
# override inventory esplicito:
ANSIBLE_INVENTORY=./ansible/inventories/prod/hosts.yml ./deploy.sh prod a.gerace
```

Oppure i playbook diretti:
```bash
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/build.yml
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/deploy.yml -e ozonapp_admin_uid=a.gerace
ansible-playbook -i ansible/inventories/dev/hosts.yml ansible/playbooks/update.yml -e ozonapp_update_service=app
```

## Gestione `.env` (no-clobber)

Tre modi, in ordine di precedenza:

1. `OZONAPP_DOTENV_CONTENT` (raw, da CI) → scrive `.env`
2. `OZONAPP_ENV_VARS` (mappa YAML/JSON, da CI) → template `env.j2` → `.env`
3. nessun contenuto CI → **il `.env` esistente NON viene mai sovrascritto**
   (contiene segreti reali). Se assente, copia il fallback `.env.example`.

Chiavi obbligatorie validate in deploy: `APP_CODE`, `MONGO_USER`, `MONGO_PASS`,
`MONGO_DB`, `SESSION_SECRET`.

## Note

- Stack single-project: **nessun `-p`** (gli script riusati non lo usano).
- `database/scripts/init_db.js` viene eseguito da MongoDB solo alla **prima**
  inizializzazione del volume `mdbdata` (fresh volume).
- Health check: `GET :7999/openapi.json` (endpoint pubblico, non `/dashboard`
  che e sotto auth). Disattiva con `-e ozonapp_verify_endpoint=false`.
