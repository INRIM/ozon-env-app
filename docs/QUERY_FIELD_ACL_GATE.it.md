# Query Field ACL Gate (IT) - Guida Client

## Ambito
Validazione server-side su `query` (filtro find-style Mongo) e `order`
(ordinamento) applicata a ogni lettura in modalita' lista. Se costruisci UI
di filtro/ordinamento lato frontend, leggi questa guida prima di collegare
input di filtro liberi a uno di questi endpoint:

- `POST /list/{model}`
- `GET /action/{name}` e `POST /action/{name}` quando l'action ha
  `mode = list`
- `POST /filter/fast_search/{action_name}`

Tutti e tre finiscono nello stesso metodo backend
(`Service.list_records` / `Service.stream_record`), quindi la regola sotto
e' identica per tutti.

## Perche' esiste
L'ACL a livello di campo (`FieldAclPolicy` / `model_fields_rule`) maschera i
campi denied/obfuscate **solo nel payload di risposta**. Senza questo gate,
un client potrebbe comunque mettere quello stesso campo nella `query` o
nell'`order` della richiesta e dedurne il valore reale da quali righe
tornano, dal loro conteggio, o dal loro rank (`order=salary:desc&limit=1`
rivela chi guadagna di piu' senza che il campo `salary` compaia mai in una
risposta). Il gate chiude questo canale validando `query`/`order` PRIMA di
qualunque lettura sul database.

## Cosa viene validato

### 1. Allowlist operatori
`query` puo' usare solo questi operatori:

```
$eq $ne $in $nin $gt $gte $lt $lte
$and $or $nor $not
$exists $all $size $elemMatch $regex $options
```

Qualunque altro operatore viene rifiutato — in particolare `$where`,
`$expr`, `$function`, `$accumulator`, `$text`, `$mod`, `$type`,
`$jsonSchema`, operatori geo (`$near`, `$geoWithin`, `$geoIntersects`). Non
esiste un modo supportato per usarli dal client: ricostruisci la condizione
con gli operatori consentiti, oppure spostala in una config server-side
(`list_query` su action/component).

Risposta in caso di violazione:
```json
{
  "message": "Query operator not allowed",
  "operator": "$where"
}
```
Status HTTP: `403`.

### 2. Cross-check con l'ACL di campo
Anche usando solo operatori consentiti, se `query` o `order` referenziano un
field path `deny`ato o `obfuscate`ato per la sessione corrente su quel
model (secondo `FieldAclPolicy` / `model_fields_rule`), la richiesta viene
rifiutata — quel campo non e' utilizzabile per filtrare o ordinare, non solo
nascosto in risposta.

Risposta in caso di violazione:
```json
{
  "message": "Query references ACL-denied fields",
  "fields": ["salary"]
}
```
oppure, per `order`:
```json
{
  "message": "Order references ACL-denied fields",
  "fields": ["salary"]
}
```
Status HTTP: `403` in entrambi i casi.

## Indicazioni pratiche per il client

- **Non offrire controlli di filtro/ordinamento su campi che l'utente non
  puo' leggere.** Se un campo e' mascherato nelle risposte di lista/form
  per quell'utente, verra' rifiutato anche qui — costruire un chip di
  filtro o un header di colonna ordinabile per quel campo e' un vicolo
  cieco lato UX. Pilota la selezione dei campi filtrabili/ordinabili con
  gli stessi metadati ACL gia' usati per nascondere il campo in
  tabella/form (o con la lista `obfucated_fields` gia' presente in
  `ResponseObjectData`).
- **Tratta il `403` di questi endpoint come un caso distinto da un errore
  di auth generico.** Mostralo come "questo campo non e' utilizzabile per
  filtrare/ordinare", non come schermata di errore bloccante — e' un esito
  atteso se la query e' costruita dinamicamente (es. un query-builder
  generico) invece che da un elenco di campi fisso e verificato.
- **I path annidati funzionano come normali dotted path Mongo**
  (`address.city`), e `$and`/`$or`/`$nor` si compongono come al solito.
  `$elemMatch` e' consentito per il match su array di subdocumenti.
- **Nessun workaround client-side per `$where`/`$expr`.** Se serve una
  condizione calcolata, fai il calcolo lato client prima di inviare la
  query (confronto con un letterale), oppure chiedi al backend di
  aggiungerla come `list_query` fisso su action/component — non provare a
  codificarla come espressione raw, verra' sempre rifiutata.

## Esempio

Consentita:
```json
{
  "query": {"status": {"$in": ["open", "pending"]}, "name": {"$regex": "^A"}},
  "order": "created_at:desc"
}
```

Rifiutata (operatore non consentito):
```json
{"query": {"$where": "this.status == 'open'"}}
```

Rifiutata (campo ACL-denied referenziato), assumendo `salary` mascherato
per questa sessione:
```json
{"query": {"salary": {"$gt": 50000}}}
```
```json
{"order": "salary:desc"}
```

## Dove e' applicato (riferimento backend)
`app/ozon_env_acl/__init__.py`: `assert_query_field_acl` /
`assert_order_field_acl` (+ `extract_query_field_paths` /
`extract_order_field_paths`). Agganciato in `Service.list_records` e
`Service.stream_record` in `app/services/service.py`, prima che qualunque
lettura raggiunga il DB.
