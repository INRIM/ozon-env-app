# Keycloak e Bootstrap Sessione

## Obiettivo

Questa app deve poter generare una sessione applicativa di `ozon-env`
partendo da un'identita autenticata upstream via OpenID Connect con
Keycloak.

Il model di sessione resta **quello canonico di `ozon-env`**:

- `ozonenv.core.BaseModels.Session`

Il backend non definisce un model sessione alternativo e non usa
monkey patch.

## Modalita supportate

### 1. `AUTH_MODE=token`

Comportamento storico:

- header richiesto: `Authorization: Bearer <token>`
- il token viene usato direttamente come chiave di sessione
- `ozon-env` carica la `Session` dalla collection `session`

### 2. `AUTH_MODE=keycloak`

Comportamento nuovo per deploy dietro reverse proxy trusted:

- autenticazione utente fatta da Keycloak fuori dal backend
- header trusted: `x-remote-user` oppure il valore di
  `KEYCLOAK_REMOTE_USER_HEADER`
- il backend **non** si fida di header mandati dal client diretto:
  si fida solo dell'header iniettato dentro il boundary del proxy
- il backend legge l'utente dalla collection `user`
- il backend crea o aggiorna una `Session` `ozon-env`
  per coppia `uid + app_code`

## Flusso Keycloak

1. Il browser fa login su Keycloak.
2. Il reverse proxy valida la sessione OIDC.
3. Il reverse proxy inoltra la richiesta al backend con
   `x-remote-user: <uid>`.
4. Il backend cerca `uid` nella collection `user`.
5. Il backend crea o aggiorna la record `session`.
6. I servizi applicativi lavorano sempre contro `env.user_session`,
   cioe contro una `Session` standard di `ozon-env`.

## Separazione responsabilita con ozon-formio

La divisione consigliata e questa:

- Keycloak gestisce autenticazione e SSO
- il reverse proxy propaga l'identita trusted al backend
- il backend genera la sessione applicativa `ozon-env`
- ozon-formio non deve costruire o falsificare `x-remote-user`

In altre parole:

- identita = Keycloak/proxy
- sessione applicativa = backend `ozon-env-app`

Questa separazione evita di accoppiare il frontend alla struttura interna
della collection `session`.

## `user` vs `people`

### Opzione A. Leggere `people` a runtime

Pro:

- dato sempre piu fresco
- nessuna replica locale da governare

Contro:

- aumenta latenza sulla creazione sessione
- introduce dipendenza sincrona da un sistema esterno nel path di login
- rende la verticalizzazione piu invasiva nel backend
- complica resilienza, retry e fallback

### Opzione B. Fare sync verso collection `user`

Pro:

- il runtime del backend resta semplice e stabile
- la sessione nasce su dati locali
- il contratto applicativo rimane uniforme
- le verticalizzazioni si concentrano nel job di sync, non nel path request

Contro:

- c'e una copia locale da mantenere coerente
- serve una policy chiara per refresh, deprovisioning e utenti nuovi

## Best practice consigliata

Per questa codebase conviene usare:

- `people` come sorgente autoritativa
- `user` come cache/read-model locale usato dal backend a runtime
- job di sync periodico verso `user`

Pattern consigliato:

- sync schedulato per gli aggiornamenti ordinari
- refresh puntuale on-demand solo quando un utente autenticato non esiste
  ancora in `user` o risulta palesemente incompleto

Questo approccio tiene pulito il path di autenticazione e limita le
verticalizzazioni al resolver/sync dell'anagrafica.

## Punto di estensione attuale

Il punto naturale da verticalizzare e:

- `app/services/session_auth.py`

In particolare:

- `_load_user_record(...)` per cambiare la strategia di risoluzione utente
- `build_keycloak_session(...)` per arricchire in modo controllato il
  bootstrap della `Session`
