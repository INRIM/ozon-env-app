# api_gateway

Gateway **service-to-service** generico fra `ozon-env-app` e le API esterne.

```
browser --> ozon-env-app --(x-ozon-s2s-token)--> api_gateway --(credenziali upstream)--> API esterna
```

Il gateway custodisce le credenziali delle API esterne e inoltra le
risposte **come arrivano**: non conosce le select. La decodifica
label/value la fa `ozon-env-app` dalle `properties` del component.

## Config

Cartella `config/` montata in `/app/config` (override del file principale
con `API_GATEWAY_CONFIG`). Nell'immagine **non** c'e' config: arriva solo
dal volume. Si versionano solo gli esempi:

| File | Versionato | Contenuto |
|---|---|---|
| `gateway.json.example` | si | `{"upstreams": {}}` |
| `upstream.json.example` | si | template di un upstream |
| `gateway.json` | no | elenco upstream -> file |
| `<upstream>.json` | no | config di una API esterna |

Un **upstream** e' un'API esterna, con un file suo; le sue **resources**
sono l'unico elenco di path raggiungibili: il gateway non inoltra path
liberi.

`config/gateway.json`:

```json
{"upstreams": {"people": "people.json"}}
```

Il valore e' un nome file nella stessa cartella (niente path: `../x` o
`/etc/x` bloccano l'avvio). Un oggetto al posto del nome file vale come
config inline.

`config/people.json`:

```json
{
  "base_url": "${PEOPLE_URL}",
  "headers": {"x-key": "${PEOPLE_KEY}"},
  "cache_ttl": 300,
  "resources": {
    "rooms":   {"path": "/api/get_rooms"},
    "persons": {"path": "/api/get_addressbook/"},
    "persons_by_workmodes": {"path": "/api/get_persons_by_workmodes", "allow_post": true}
  }
}
```

Primo avvio:

```bash
cp config/gateway.json.example config/gateway.json
cp config/upstream.json.example config/people.json   # poi compilare
# e aggiungere "people": "people.json" in gateway.json
```

| Campo | Dove | Note |
|---|---|---|
| `base_url` | upstream | http(s), obbligatorio |
| `headers` | upstream | header statici verso l'upstream |
| `auth` | upstream | alternativo/aggiuntivo agli header: `{"type": "client_credentials", "token_url", "client_id", "client_secret", "audience"?, "scope"?}` (M2M keycloak, bearer rinnovato prima della scadenza) |
| `cache_ttl` | upstream | secondi, solo GET, chiave = risorsa + query string. `0` = off. Default 300 |
| `timeout` | upstream | override di `API_GATEWAY_HTTP_TIMEOUT` |
| `path` | resource | copiato **verbatim** dall'API esterna (People non e' uniforme sugli slash finali: sbagliarli da' 307/404) |
| `allow_post` | resource | default `false`: POST su una risorsa non abilitata = **405**. E' il config a decidere cosa accetta un body |
| `description` | resource | solo per l'indice `GET /` |

Chiavi sconosciute nel config = errore all'avvio (un refuso come `allowpost`
non deve abilitare/disabilitare niente in silenzio).

`${VAR}` nelle stringhe viene risolto dall'env all'avvio. **Una var
mancante o vuota blocca l'avvio**: un header `x-key` vuoto darebbe 401
dall'upstream e select vuote senza nessun segnale.

Aggiungere una risorsa o un upstream: modificare/aggiungere il file in
`config/` e riavviare il container, nessun rebuild.

## Endpoint

| Metodo | Path | Auth | Note |
|---|---|---|---|
| GET | `/health` | no | healthcheck container |
| GET | `/` | si | indice `upstream/resource` con `allow_post` |
| GET | `/{upstream}/{resource}` | si | query string inoltrata, risposta cachata |
| POST | `/{upstream}/{resource}` | si | solo se `allow_post: true`; body JSON inoltrato, mai cachato |

Errori: `404` risorsa non in config, `405` POST non abilitato, `400` body
non JSON, `502` upstream non 2xx / non JSON / irraggiungibile.

## Configurazione del component (lato app)

L'URL va scritto **completo** nel component: ozon-env passa all'app solo
`url`, gli header e le `properties`.

```json
{
  "dataSrc": "url",
  "data": {"url": "http://api-gateway:8080/people/rooms"},
  "properties": {"label": "name,parent_building_code", "id": "id"}
}
```

- `properties.label`: campo (o campi separati da virgola, uniti con uno
  spazio) da mostrare; `properties.id`: campo salvato come value.
- POST: `properties.method = "POST"` e `properties.body` = JSON (stringa o
  oggetto) inoltrato come body.
- Nessun header sul component: la credenziale S2S la mette l'app da env var.

Nel `.env` di `ozon-env-app`:

```
REMOTE_SELECT_ALLOWED_HOSTS=api-gateway
REMOTE_SELECT_S2S_TOKEN=<stesso valore di API_GATEWAY_S2S_TOKEN>
REMOTE_SELECT_S2S_HEADER=x-ozon-s2s-token
```

> `REMOTE_SELECT_ALLOWED_HOSTS` **deve** contenere `api-gateway`: con
> allowlist vuota l'app non invia il token, il gateway risponde 401 e la
> select torna vuota **senza errore visibile**.

L'hostname `api-gateway` e' un alias di rete nel compose: resta stabile
qualunque sia `STACK_NAME`.

## Avvio

```bash
./run.sh          # token S2S + var del config mancanti, poi docker compose up -d --build
```

Il service non pubblica porte: e' raggiungibile solo dalla rete docker
`${NETWORK}`.

## Env

Vedi `service.env.example`. Obbligatorie: `API_GATEWAY_S2S_TOKEN` e ogni
`${VAR}` usata nei file di `config/`.

## Test

```bash
uv run python -m pytest tests/
```
