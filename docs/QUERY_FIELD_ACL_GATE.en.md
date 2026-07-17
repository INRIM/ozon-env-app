# Query Field ACL Gate (EN) - Client Guide

## Scope
This guide explains a server-side validation on `query` (Mongo find-style
filter) and `order` (sort) that runs on every list-mode read. If you build
filter/sort UI on the frontend, read this before wiring free-form filter
inputs to any of these endpoints:

- `POST /list/{model}`
- `GET /action/{name}` and `POST /action/{name}` when the action's
  `mode = list`
- `POST /filter/fast_search/{action_name}`

All three end up calling the same backend method
(`Service.list_records` / `Service.stream_record`), so the rule below is
identical across them.

## Why this exists
Field-level ACL (`FieldAclPolicy` / `model_fields_rule`) masks denied or
obfuscated fields **in the response payload only**. Without this gate, a
client could still put that same field in the request `query` or `order`
and infer its real value from which rows come back, their count, or their
rank (`order=salary:desc&limit=1` reveals the top earner without the
`salary` field ever appearing in a response). The gate closes that channel
by validating `query`/`order` **before** any database read.

## What gets validated

### 1. Operator allowlist
`query` must be built only from these operators:

```
$eq $ne $in $nin $gt $gte $lt $lte
$and $or $nor $not
$exists $all $size $elemMatch $regex $options
```

Any other operator is rejected outright — most notably `$where`, `$expr`,
`$function`, `$accumulator`, `$text`, `$mod`, `$type`, `$jsonSchema`, and
geo operators (`$near`, `$geoWithin`, `$geoIntersects`). There is no
supported way to use these from the client; build the condition with the
allowed operators, or push it into a server-side action/component config
(`list_query`) instead.

Response on violation:
```json
{
  "message": "Query operator not allowed",
  "operator": "$where"
}
```
HTTP status: `403`.

### 2. Field ACL cross-check
Even with only allowed operators, if `query` or `order` references a field
path that is `deny`d or `obfuscate`d for the current session on that model
(per `FieldAclPolicy` / `model_fields_rule`), the request is rejected —
same as above, the field is unusable for filtering or sorting, not just
hidden in the response.

Response on violation:
```json
{
  "message": "Query references ACL-denied fields",
  "fields": ["salary"]
}
```
or, for `order`:
```json
{
  "message": "Order references ACL-denied fields",
  "fields": ["salary"]
}
```
HTTP status: `403` in both cases.

## Practical guidance for client code

- **Don't offer filter/sort controls on fields the user can't read.** If a
  field is masked in list/form responses for this user, it will also be
  rejected here — building a filter chip or a sortable column header for it
  is dead-end UX. Drive the filter/sort field picker off the same ACL
  metadata you already use to hide the field in the table/form (or off the
  `obfucated_fields` list already present in `ResponseObjectData`).
- **Treat `403` from these endpoints as a distinct case from generic auth
  failure.** Surface it as "this field can't be used to filter/sort", not
  a hard error screen — it's an expected outcome if the query was built
  dynamically (e.g. a generic query-builder component) rather than from a
  fixed, reviewed field list.
- **Nested paths work as normal dotted Mongo paths** (`address.city`), and
  `$and`/`$or`/`$nor` compose as usual. `$elemMatch` is allowed for
  array-of-subdocument matching.
- **No client-side workaround for `$where`/`$expr`.** If you need a
  computed condition, do the computation client-side before sending the
  query (compare against a literal), or ask backend to add it as a fixed
  `list_query` on the action/component — don't try to encode it as a raw
  expression, it will always be rejected.

## Example

Allowed:
```json
{
  "query": {"status": {"$in": ["open", "pending"]}, "name": {"$regex": "^A"}},
  "order": "created_at:desc"
}
```

Rejected (disallowed operator):
```json
{"query": {"$where": "this.status == 'open'"}}
```

Rejected (ACL-denied field referenced), assuming `salary` is masked for
this session:
```json
{"query": {"salary": {"$gt": 50000}}}
```
```json
{"order": "salary:desc"}
```

## Where this is enforced (backend reference)
`app/ozon_env_acl/__init__.py`: `assert_query_field_acl` /
`assert_order_field_acl` (+ `extract_query_field_paths` /
`extract_order_field_paths`). Wired into `Service.list_records` and
`Service.stream_record` in `app/services/service.py`, before any read
reaches the DB.
