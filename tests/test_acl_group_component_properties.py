import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from app.ozon_env_acl import apply_session_groups
from app.ozon_env_acl import synth_policies_from_component_properties
from app.services.service import Service


class _Status:
    fail = False
    msg = ""


class _Schema:
    @staticmethod
    def schema():
        return {"components": []}

    @staticmethod
    def filter_keys():
        return {}


class _RecordModel:
    def __init__(self, name, rows=None):
        self.data_model = name
        self.status = _Status()
        self.model = _Schema()
        self.table_columns = {}
        self.rows = list(rows or [])

    def get_domain(self, query):
        return query

    async def count(self, domain):
        return len(self.rows)

    async def find(
        self,
        domain,
        sort="",
        skip=0,
        limit=0,
        pipeline_items=None,
        obfuscate_fields=None,
        fields=None,
    ):
        return [row.copy() for row in self.rows]

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row.copy()
        return {}

    async def stream_find(
        self,
        domain,
        sort="",
        skip=0,
        limit=0,
        pipeline_items=None,
        obfuscate_fields=None,
        fields=None,
        batch_size=0,
    ):
        for row in self.rows:
            yield row.copy()


class _ComponentModel:
    def __init__(self, components):
        self.components = components

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [c.copy() for c in self.components]

    async def by_name(self, name):
        for component in self.components:
            if component.get("rec_name") == name:
                return component.copy()
        return None


class _ModelFieldsRuleModel:
    """Fake per la collection `model_fields_rule` — fonte di verita' letta
    da Service._get_record_rulse/_load_model_fields_rule_policies (NON
    component.properties, che puo' non persistere la config)."""

    def __init__(self, rows):
        self.rows = rows

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        clauses = domain.get("$and", [domain]) if isinstance(domain, dict) else []

        def matches(row):
            for clause in clauses:
                for key, expected in clause.items():
                    if row.get(key) != expected:
                        return False
            return True

        return [row.copy() for row in self.rows if matches(row)]


class _GroupUsersModel:
    def __init__(self, rows):
        self.rows = rows

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        return [row.copy() for row in self.rows]


class _UserRuleModel:
    def __init__(self, rows):
        self.rows = rows
        self.domains = []

    def get_domain(self, query):
        return query

    async def find(self, domain, limit=0):
        self.domains.append(domain)
        serialized = json.dumps(domain, sort_keys=True)
        if '"department": "it"' not in serialized:
            return []
        return [row.copy() for row in self.rows[:limit or None]]


class _Env:
    def __init__(self, models, session):
        self.user_session = session
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                module_name="demo", version="1.0.0", logo_img_url=""
            )
        )
        self._models = models
        self.db = SimpleNamespace(engine=None)

    def get(self, model_name):
        return self._models[model_name]


def _session(groups=None, is_admin=False, uid="u1"):
    return SimpleNamespace(
        app_code="demo",
        uid=uid,
        is_admin=is_admin,
        user={"uid": uid, "groups": groups or []},
    )


def test_synth_policies_from_models_groups_deny_non_admin_wildcard():
    components = [
        {
            "rec_name": "customer",
            "properties": {"models_groups": ["hr", "dpo"]},
        }
    ]
    policies = synth_policies_from_component_properties(components)

    assert len(policies) == 3  # read/insert/update, whole-model field_path "*"
    for policy in policies:
        assert policy["model_key"] == "customer"
        assert policy["field_path"] == "*"
        assert policy["effect"] == "deny"
        assert policy["actor_selector"]["exclude_groups"] == ["dpo", "hr"]
        assert policy["actor_selector"]["is_admin"] is False


def test_synth_policies_from_models_restricted_fields_per_field():
    components = [
        {
            "rec_name": "customer",
            "properties": {
                "models_restricted_fields": {"salary": ["hr", "manager"]}
            },
        }
    ]
    policies = synth_policies_from_component_properties(components)

    assert len(policies) == 3
    for policy in policies:
        assert policy["field_path"] == "salary"
        assert policy["actor_selector"]["exclude_groups"] == [
            "hr",
            "manager",
        ]


