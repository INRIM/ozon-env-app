# Import Record Ownership

Available versions:
- Italian: `docs/IMPORT_RECORD_OWNERSHIP.it.md`

Related documentation:
- ACL, model groups and field rules (Italian): `docs/ACL_MODEL_GROUPS_FIELDS_RULES.it.md`
- API technical docs (Italian): `docs/ENDPOINTS_REMOTE_SELECT_TECHNICAL.it.md`

Notes:
- `take_ownership` is a query-string parameter of `POST /import/{model}` only. Every other write endpoint keeps the historic behaviour (the writer owns the record).
- Preserving a foreign `owner_uid` requires an admin session: `owner_uid` feeds `record_rules` and the `$owner` write unlock in `f_rule.write`.
- A record is created under the importer's uid in exactly two cases: `take_ownership=true`, or a payload with no `owner_uid`. Anything else that would silently reassign ownership is a `403` — including a `field_acl_policy` that denies `owner_uid` on `create` (`reason: "owner_uid_denied_by_field_acl"`).
- On the preserve path, only `owner_uid` comes from the payload. `owner_name`, `owner_mail`, `owner_sector`, `owner_sector_id`, `owner_function`, `owner_personal_type` and `owner_job_title` are resolved from the local `user` collection by uid; an uid with no local `user` row is fail-soft (owner_uid kept, the rest left empty).
- `POST /import/{model}` strips `id`/`_id` from the body: they identify the source instance, and keeping them lets ozon-env's `by_id` fallback perform an UPDATE while the app authorized an INSERT. Matching stays on `rec_name`.
- Both import `403`s carry a stable `reason` key in `detail` (`foreign_owner_requires_admin`, `owner_uid_denied_by_field_acl`); match on that, not on the message string.
