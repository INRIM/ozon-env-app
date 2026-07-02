# Identity Manager Service

Worker che si occupa di sincronizzare periodicamente la lista `users` per ciascun record `group_users` che presenta una query `rule` compilata.

## Configurazione

Il worker legge le variabili del database Mongo (`MONGO_*`) dal file `.env` principale dello stack e supporta:

* `IDENTITY_MANAGER_INTERVAL_MINUTES`: Intervallo in minuti tra le sincronizzazioni (default `10`). Se impostato su un valore `<= 0`, la sincronizzazione periodica viene disattivata.