def test_synth_policies_ignores_new_models_groups_rules_shape():
    """models_groups in formato {"rules": [...]} e' un dict, non list/CSV:
    _as_set lo stringificherebbe in un gruppo-fantasma e negherebbe tutto a
    tutti i non-admin. Va ignorato qui — il consumo di questo formato per
    models_groups passa da model_groups_rule (model_rules_sync), non da
    synth_policies_from_component_properties. models_restricted_fields nel
    formato nuovo con fields_rule/record_rulse vuoti non produce invece
    policy (nessun gruppo/campo da restringere)."""
    components = [
        {
            "rec_name": "customer",
            "properties": {
                "models_groups": {
                    "rules": [{"groups": ["admin"], "actions": {"read": True}}]
                },
                "models_restricted_fields": {
                    "fields_rule": {"resticted_fields": [], "allowed_groups": []},
                    "record_rulse": [],
                },
            },
        }
    ]

    policies = synth_policies_from_component_properties(components)

    assert policies == []


def test_synth_policies_from_component_properties_ignores_fields_rule():
    """component.properties.models_restricted_fields.fields_rule NON e'
    piu' la fonte per l'enforcement (l'utente ha confermato che la config
    puo' non persistere li' anche a regola attiva): synth_policies_from_
    component_properties resta un no-op per il formato nuovo, l'unica
    fonte e' la collection model_fields_rule (vedi
    Service._load_model_fields_rule_policies, testato sotto)."""
    components = [
        {
            "rec_name": "user",
            "properties": {
                "models_restricted_fields": {
                    "fields_rule": {
                        "resticted_fields": ["codicefiscale"],
                        "allowed_groups": [
                            {"groups": ["gdpr"], "actions": {"read": True}},
                        ],
                    },
                    "record_rulse": [],
                },
            },
        }
    ]

    assert synth_policies_from_component_properties(components) == []


