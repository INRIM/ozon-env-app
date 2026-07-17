# Query Field ACL Gate

- Italian: `docs/QUERY_FIELD_ACL_GATE.it.md`
- English: `docs/QUERY_FIELD_ACL_GATE.en.md`

Summary: client-supplied `query` (Mongo find-style filter) and `order` (sort)
are now validated server-side before hitting the DB — allowlisted operators
only, and no reference to a field the caller's ACL denies/obfuscates on read.
Applies to every endpoint that ends up in `Service.list_records` /
`Service.stream_record`: `POST /list/{model}`, `GET/POST /action/{name}`
(`mode=list`), `POST /filter/fast_search/{action_name}`.

Violation -> `403`, not a silent empty/wrong result.
