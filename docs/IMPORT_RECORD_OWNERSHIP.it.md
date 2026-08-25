# Import record e ownership — direttive frontend

Riferimento: `POST /import/{model}` (`app/api/routes.py`), `Service.upsert`
(`app/services/service.py`), `OzonModelApp.set_user_data`
(`app/core/OzonModelApp.py`).

## Cosa cambia

`POST /import/{model}` accetta un nuovo parametro **in query string**:

```
POST /import/{model}?take_ownership=false        # default dell'endpoint
POST /import/{model}?take_ownership=true
```

| valore                   | effetto su un record NUOVO                                                                                   |
|--------------------------|--------------------------------------------------------------------------------------------------------------|
| `false` (default import) | se il payload contiene `owner_uid`, quel valore viene scritto: il record resta intestato all'owner originale |
| `true`                   | gli `owner_*` vengono sovrascritti con l'utente che sta importando                                           |

Nessun altro endpoint cambia comportamento. `POST /{model}/{rec_name}` e tutte le altre
scritture restano come prima: chi scrive è l'owner.

## Regole che il frontend deve rispettare

### 1. Il parametro va in query string, mai nel body

Il body dell'import è il record da importare e non ha allowlist: un campo
`take_ownership` dentro il payload verrebbe interpretato come un dato del record, non
come un'opzione.

```js
// SÌ
await fetch(`/import/${model}?take_ownership=true`, {
    method: "POST",
    headers: {"Content-Type": "application/json", ...authHeaders},
    body: JSON.stringify(record),
});

// NO — finisce nel record
body: JSON.stringify({...record, take_ownership: true})
```

### 2. Non spedire `owner_uid` sulle scritture normali

Sul salvataggio ordinario (`POST /{model}/{rec_name}`) il campo `owner_uid`
non serve: viene ignorato su update e, su insert, sovrascritto con la sessione. Se un
form lo ha in pancia perché arriva da una `GET` precedente, va rimosso prima del `POST`.
Vale in particolare per i flussi "duplica" /
"salva come nuovo", che spesso ripartono dal record letto.

### 3. Gestire il 403 sull'import

Con `take_ownership=false`, se il payload porta un `owner_uid` **diverso**
dall'utente corrente e l'utente **non è admin**, la risposta è `403`:

```json
{
  "detail": {
    "message": "Importing a record owned by another user requires admin; pass take_ownership=true to import it under your own uid",
    "reason": "foreign_owner_requires_admin",
    "model": "component"
  }
}
```

Non è un errore da mostrare grezzo. L'azione giusta lato UI è riproporre l'import con
`take_ownership=true`, spiegando che i record verranno intestati all'utente corrente. Un
admin non vede mai questo errore.

Il `detail` porta `"reason": "foreign_owner_requires_admin"` — usa quello per
discriminare, non il testo del messaggio: l'import può rispondere `403` anche per
un'altra ragione (vedi §4).

Questo `403` **non** scatta se:

- il payload non ha `owner_uid` (creazione normale)
- l'`owner_uid` del payload è già quello dell'utente corrente
- si passa `take_ownership=true`

### 4. Il 403 da field ACL su `owner_uid`

Se una `field_acl_policy` nega `owner_uid` in `create`, il campo viene scartato dal
payload prima del gate: l'owner del payload non è più preservabile. In quel caso l'import
con `take_ownership=false` **fallisce**
con `403`:

```json
{
  "detail": {
    "message": "A field ACL policy denies writing owner_uid on create: the record owner cannot be preserved. Pass take_ownership=true to import it under your own uid, or remove the policy on owner_uid",
    "reason": "owner_uid_denied_by_field_acl",
    "model": "component"
  }
}
```

L'owner diventa chi importa **solo** in due casi: `take_ownership=true`
(richiesta esplicita) o payload senza `owner_uid`. Nessun fallback silenzioso. Anche qui
la remediation lato UI è riproporre l'import con
`take_ownership=true`; se invece la policy è un errore di configurazione, va tolta lato
admin.

