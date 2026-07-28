import asyncio
from types import SimpleNamespace

from app.ozon_env_acl.model_rules_sync import sync_all_model_rules
from app.ozon_env_acl.model_rules_sync import sync_model_rules


class _AsyncCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._rows:
            raise StopAsyncIteration
        return self._rows.pop(0)


class _Collection:
    def __init__(self, rows=None, existing=0):
        self.rows = list(rows or [])
        self.deleted = []
        self.inserted = []
        # Righe gia' presenti per lo scope (app_code, model). Serve al
        # watchdog di _replace_rules, che rifiuta di cancellare regole
        # esistenti quando il calcolo ne produce zero.
        self.existing = existing

    def find(self, query):
        return _AsyncCursor(self.rows)

    async def count_documents(self, query):
        return self.existing

    async def delete_many(self, query):
        self.deleted.append(query)

    async def insert_many(self, rows):
        self.inserted.extend(rows)


class _Engine:
    def __init__(self, collections):
        self.collections = collections

    def get_collection(self, name):
        return self.collections[name]


class _ValidatedRecord:
    """Passthrough: simula CoreModel.get_dict() senza validazione tipi
    (le row costruite da model_groups_rows/model_fields_rows sono gia'
    shaped correttamente dal chiamante, qui serve solo il round-trip
    async model.new(...).get_dict(...) usato da _validated_row)."""

    def __init__(self, data):
        self._data = dict(data)

    def get_dict(self, exclude=None):
        exclude = exclude or set()
        return {k: v for k, v in self._data.items() if k not in exclude}


class _PassthroughModel:
    async def new(self, data):
        return _ValidatedRecord(data)


class _Env:
    def __init__(self, engine):
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(app_code="demo"),
            db=SimpleNamespace(engine=engine),
        )
        self._passthrough_model = _PassthroughModel()

    def get(self, name):
        return self._passthrough_model


def test_sync_model_rules_writes_flat_rows_using_orm_db_engine():
    groups = _Collection()
    fields = _Collection()
    env = _Env(_Engine({"model_groups_rule": groups, "model_fields_rule": fields}))
    schema = {
        "rec_name": "document",
        "properties": {
            "models_groups": {
                "rules": [
                    {
                        "groups": ["manager"],
                        "actions": {"read": True, "update": True},
                    }
                ]
            },
            "models_restricted_fields": {
                "fields_rule": {
                    "resticted_fields": ["salary"],
                    "allowed_groups": [
                        {
                            "groups": ["dpo"],
                            "actions": {"read": True},
                        }
                    ],
                },
                "record_rulse": [
                    {
                        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
                        "actions": {"read": True, "update": True},
                    }
                ],
            },
        },
    }

    asyncio.run(sync_model_rules(env, schema))

    assert groups.deleted == [{"app_code": "demo", "model": "document"}]
    assert fields.deleted == [{"app_code": "demo", "model": "document"}]
    assert groups.inserted[0]["rec_name"] == "mgr.demo.document.manager"
    assert groups.inserted[0]["read"] is True
    assert groups.inserted[0]["update"] is True
    # "fields_rule" e' config vecchia (rule_type="fields", ritirato in
    # favore di Layer 3/ozon-env): presente nello schema di input ma nessun
    # ramo la legge piu', quindi produce zero righe fields_rule-derived.
    assert {row["rule_type"] for row in fields.inserted} == {"record"}


def test_record_rulse_with_groups_writes_one_row_per_group():
    groups = _Collection()
    fields = _Collection()
    env = _Env(_Engine({"model_groups_rule": groups, "model_fields_rule": fields}))
    schema = {
        "rec_name": "document",
        "properties": {
            "models_restricted_fields": {
                "record_rulse": [
                    {
                        "groups": ["gdpr"],
                        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
                        "actions": {"read": True, "create": True, "update": True},
                    },
                    {
                        "groups": ["dpo"],
                        "filters": {},
                        "actions": {"read": True},
                    },
                    {
                        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
                        "actions": {"read": True},
                    },
                ],
            },
        },
    }

    asyncio.run(sync_model_rules(env, schema))

    record_rows = [row for row in fields.inserted if row["rule_type"] == "record"]
    by_group = {row["group"]: row for row in record_rows}

    assert set(by_group) == {"gdpr", "dpo", ""}
    assert by_group["gdpr"]["read"] is True
    assert by_group["gdpr"]["create"] is True
    assert by_group["dpo"]["read"] is True
    assert by_group["dpo"]["create"] is False
    assert by_group[""]["read"] is True


def test_sync_all_model_rules_normalizes_components_and_rewrites_tables():
    component = _Collection(
        rows=[
            {
                "rec_name": "document",
                "sys": False,
                "deleted": 0,
                "properties": {},
            }
        ]
    )
    groups = _Collection()
    fields = _Collection()
    env = _Env(
        _Engine(
            {
                "component": component,
                "model_groups_rule": groups,
                "model_fields_rule": fields,
            }
        )
    )

    asyncio.run(sync_all_model_rules(env))

    group_names = {row["group"] for row in groups.inserted}
    field_types = {row["rule_type"] for row in fields.inserted}
    assert {"admin", "user", "technical_operator", "operator", "manager", "dpo"} <= group_names
    # default seed produce solo record_rulse (owner-only universale) ora
    # che "fields_rule" e' ritirato dal default.
    assert field_types == {"record"}