def test_load_model_fields_rule_policies_emits_obfuscate_not_deny():
    """Service._load_model_fields_rule_policies legge la collection
    model_fields_rule (rule_type="fields"), scoped per app_code corrente,
    e unisce i gruppi read=true di piu' righe (gdpr, dpo) in UNA policy per
    campo — separarle in policy distinte negherebbe un attore in gdpr ma
    non in dpo (read_masks oscura se ALMENO UNA policy matcha)."""
    rows = [
        {
            "app_code": "demo",
            "model": "user",
            "rule_type": "fields",
            "group": "gdpr",
            "restricted_fields": ["codicefiscale"],
            "filters": {},
            "read": True,
            "active": True,
            "deleted": 0,
        },
        {
            "app_code": "demo",
            "model": "user",
            "rule_type": "fields",
            "group": "dpo",
            "restricted_fields": ["codicefiscale"],
            "filters": {},
            "read": False,
            "active": True,
            "deleted": 0,
        },
        {
            # app_code diverso: non deve inquinare la policy dell'app corrente.
            "app_code": "other-app",
            "model": "user",
            "rule_type": "fields",
            "group": "hr",
            "restricted_fields": ["codicefiscale"],
            "filters": {},
            "read": True,
            "active": True,
            "deleted": 0,
        },
    ]
    env = _Env(
        {"model_fields_rule": _ModelFieldsRuleModel(rows)},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    policies = asyncio.run(service._load_model_fields_rule_policies())

    assert len(policies) == 1
    policy = policies[0]
    assert policy["model_key"] == "user"
    assert policy["field_path"] == "codicefiscale"
    assert policy["operation"] == "read"
    assert policy["effect"] == "obfuscate"
    # dpo ha read=false -> non esclude dall'oscuramento, solo gdpr lo fa;
    # la riga "other-app" e' di un altro app_code, ignorata.
    assert policy["actor_selector"]["exclude_groups"] == ["gdpr"]
    # niente is_admin nel selector: fields_rule NON deve bypassare l'admin
    # (GDPR: essere admin non significa essere autorizzati al dato).
    assert "is_admin" not in policy["actor_selector"]


def test_models_groups_denies_whole_model_for_non_member_group():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data == {}
    assert response.content.obfucated_fields == []


def test_models_groups_allows_member_group():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["hr"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_groups_bypassed_for_admin():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {"models_groups": ["hr"]},
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=[], is_admin=True),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_restricted_fields_hides_field_for_non_allowed_group():
    customer = _RecordModel(
        "customer", rows=[{"rec_name": "c1", "name": "Ada", "salary": 42}]
    )
    component = _ComponentModel(
        [
            {
                "rec_name": "customer",
                "active": True,
                "deleted": 0,
                "properties": {
                    "models_restricted_fields": {"salary": ["hr"]}
                },
            }
        ]
    )
    env = _Env(
        {"customer": customer, "component": component},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"
    assert "salary" not in response.content.data


def _gdpr_rule_rows(app_code="demo", record_filters=None, model="user"):
    """Righe `model_fields_rule` — fonte di verita' per l'enforcement (NON
    component.properties, che l'utente ha confermato puo' restare vuota
    anche a regola configurata: il sync scrive qui al salva del component
    e questa collection e' cio' che Service legge davvero)."""
    if record_filters is None:
        record_filters = {"owner_uid": {"$eq": {"var": "user.uid"}}}
    return [
        {
            "app_code": app_code,
            "model": model,
            "rule_type": "fields",
            "group": "gdpr",
            "restricted_fields": ["codicefiscale"],
            "filters": {},
            "read": True,
            "active": True,
            "deleted": 0,
        },
        {
            "app_code": app_code,
            "model": model,
            "rule_type": "record",
            "group": "",
            "restricted_fields": [],
            "filters": record_filters,
            "read": True,
            "active": True,
            "deleted": 0,
        },
    ]


def test_fields_rule_obfuscates_field_without_gdpr_group():
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Ada",
                "codicefiscale": "ABC123",
            }
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["name"] == "Ada"
    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_fields_rule_obfuscates_field_for_admin_without_gdpr_group_or_ownership():
    """Regression: admin NON deve bypassare fields_rule/record_rulse — solo
    il gruppo gdpr o essere proprietario del record sbloccano il campo,
    admin incluso (segnalato dall'utente: vedeva codicefiscale in chiaro
    su tutta la lista utenti pur essendo solo admin, senza gdpr)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Ada",
                "codicefiscale": "ABC123",
            }
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=[], is_admin=True, uid="admin1"),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_fields_rule_visible_with_gdpr_group():
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Ada",
                "codicefiscale": "ABC123",
            }
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["gdpr"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] == "ABC123"
    assert response.content.obfucated_fields == []


def test_record_rulse_overrides_baseline_for_owned_record():
    """Utente senza gruppo gdpr ma proprietario del record (owner_uid ==
    user.uid): la regola record matcha e sblocca il campo altrimenti
    oscurato dalla baseline fields_rule."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Own Record",
                "codicefiscale": "OWN123",
            }
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"
    assert response.content.obfucated_fields == []