### 5. Gli altri `owner_*` non li decide il payload

Con `take_ownership=false` sopravvive l'`owner_uid` del payload, ma
`owner_name`, `owner_mail`, `owner_sector`, `owner_sector_id`,
`owner_function`, `owner_personal_type` e `owner_job_title` vengono riletti dalla
collection `user` di **questa** istanza, per `uid`. Quelli del payload sono ignorati:
sono i dati dell'istanza di origine, o valori scelti da chi importa (e `owner_name`/
`owner_sector` finiscono in liste e report).

Se l'`uid` non esiste nella collection `user` locale — record esportato da un'altra
istanza, utente non ancora sincronizzato — l'import **non**
fallisce: il record entra con l'`owner_uid` originale e gli altri
`owner_*` vuoti. Un `owner_name` che non corrisponde a nessun utente locale sarebbe
peggio di un campo vuoto.

Per il frontend non cambia nulla: non serve spedire gli `owner_*` nel payload di import,
verrebbero comunque scartati.

### 6. `id`/`_id` nel payload vengono scartati

L'import toglie `id` e `_id` dal body prima di passarlo a `Service.upsert`:
sono l'identità dell'istanza di **origine** e qui non significano niente
(l'insert ne genera uno nuovo comunque). Se restassero, `upsert` di
ozon-env potrebbe non trovare il `rec_name` e ripiegare su
`by_id(payload["id"])`: l'app autorizzerebbe un INSERT (gate ACL `create`,
preservazione owner armata) mentre l'ORM eseguirebbe un UPDATE su un altro
record — e su update la preservazione dell'owner non fa nulla.

Il frontend non deve ripulire niente: può spedire il record esportato tale
e quale. Il match dell'import resta su `rec_name`, quindi reimportare lo
stesso file aggiorna lo stesso record.

### 7. L'ownership di un record esistente non è mai riassegnabile

Se il `rec_name` esiste già, l'import è un update e gli `owner_*` del payload vengono
scartati — sempre, per chiunque, con qualunque valore del flag. Un'interfaccia che
promette "reimposta owner" su record esistenti prometterebbe una cosa che il backend non
fa.

## UI consigliata

Nel dialog di import, una singola scelta:

- **Mantieni gli autori originali** → `take_ownership=false` — solo per admin; per i
  non-admin va disabilitata o nascosta (`session.is_admin`
  è già disponibile da `GET /get_session`)
- **Importa a mio nome** → `take_ownership=true` — sempre disponibile

Default suggerito: "mantieni" per gli admin, "a mio nome" per tutti gli altri. Così il
`403` del punto 3 diventa un caso di bordo (payload manipolato o sessione scaduta), non
il flusso normale.

## Perché il gate esiste

`owner_uid` non è un campo descrittivo: è un input dell'autorizzazione.

- le `record_rules` filtrano i record su `owner_uid == user.uid`
  (`app/core/OzonEnvApp.py:44`)
- `f_rule.write` supporta il sentinel `$owner`, che sblocca in scrittura campi altrimenti
  riservati quando chi scrive è l'owner del record (`_OWNER_WRITE_SENTINEL` in
  `app/services/service.py`)

Creare un record intestato a un altro utente significa quindi assegnargli visibilità e
permessi di scrittura. È un'operazione da admin, e il frontend non deve provare a
aggirarla: il gate è server-side.

## Nota per il backend

Se la conservazione dell'owner in import fallisce con
`reason="owner_uid_denied_by_field_acl"`, la causa è una
`field_acl_policy` su `owner_uid` con `operation="create"` ed
`effect="deny"`: il campo viene scartato dal payload prima del gate, e
`Service.upsert` solleva invece di ricadere su "owner = chi importa"
(riassegnazione silenziosa di ownership, per giunta saltando il gate admin di §3 che
legge il payload dopo l'ACL). Lo snapshot `incoming_owner_uid` in
`Service.upsert` è preso subito dopo il webhook `data.before_write` e prima di
`enforce_write_acl` proprio per poterlo rilevare.
