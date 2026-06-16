# keycloak-manager

Servizio **interattivo** e **agnostico** per configurare keycloak passo-passo e
generare le env var del consumer. Dockerizzato, con un suo `.env`. Backend:
[INRIM/kc-provision](https://github.com/INRIM/kc-provision) (dep git).

## Cosa fa

Pipeline a menu (idempotente, verifica-poi-crea):

1. **Client APP** (resource server): crea il client app se assente.
2. **Client M2M → client APP**: crea il client M2M (client_credentials) + secret,
   crea un **client scope audience** con mapper **client-audience** verso il
   client app (`included.client.audience=<app-client-id>`) e lo assegna come
   default al client M2M (e, opzionale, ad altri consumer).
3. **Genera `kc-env.var`**: scrive le env var da incollare nell'`.env` del consumer.

L'**audience** = clientId del client app (pattern resource-server): il token M2M
porta `aud=<app-client-id>`; l'app verifica `aud` = proprio clientId.

## Uso

```bash
cp .env.example .env      # compila KC_SERVER_URL, KC_REALM, KC_ADMIN_USER...
./run.sh                  # pipeline interattiva; output in ./out/kc-env.var
```

`docker compose run` alloca un TTY: la pipeline è interattiva. La password admin
può stare in `.env` (`KC_ADMIN_PASSWORD`) o essere chiesta a runtime (getpass).

## `kc-env.var` generato

Prefisso configurabile (`KC_ENV_PREFIX`): vuoto → `OAUTH_*`; es `SCHEDULER` →
`SCHEDULER_OAUTH_*` (pronti per il calendar_scheduler).

```
{P}OAUTH_TOKEN_URL=...        {P}OAUTH_CLIENT_ID=...
{P}OAUTH_CLIENT_SECRET=...    {P}OAUTH_AUDIENCE=<app-client-id>
TOKEN_AUDIENCE=<app-client-id>   # -> OZON_TOKEN_AUDIENCE lato app
```

### ⚠️ Invariante audience + enforcement

Lo stesso `<app-client-id>` deve stare nel mapper, in `OAUTH_AUDIENCE` e in
`OZON_TOKEN_AUDIENCE` (l'app fa string-compare su `aud`). Abilitare
`OZON_TOKEN_AUDIENCE` lato app **solo dopo** che TUTTI i client che la chiamano
emettono l'aud, altrimenti 401 a tappeto (login utente compresi). Il manager
configura ed emette, **non** abilita l'enforcement (scelta dell'operatore).

## Config (.env, agnostico)

| Var | Note |
| --- | --- |
| `KC_SERVER_URL` | base url keycloak |
| `KC_REALM` | realm gestito |
| `KC_ADMIN_REALM` | realm dove autentica l'admin (service account nel realm gestito → `KC_REALM`; admin-cli → `master`) |
| `KC_ADMIN_CLIENT_ID` / `KC_ADMIN_CLIENT_SECRET` | **preferito**: service account (client_credentials), es `admin-rest-client` con ruoli `realm-management`. Se il secret è valorizzato vince sul password grant |
| `KC_ADMIN_USER` / `KC_ADMIN_PASSWORD` | fallback admin-cli password grant (password vuota → prompt) |
| `KC_VERIFY_TLS` | default `true` |

`.env` = solo connessione + secret admin. **Prefisso** delle env var e **file di
output** si scelgono nella **pipeline (step 3)**, non in config: così, impostati i
secret, il service non si tocca più. Output in `/out` (volume → `./out`).

## Test

```bash
uv run python -m pytest tests/
```