def test_record_rulse_match_reveals_field_in_its_own_scope():
    """Regressione: la riga record_rulse reale (sync da model_rules_sync,
    vedi dati storici it-settings-*) ha `restricted_fields` = lo SCOPE di
    campi che sblocca (qui coincide col solo campo baseline, quindi match
    -> tutto visibile). Non e' "ignora restricted_fields, sblocca sempre
    tutto" (fix intermedio sbagliato); e' "baseline - scope"."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Own Record",
                "codicefiscale": "OWN123",
            }
        ],
    )
    rows = _gdpr_rule_rows()
    for row in rows:
        if row["rule_type"] == "record":
            row["restricted_fields"] = ["codicefiscale"]
    env = _Env(
        {"user": users, "model_fields_rule": _ModelFieldsRuleModel(rows)},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"
    assert response.content.obfucated_fields == []


def test_record_rulse_match_only_reveals_fields_in_its_scope():
    """fields_rule oscura DUE campi (codicefiscale, token) per chi non e'
    gdpr; il record_rulse dell'utente e' scoped a SOLO codicefiscale
    (restricted_fields=["codicefiscale"]) — match deve sbloccare
    codicefiscale ma lasciare token oscurato: nessuna regola copre token,
    quindi resta sotto la baseline. Riproduce esattamente il caso
    segnalato (fields_rule=[codicefiscale, token], record rule=
    [codicefiscale] -> token deve restare sempre oscurato)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Own Record",
                "codicefiscale": "OWN123",
                "token": "secret-token-value",
            }
        ],
    )
    rows = [
        {
            "app_code": "demo",
            "model": "user",
            "rule_type": "fields",
            "group": "gdpr",
            "restricted_fields": ["codicefiscale", "token"],
            "filters": {},
            "read": True,
            "active": True,
            "deleted": 0,
        },
        {
            "app_code": "demo",
            "model": "user",
            "rule_type": "record",
            "group": "",
            "restricted_fields": ["codicefiscale"],
            "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
            "read": True,
            "active": True,
            "deleted": 0,
        },
    ]
    env = _Env(
        {"user": users, "model_fields_rule": _ModelFieldsRuleModel(rows)},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"
    assert response.content.data["token"] is None
    assert response.content.obfucated_fields == ["token"]


def test_record_rulse_keeps_baseline_when_no_match():
    """Utente senza gdpr e non proprietario: la regola record non matcha,
    la baseline fields_rule resta in vigore (campo oscurato)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Other Record",
                "codicefiscale": "OTHER123",
            }
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_record_rulse_and_wrapped_filter_matches_owner():
    """filters avvolti in $and (forma Mongo comune, non la forma flat degli
    esempi base) devono comunque matchare correttamente per il proprietario."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Own Record",
                "codicefiscale": "OWN123",
            }
        ],
    )
    rows = _gdpr_rule_rows(
        record_filters={"$and": [{"owner_uid": {"$eq": {"var": "user.uid"}}}]}
    )
    env = _Env(
        {"user": users, "model_fields_rule": _ModelFieldsRuleModel(rows)},
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"


def test_record_rulse_and_wrapped_filter_fails_closed_for_non_owner():
    """Stesso filtro $and, ma per un record non di proprieta': deve fallire
    CLOSED (campo resta oscurato), non aprirsi a chiunque per il solo fatto
    che $and non era gestito esplicitamente (regressione verificata: prima
    del fix $and/$or venivano ignorati e il match tornava sempre True)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Other Record",
                "codicefiscale": "OTHER123",
            }
        ],
    )
    rows = _gdpr_rule_rows(
        record_filters={"$and": [{"owner_uid": {"$eq": {"var": "user.uid"}}}]}
    )
    env = _Env(
        {"user": users, "model_fields_rule": _ModelFieldsRuleModel(rows)},
        session=_session(groups=["sales"], uid="u1"),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_record_rulse_varies_per_row_in_list_records():
    """list_records: stesso utente (sales, no gdpr) vede il proprio record
    in chiaro e quello altrui oscurato, nella STESSA risposta."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Mine",
                "codicefiscale": "MINE123",
            },
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Other",
                "codicefiscale": "OTHER123",
            },
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["sales"], uid="u1"),
    )
    service = Service(env)

    response = asyncio.run(
        service.list_records(
            model_name="user",
            query={},
            order="",
            skip=0,
            limit=10,
        )
    )

    by_rec_name = {row["rec_name"]: row for row in response.content.data}
    assert by_rec_name["u1"]["codicefiscale"] == "MINE123"
    assert by_rec_name["u2"]["codicefiscale"] is None