# --- Default ACL: copertura dei gruppi ------------------------------------


# Gruppi previsti dalla collection `groups` (seed applicativo). Se ne
# viene aggiunto uno, va deciso esplicitamente che permessi ha nei
# default: con model_group_access fail-closed, un gruppo non citato qui
# non ha accesso a NESSUN model che usa i default — e il sintomo (utente
# che "non vede niente") non punta a questo file.
_EXPECTED_GROUPS = {
    "admin",
    "user",
    "operator",
    "manager",
    "dpo",
    "gdpr",
    "technical_operator",
}


def _default_groups(defaults):
    return {g for rule in defaults["rules"] for g in rule["groups"]}


def test_non_sys_defaults_cover_every_known_group():
    from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_NON_SYS

    missing = _EXPECTED_GROUPS - _default_groups(_DEFAULT_MODELS_GROUPS_NON_SYS)

    assert not missing, (
        f"gruppi senza permessi nei default non_sys: {sorted(missing)} — "
        "fail-closed, resterebbero senza accesso"
    )


def test_sys_defaults_stay_restricted():
    """I model `sys` sono configurazione condivisa: solo admin e
    technical_operator. Questo test e' un fermo: allargarli e' una
    decisione, non una svista."""
    from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_SYS

    assert _default_groups(_DEFAULT_MODELS_GROUPS_SYS) == {
        "admin",
        "technical_operator",
    }


def test_gdpr_has_same_profile_as_other_functional_roles():
    from app.core.OzonEnvApp import _DEFAULT_MODELS_GROUPS_NON_SYS

    actions = {
        group: rule["actions"]
        for rule in _DEFAULT_MODELS_GROUPS_NON_SYS["rules"]
        for group in rule["groups"]
    }

    assert actions["gdpr"] == actions["dpo"] == actions["manager"]
    assert actions["gdpr"]["delete"] is False


# --- Watchdog: nessuna cancellazione senza sostituzione -------------------


def _sync(schema, groups, fields):
    env = _Env(_Engine({
        "model_groups_rule": groups,
        "model_fields_rule": fields,
    }))
    asyncio.run(sync_model_rules(env, schema))


def test_malformed_models_groups_leaves_existing_rules_untouched():
    """`models_groups` come ARRAY (la forma che mostra il json editor del
    form) invece che come oggetto `{"rules": [...]}`.

    Prima: _parse_dict_property degradava a None, si generavano 0 righe e
    il delete_many incondizionato azzerava l'ACL del model — con
    model_group_access fail-closed, lockout di tutti i non-admin.
    """
    groups = _Collection(existing=6)
    fields = _Collection(existing=1)
    schema = {
        "rec_name": "document",
        "properties": {"models_groups": []},
    }

    _sync(schema, groups, fields)

    assert groups.deleted == [], (
        "ha cancellato regole valide su input malformato"
    )
    assert groups.inserted == []


def test_malformed_models_groups_string_is_not_silently_ignored():
    groups = _Collection(existing=6)
    fields = _Collection(existing=0)
    schema = {
        "rec_name": "document",
        "properties": {"models_groups": "non-json"},
    }

    _sync(schema, groups, fields)

    assert groups.deleted == []


def test_empty_result_does_not_wipe_existing_group_rules():
    """Property formalmente valida ma senza regole utili: se righe
    esistono, non si cancella (sarebbe un lockout silenzioso)."""
    groups = _Collection(existing=6)
    fields = _Collection(existing=0)
    schema = {
        "rec_name": "document",
        "properties": {"models_groups": {"rules": []}},
    }

    _sync(schema, groups, fields)

    assert groups.deleted == []
    assert groups.inserted == []


def test_empty_result_does_not_wipe_existing_record_rules():
    """Stessa guardia sulla tabella record: azzerarla non blocca nessuno,
    ma TOGLIE il filtro per riga e allarga l'accesso."""
    groups = _Collection(existing=0)
    fields = _Collection(existing=3)
    schema = {
        "rec_name": "document",
        "properties": {"models_restricted_fields": {"record_rulse": []}},
    }

    _sync(schema, groups, fields)

    assert fields.deleted == []
    assert fields.inserted == []


def test_empty_result_is_allowed_when_nothing_exists_yet():
    """Nessuna riga da proteggere: il sync normale non deve essere
    ostacolato dal watchdog (primo avvio, model nuovo)."""
    groups = _Collection(existing=0)
    fields = _Collection(existing=0)
    schema = {
        "rec_name": "document",
        "properties": {"models_groups": {"rules": []}},
    }

    _sync(schema, groups, fields)

    assert groups.deleted == [{"app_code": "demo", "model": "document"}]


def test_valid_rules_still_replace_existing_rows():
    """Il watchdog non deve impedire un aggiornamento legittimo."""
    groups = _Collection(existing=6)
    fields = _Collection(existing=0)
    schema = {
        "rec_name": "document",
        "properties": {
            "models_groups": {
                "rules": [
                    {"groups": ["manager"], "actions": {"read": True}}
                ]
            }
        },
    }

    _sync(schema, groups, fields)

    assert groups.deleted == [{"app_code": "demo", "model": "document"}]
    assert [r["group"] for r in groups.inserted] == ["manager"]