def test_record_rulse_varies_per_row_in_streamed_list():
    """Il path streaming (resp_stream=True, usato di default da
    POST /list/{model}) deve applicare record_rulse riga per riga tanto
    quanto il path non-stream — stesso scenario del test precedente ma
    passando per service.stream_record (NDJSON)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "u1",
                "owner_uid": "u1",
                "name": "Mine",
                "codicefiscale": "MINE123",
            },
            {
                "rec_name": "u2",
                "owner_uid": "u2",
                "name": "Other",
                "codicefiscale": "OTHER123",
            },
        ],
    )
    env = _Env(
        {
            "user": users,
            "model_fields_rule": _ModelFieldsRuleModel(_gdpr_rule_rows()),
        },
        session=_session(groups=["sales"], uid="u1"),
    )
    service = Service(env)

    envelope = asyncio.run(
        service.list_records(
            model_name="user",
            query={},
            order="",
            skip=0,
            limit=10,
            resp_stream=True,
        )
    )
    assert envelope.content.obfucated_fields == ["codicefiscale"]

    async def _collect():
        cursor = await service.stream_record(envelope, order="", skip=0, limit=10)
        return [row async for row in cursor]

    rows = asyncio.run(_collect())
    by_rec_name = {row["rec_name"]: row for row in rows}
    assert by_rec_name["u1"]["codicefiscale"] == "MINE123"
    assert by_rec_name["u2"]["codicefiscale"] is None


def test_apply_session_groups_matches_uid_in_group_users():
    session = _session()
    group_users = _GroupUsersModel(
        [
            {
                "group": "manager",
                "users": ["u1", "u2"],
                "app_code": "demo",
                "active": True,
                "deleted": 0,
            },
            {"group": "legacy", "users": ["u1"], "active": True, "deleted": 0},
            {
                "group": "other-app",
                "users": ["u1"],
                "app_code": "other",
                "active": True,
                "deleted": 0,
            },
            {"group": "dpo", "users": ["u9"], "active": True, "deleted": 0},
        ]
    )
    groups_model = _GroupUsersModel(
        [
            {
                "rec_name": "manager",
                "implied_groups": ["user", "operator"],
                "active": True,
                "deleted": 0,
            },
            {
                "rec_name": "operator",
                "implied_groups": [],
                "active": True,
                "deleted": 0,
            },
            {
                "rec_name": "user",
                "implied_groups": [],
                "active": True,
                "deleted": 0,
            },
        ]
    )
    env = _Env({"group_users": group_users, "groups": groups_model}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == ["manager", "operator", "user"]
    assert session.user["groups"] == ["manager", "operator", "user"]


def test_apply_session_groups_empty_when_no_match():
    session = _session()
    group_users = _GroupUsersModel(
        [{"group": "dpo", "users": ["u9"], "active": True, "deleted": 0}]
    )
    env = _Env({"group_users": group_users}, session=session)

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == []
    assert session.user["groups"] == []


def test_apply_session_groups_adds_groups_from_mongo_rule():
    session = _session()
    group_users = _GroupUsersModel([])
    groups_model = _GroupUsersModel(
        [
            {
                "rec_name": "technical_operator",
                "rule": "{\"department\":\"it\"}",
                "implied_groups": ["user"],
                "active": True,
                "deleted": 0,
            }
        ]
    )
    user_model = _UserRuleModel([{"rec_name": "u1", "active": True, "deleted": 0}])
    env = _Env(
        {"group_users": group_users, "groups": groups_model, "user": user_model},
        session=session,
    )

    groups = asyncio.run(apply_session_groups(env, session))

    assert groups == ["technical_operator", "user"]
    serialized_domain = json.dumps(user_model.domains[0], sort_keys=True)
    assert '"active": true' in serialized_domain
    assert '"department": "it"' in serialized_domain
    assert '"rec_name": "u1"' in serialized_domain


def test_group_users_schema_has_editable_app_code_with_cookie_default():
    components = json.loads(Path("app/base/schema/components.json").read_text())
    group_users = next(
        item for item in components if item["rec_name"] == "group_users"
    )
    app_code = next(
        item for item in group_users["components"] if item.get("key") == "app_code"
    )

    assert app_code["type"] == "textfield"
    assert app_code["input"] is True
    assert app_code["validate"]["required"] is True
    assert "document.cookie" in app_code["customDefaultValue"]
    assert "app_code" in app_code["customDefaultValue"]


def test_groups_schema_and_seed_define_default_implications():
    components = json.loads(Path("app/base/schema/components.json").read_text())
    groups_schema = next(item for item in components if item["rec_name"] == "groups")
    implied_groups = next(
        item
        for item in groups_schema["components"]
        if item.get("key") == "implied_groups"
    )
    rule = next(
        item for item in groups_schema["components"] if item.get("key") == "rule"
    )
    groups = json.loads(Path("app/base/data/groups.json").read_text())
    by_name = {item["rec_name"]: item for item in groups}

    assert implied_groups["type"] == "select"
    assert implied_groups["multiple"] is True
    assert implied_groups["properties"]["model"] == "groups"
    assert rule["type"] == "textarea"
    assert rule["properties"]["jeditor"] == "y"
    assert by_name["manager"]["implied_groups"] == ["user", "operator"]
    assert by_name["manager"]["rule"] == "{}"
    assert by_name["technical_operator"]["label"] == "Technical Operator"
