import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from app.ozon_env_acl import _record_matches_filters
from app.ozon_env_acl import apply_session_groups
from app.ozon_env_acl import enforce_write_acl
from app.services.service import Service


def _domain_matches(row, domain):
    """Domain vuoto ({}) = match tutto (semantica list senza filtro); un
    domain non vuoto e' un filtro mongo-shaped valutato con la stessa
    matching logic (fail-closed $and/$or/$in) usata in produzione per
    valutare record_rulse — necessario per verificare che
    record_rule_read_domain narrowi davvero i risultati, non solo che
    costruisca la clausola giusta."""
    if not domain:
        return True
    return _record_matches_filters(row, domain)


class _Status:
    fail = False
    msg = ""


class _ModelStub:
    """Fake della classe pydantic generata (record_model.model in
    produzione) — espone SOLO le classmethod Layer 3 (`get_field_rules()`/
    `get_field_rules_conditions()`, baked a codegen-time in ozon-env da
    `properties.f_rule`/`f_rule_cond`) di cui Service._load_field_rule_
    policies/_get_field_rule_conditions ha bisogno, configurabili per test.
    Default vuoto = nessuna regola Layer 3 (comportamento baseline)."""

    def __init__(self, field_rules=None, field_rule_conditions=None):
        self._field_rules = field_rules or {}
        self._field_rule_conditions = field_rule_conditions or {}

    @staticmethod
    def schema():
        return {"components": []}

    @staticmethod
    def filter_keys():
        return {}

    def get_field_rules(self):
        return self._field_rules

    def get_field_rules_conditions(self):
        return self._field_rule_conditions

    def get_restricted_fields(self):
        return sorted(set(self._field_rules) | set(self._field_rule_conditions))


class _RecordModel:
    def __init__(self, name, rows=None, field_rules=None, field_rule_conditions=None):
        self.data_model = name
        self.status = _Status()
        self.model = _ModelStub(
            field_rules=field_rules, field_rule_conditions=field_rule_conditions
        )
        self.table_columns = {}
        self.rows = list(rows or [])

    def get_domain(self, query):
        return query

    async def count(self, domain):
        return len([row for row in self.rows if _domain_matches(row, domain)])

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
        return [
            row.copy() for row in self.rows if _domain_matches(row, domain)
        ]

    async def by_name(self, name):
        for row in self.rows:
            if row.get("rec_name") == name:
                return row.copy()
        return {}

    async def upsert(
        self,
        data=None,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
    ):
        data = dict(data or {})
        target = rec_name or data.get("rec_name")
        for row in self.rows:
            if row.get("rec_name") == target:
                row.update(data)
                return row.copy()
        record = dict(data)
        record.setdefault("rec_name", target)
        self.rows.append(record)
        return record.copy()

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
            if _domain_matches(row, domain):
                yield row.copy()


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


class _ModelGroupsRuleModel:
    """Fake per la collection `model_groups_rule` — fonte di verita' letta
    da Service._get_model_group_access (gate CRUD a livello di MODEL,
    fail-closed per i non-admin: senza una riga che copra il gruppo
    dell'attore, l'accesso e' negato PRIMA di arrivare a fields_rule/
    record_rulse)."""

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


def _model_group_row(model, group, *, read=True, create=False, update=False,
                      delete=False, export=False, app_code="demo"):
    return {
        "app_code": app_code,
        "model": model,
        "group": group,
        "read": read,
        "create": create,
        "update": update,
        "delete": delete,
        "export": export,
        "active": True,
        "deleted": 0,
    }


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
    """`model_groups_rule` di default: se il chiamante non registra la
    propria fake (i test che esercitano davvero il gate model-level lo
    fanno esplicitamente), sintetizza righe permissive full-CRUD per i
    gruppi della sessione su ogni model registrato — rappresenta lo
    stato "sync gia' avvenuto, ACL di default configurata" di una vera
    installazione, cosi' i test che esercitano fields_rule/record_rulse
    (un layer DIVERSO e piu' fine) non devono ripetere lo stesso
    boilerplate model-level in ognuno."""

    def __init__(self, models, session):
        self.user_session = session
        self.orm = SimpleNamespace(
            app_settings=SimpleNamespace(
                module_name="demo", version="1.0.0", logo_img_url=""
            )
        )
        self._models = dict(models)
        if "model_groups_rule" not in self._models:
            user = getattr(session, "user", None) or {}
            groups = user.get("groups") if isinstance(user, dict) else []
            rows = [
                _model_group_row(
                    model_name, group,
                    read=True, create=True, update=True, delete=True,
                )
                for model_name in self._models
                for group in (groups or [])
            ]
            self._models["model_groups_rule"] = _ModelGroupsRuleModel(rows)
        self.db = SimpleNamespace(engine=None)
        # Service._load_field_rule_policies (Layer 3) itera env.models
        # direttamente (zero I/O, no collection dedicata) — stesso dict di
        # _models, coerente con la produzione dove env.get(name) e
        # env.models[name] sono lo stesso oggetto.
        self.models = self._models

    def get(self, model_name):
        return self._models[model_name]


def _session(groups=None, is_admin=False, uid="u1"):
    return SimpleNamespace(
        app_code="demo",
        uid=uid,
        is_admin=is_admin,
        user={"uid": uid, "groups": groups or []},
    )


def test_load_field_rule_policies_emits_read_obfuscate_and_write_deny():
    """Service._load_field_rule_policies (Layer 3) legge Model.
    get_field_rules() da env.models (zero I/O, niente DB) — f_rule.read
    produce una policy READ/OBFUSCATE (exclude_groups = i gruppi listati);
    f_rule.write produce UNA sola policy DENY, solo su UPDATE (mai su
    INSERT — a creation-time non esiste ancora un valore da proteggere da
    un blind-overwrite, e il creatore ne diventa owner nello stesso
    istante: bloccare l'INSERT romperebbe qualsiasi self-service form).
    Comportamento NUOVO, prima dead code in fields_rule.allowed_groups.
    actions.{create,update} (mai consumato).

    Isolamento multi-app: niente piu' test dedicato a "policy di un altro
    app_code non deve contaminare" — non serve piu', e' strutturale.
    _load_field_rule_policies itera env.models, che e' gia' single-app
    (un env = un app_code, vedi get_ozon_env), quindi non esistono righe
    di un app_code diverso da unire/filtrare."""
    users = _RecordModel(
        "user",
        rows=[],
        field_rules={"codicefiscale": {"read": ["gdpr"], "write": ["gdpr"]}},
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    policies = service._load_field_rule_policies()

    by_operation = {p["operation"]: p for p in policies}
    assert len(policies) == 2
    assert set(by_operation) == {"read", "update"}
    read_policy = by_operation["read"]
    assert read_policy["model_key"] == "user"
    assert read_policy["field_path"] == "codicefiscale"
    assert read_policy["effect"] == "obfuscate"
    assert read_policy["actor_selector"]["exclude_groups"] == ["gdpr"]
    # niente is_admin nel selector: f_rule NON deve bypassare l'admin
    # (GDPR: essere admin non significa essere autorizzati al dato).
    assert "is_admin" not in read_policy["actor_selector"]

    update_policy = by_operation["update"]
    assert update_policy["effect"] == "deny"
    assert update_policy["actor_selector"]["exclude_groups"] == ["gdpr"]


def test_load_field_rule_policies_ignores_models_without_layer3():
    """Model senza get_field_rules()/con dict vuoto (baseline, la
    stragrande maggioranza) non produce nessuna policy — nessuna
    regressione su model mai configurati con f_rule."""
    plain = _RecordModel("plain_model", rows=[])
    env = _Env({"plain_model": plain}, session=_session(groups=["sales"]))
    service = Service(env)

    policies = service._load_field_rule_policies()

    assert policies == []


def test_field_rule_write_denied_even_when_read_revealed_by_condition():
    """f_rule_cond sblocca SOLO il read, mai il write (esplicito: "sblocca
    solo il read poi saranno le altre rule a definire se puoi scrivere,
    altrimenti rischio bypass le logiche del service e caos"). Qui il
    campo e' oscurato per chiunque non sia in "gdpr" (f_rule.read/write =
    [gdpr]) MA f_rule_cond rivela il campo in lettura per il proprietario
    del record. Verifica end-to-end sullo STESSO attore (owner1) che vede
    davvero il campo in lettura (load_record) e che ciononostante NON
    riesce a modificarlo scrivendo (Service.upsert, il varco reale) — il
    read-reveal non deve mai propagarsi al write.

    Un campo negato in scrittura NON blocca l'intero salvataggio (stessa
    filosofia del read: oscura, non 404 tutto): il resto del payload
    (un campo che l'attore PUO' scrivere) deve comunque passare."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "owner1",
                "owner_uid": "owner1",
                "name": "Owner Record",
                "codicefiscale": "OWN123",
            }
        ],
        field_rules={"codicefiscale": {"read": ["gdpr"], "write": ["gdpr"]}},
        field_rule_conditions={
            "codicefiscale": {"owner_uid": {"$eq": {"var": "user.uid"}}}
        },
    )

    async def _try_write(session, name_value):
        env = _Env({"user": users}, session=session)
        service = Service(env)
        return await service.upsert(
            model_name="user",
            data={"codicefiscale": "HACKED", "name": name_value},
            rec_name="owner1",
        )

    # owner ma non-gdpr: f_rule_cond gli rivela DAVVERO il campo in
    # lettura (stesso attore, path reale load_record)...
    owner_session = _session(groups=["sales"], uid="owner1")
    owner_env = _Env({"user": users}, session=owner_session)
    owner_service = Service(owner_env)
    read_response = asyncio.run(owner_service.load_record("user", "owner1"))
    assert read_response.content.data["codicefiscale"] == "OWN123"

    # ...ma scrivere lo STESSO campo, dallo STESSO attore, non lo cambia:
    # il campo resta com'era, MA il resto del payload (name) passa.
    response = asyncio.run(_try_write(owner_session, "Owner Updated"))
    assert response.fail is False
    assert users.rows[0]["codicefiscale"] == "OWN123"
    assert users.rows[0]["name"] == "Owner Updated"

    # gdpr: scrittura consentita (f_rule.write li elenca esplicitamente).
    response = asyncio.run(_try_write(_session(groups=["gdpr"], uid="u2"), "GDPR Updated"))
    assert response.fail is False
    assert users.rows[0]["codicefiscale"] == "HACKED"
    assert users.rows[0]["name"] == "GDPR Updated"

    # ne' owner ne' gdpr: campo bloccato (ripristinato al valore corrente,
    # cioe' "HACKED" scritto sopra da gdpr — non c'e' nulla di magico nel
    # nome, e' solo l'ultimo valore legittimo), ma name passa comunque.
    response = asyncio.run(_try_write(_session(groups=["marketing"], uid="u3"), "Marketing Updated"))
    assert response.fail is False
    assert users.rows[0]["codicefiscale"] == "HACKED"
    assert users.rows[0]["name"] == "Marketing Updated"


def test_field_rule_write_insert_ungated_update_gated():
    """f_rule.write nega SOLO su UPDATE, mai su INSERT: a creation-time
    non esiste un valore esistente da proteggere, e chi crea il record ne
    diventa owner nello stesso istante — negare l'INSERT romperebbe il
    self-service (es. l'operatore che valorizza "Data di Nascita" alla
    creazione del form). Un attore non nei gruppi write puo' quindi
    settare liberamente il campo in INSERT, ma non modificarlo in UPDATE
    dopo che il record esiste (il campo resta al valore impostato in
    creazione, il resto del payload passa comunque)."""
    users = _RecordModel(
        "user",
        rows=[],
        field_rules={"codicefiscale": {"read": ["gdpr"], "write": ["gdpr"]}},
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="operator1"))
    service = Service(env)

    # INSERT: nessun gruppo write richiesto, il campo si crea liberamente.
    insert_response = asyncio.run(
        service.upsert(
            model_name="user",
            data={"rec_name": "rec1", "codicefiscale": "FIRST123", "name": "A"},
            rec_name="rec1",
        )
    )
    assert insert_response.fail is False
    assert users.rows[0]["codicefiscale"] == "FIRST123"

    # UPDATE: stesso attore, stesso campo -> negato (resta FIRST123), ma
    # "name" (non gated) passa.
    update_response = asyncio.run(
        service.upsert(
            model_name="user",
            data={"codicefiscale": "SECOND456", "name": "B"},
            rec_name="rec1",
        )
    )
    assert update_response.fail is False
    assert users.rows[0]["codicefiscale"] == "FIRST123"
    assert users.rows[0]["name"] == "B"


def test_field_rule_write_owner_sentinel_allows_owner_after_creation():
    """Sentinel `$owner` in f_rule.write (vedi Service._OWNER_WRITE_
    SENTINEL/_get_field_owner_writable_fields): un campo group-write-gated
    resta scrivibile dall'OWNER del record anche DOPO la creazione, senza
    dover essere nei gruppi write espliciti — risolve il limite "l'owner
    non puo' correggere un proprio errore di battitura dopo l'INSERT".
    Verifica end-to-end tramite Service.upsert (il varco reale)."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "owner1",
                "owner_uid": "owner1",
                "codicefiscale": "OLD123",
            }
        ],
        field_rules={
            "codicefiscale": {"read": ["gdpr"], "write": ["gdpr", "$owner"]}
        },
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="owner1"))
    service = Service(env)

    response = asyncio.run(
        service.upsert(
            model_name="user",
            data={"codicefiscale": "NEW456"},
            rec_name="owner1",
        )
    )

    assert response.fail is False
    assert users.rows[0]["codicefiscale"] == "NEW456"


def test_field_rule_write_owner_sentinel_checks_stored_record_not_payload():
    """Il match per `$owner` usa l'owner_uid dello STORED record, MAI
    quello dichiarato nel payload in arrivo: un attaccante che scrive
    owner_uid=se-stesso nell'update non deve auto-concedersi lo sblocco
    $owner su un record che non possiede davvero. Il punto esatto dove
    uno slip diventerebbe una vulnerabilita' — Service.upsert deve leggere
    l'owner dallo STORED record (via _resolve_write_operation), mai dal
    payload. Un campo negato in scrittura non blocca l'intero salvataggio:
    il campo protetto resta invariato, ma un campo co-inviato che
    l'attaccante PUO' scrivere (name, non gated) passa comunque — la
    proprieta' verificata e' "l'attaccante non altera il campo protetto",
    non "l'intera richiesta fallisce"."""
    users = _RecordModel(
        "user",
        rows=[
            {
                "rec_name": "victim_record",
                "owner_uid": "victim1",
                "codicefiscale": "OLD123",
                "name": "Victim",
            }
        ],
        field_rules={
            "codicefiscale": {"read": ["gdpr"], "write": ["gdpr", "$owner"]}
        },
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="attacker1"))
    service = Service(env)

    response = asyncio.run(
        service.upsert(
            model_name="user",
            data={
                "codicefiscale": "HACKED",
                "owner_uid": "attacker1",
                "name": "Attacker Renamed It",
            },
            rec_name="victim_record",
        )
    )

    assert response.fail is False
    assert users.rows[0]["codicefiscale"] == "OLD123"
    assert users.rows[0]["name"] == "Attacker Renamed It"


def test_models_groups_denies_whole_model_for_non_member_group():
    """model_groups_rule e' l'unica fonte di verita' per il gate CRUD a
    livello di MODEL (retirato synth_policies_from_component_properties,
    che leggeva component.properties direttamente): nessuna riga per il
    gruppo dell'attore -> fail-closed, 404 (non piu' record svuotato a
    livello di campo)."""
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    env = _Env(
        {
            "customer": customer,
            "model_groups_rule": _ModelGroupsRuleModel(
                [_model_group_row("customer", "hr", read=True)]
            ),
        },
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    try:
        asyncio.run(service.load_record("customer", "c1"))
        assert False, "expected HTTPException 404"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_models_groups_allows_member_group():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    env = _Env(
        {
            "customer": customer,
            "model_groups_rule": _ModelGroupsRuleModel(
                [_model_group_row("customer", "hr", read=True)]
            ),
        },
        session=_session(groups=["hr"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_groups_bypassed_for_admin():
    customer = _RecordModel("customer", rows=[{"rec_name": "c1", "name": "Ada"}])
    env = _Env(
        {
            "customer": customer,
            "model_groups_rule": _ModelGroupsRuleModel([]),
        },
        session=_session(groups=[], is_admin=True),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"


def test_models_restricted_fields_hides_field_for_non_allowed_group():
    """Restrizione a livello di CAMPO: Layer 3 (Model.get_field_rules(),
    baked a codegen-time in ozon-env da properties.f_rule), non piu'
    model_fields_rule rule_type="fields" (ritirato)."""
    customer = _RecordModel(
        "customer",
        rows=[{"rec_name": "c1", "name": "Ada", "salary": 42}],
        field_rules={"salary": {"read": ["hr"], "write": []}},
    )
    env = _Env(
        {
            "customer": customer,
            "model_groups_rule": _ModelGroupsRuleModel(
                [_model_group_row("customer", "sales", read=True)]
            ),
        },
        session=_session(groups=["sales"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("customer", "c1"))

    assert response.content.data["name"] == "Ada"
    assert response.content.data["salary"] is None
    assert response.content.obfucated_fields == ["salary"]


def _gdpr_field_rules(record_filters=None):
    """Layer 3 (field_rules/field_rule_conditions) per il campo
    codicefiscale: oscurato di base salvo gruppo gdpr (f_rule.read),
    sbloccato in LETTURA se la condizione matcha il record (f_rule_cond,
    default owner_uid == user.uid) — mai in scrittura, quella resta
    governata da f_rule.write/Layer 2."""
    if record_filters is None:
        record_filters = {"owner_uid": {"$eq": {"var": "user.uid"}}}
    field_rules = {"codicefiscale": {"read": ["gdpr"], "write": ["gdpr"]}}
    field_rule_conditions = {"codicefiscale": record_filters}
    return field_rules, field_rule_conditions


def test_fields_rule_obfuscates_field_without_gdpr_group():
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["name"] == "Ada"
    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_fields_rule_obfuscates_field_for_admin_without_gdpr_group_or_ownership():
    """Regression: admin NON deve bypassare f_rule/f_rule_cond — solo
    il gruppo gdpr o essere proprietario del record sbloccano il campo,
    admin incluso (segnalato dall'utente: vedeva codicefiscale in chiaro
    su tutta la lista utenti pur essendo solo admin, senza gdpr)."""
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env(
        {"user": users},
        session=_session(groups=[], is_admin=True, uid="admin1"),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_fields_rule_visible_with_gdpr_group():
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["gdpr"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] == "ABC123"
    assert response.content.obfucated_fields == []


def test_record_rulse_overrides_baseline_for_owned_record():
    """Utente senza gruppo gdpr ma proprietario del record (owner_uid ==
    user.uid): f_rule_cond matcha e sblocca in lettura il campo altrimenti
    oscurato dalla baseline f_rule. Nessun record_rulse configurato: Layer
    2 resta senza restrizioni (record_rule_access: niente record_rulse =
    accesso pieno), questo test isola Layer 3."""
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"
    assert response.content.obfucated_fields == []


def test_field_rule_conditions_reveal_matching_field_only():
    """Due campi oscurati dalla baseline (codicefiscale, token): solo
    codicefiscale ha una f_rule_cond configurata (owner match, vero per
    questo record) -> si sblocca; token non ha NESSUNA condizione
    configurata -> resta oscurato per sempre, indipendente dal match di
    altri campi (apply_field_rule_conditions e' per-campo, non per-regola:
    niente piu' concetto di "scope" condiviso tra campi diversi)."""
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
        field_rules={
            "codicefiscale": {"read": ["gdpr"], "write": ["gdpr"]},
            "token": {"read": ["gdpr"], "write": ["gdpr"]},
        },
        field_rule_conditions={
            "codicefiscale": {"owner_uid": {"$eq": {"var": "user.uid"}}},
        },
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"
    assert response.content.data["token"] is None
    assert response.content.obfucated_fields == ["token"]


def test_record_rulse_keeps_baseline_when_no_match():
    """Utente senza gdpr e non proprietario: f_rule_cond non matcha, la
    baseline f_rule resta in vigore (campo oscurato)."""
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_record_rulse_and_wrapped_filter_matches_owner():
    """filters avvolti in $and (forma Mongo comune, non la forma flat degli
    esempi base) devono comunque matchare correttamente per il proprietario."""
    field_rules, field_rule_conditions = _gdpr_field_rules(
        record_filters={"$and": [{"owner_uid": {"$eq": {"var": "user.uid"}}}]}
    )
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"]))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u1"))

    assert response.content.data["codicefiscale"] == "OWN123"


def test_record_rulse_and_wrapped_filter_fails_closed_for_non_owner():
    """Stesso filtro $and, ma per un record non di proprieta': deve fallire
    CLOSED (campo resta oscurato), non aprirsi a chiunque per il solo fatto
    che $and non era gestito esplicitamente (regressione verificata: prima
    del fix $and/$or venivano ignorati e il match tornava sempre True)."""
    field_rules, field_rule_conditions = _gdpr_field_rules(
        record_filters={"$and": [{"owner_uid": {"$eq": {"var": "user.uid"}}}]}
    )
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="u1"))
    service = Service(env)

    response = asyncio.run(service.load_record("user", "u2"))

    assert response.content.data["codicefiscale"] is None
    assert response.content.obfucated_fields == ["codicefiscale"]


def test_record_rulse_varies_per_row_in_list_records():
    """list_records: stesso utente (sales, no gdpr) vede il proprio record
    in chiaro e quello altrui oscurato, nella STESSA risposta (f_rule_cond
    valutata riga per riga)."""
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="u1"))
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
    POST /list/{model}) deve applicare f_rule_cond riga per riga tanto
    quanto il path non-stream — stesso scenario del test precedente ma
    passando per service.stream_record (NDJSON)."""
    field_rules, field_rule_conditions = _gdpr_field_rules()
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
        field_rules=field_rules,
        field_rule_conditions=field_rule_conditions,
    )
    env = _Env({"user": users}, session=_session(groups=["sales"], uid="u1"))
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


def test_record_rulse_union_multi_group_load_record():
    """Layer 2, union: utente in due gruppi (gdpr, manager), due entry
    record_rulse DIVERSE matchano lo stesso record (owner-match per gdpr,
    un altro campo per manager) — le azioni finali sono l'OR delle due,
    non quelle della prima entry nell'ordine di config (comportamento
    corretto dopo la rimozione del first-match-wins)."""
    read_only_owner_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "gdpr",
        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    update_grant_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "manager",
        "filters": {"name": {"$eq": "Mine"}},
        "read": False,
        "create": False,
        "update": True,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u1", "name": "Mine"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [read_only_owner_rule, update_grant_rule]
            ),
            "component": _SysFlagComponentModel({"modulo_dati_persona": False}),
        },
        session=_session(uid="u1", groups=["gdpr", "manager"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("modulo_dati_persona", "d1"))

    assert response.content.readable is True
    assert response.content.editable is True


def test_record_rulse_read_domain_unions_real_group_filters_for_list():
    """record_rule_read_domain unisce (OR) i filtri REALI (non vuoti) di
    tutte le entry con read=True che si applicano alla sessione — un
    utente in due gruppi scoped con filtri diversi vede l'unione delle
    righe coperte da ciascuno."""
    gdpr_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "gdpr",
        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    manager_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "manager",
        "filters": {"name": {"$eq": "B"}},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[
            {"rec_name": "d1", "owner_uid": "u1", "name": "A"},
            {"rec_name": "d2", "owner_uid": "owner2", "name": "B"},
            {"rec_name": "d3", "owner_uid": "owner3", "name": "C"},
        ],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel([gdpr_rule, manager_rule]),
            "component": _SysFlagComponentModel({"modulo_dati_persona": False}),
        },
        session=_session(uid="u1", groups=["gdpr", "manager"]),
    )
    service = Service(env)

    response = asyncio.run(
        service.list_records(
            model_name="modulo_dati_persona",
            query={},
            order="",
            skip=0,
            limit=10,
        )
    )

    rec_names = {row["rec_name"] for row in response.content.data}
    assert rec_names == {"d1", "d2"}


def test_record_rulse_empty_filter_group_scoped_never_matches():
    """Regressione: filtro vuoto su riga group-scoped NON deve piu' fare
    match incondizionato (hack rimosso — i gruppi non sono un campo del
    record, un grant incondizionato per gruppo e' Layer 1/model_groups_
    rule o Layer 3/f_rule, mai Layer 2/record_rulse). dpo con filters={}
    ottiene 404, non read-all."""
    dpo_empty_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "dpo",
        "filters": {},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "owner1", "name": "Other"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel([dpo_empty_rule]),
            "component": _SysFlagComponentModel({"modulo_dati_persona": False}),
        },
        session=_session(uid="u9", groups=["dpo"]),
    )
    service = Service(env)

    try:
        asyncio.run(service.load_record("modulo_dati_persona", "d1"))
        assert False, "expected HTTPException 404"
    except HTTPException as exc:
        assert exc.status_code == 404


class _SysFlagComponentModel:
    """Fake per la collection `component` — usata SOLO da
    Service._is_sys_model per decidere se il model e' config condivisa
    (sys=True, enforcement record_rulse saltato) o un documento applicativo
    (sys=False, enforcement attivo). Ritorna un oggetto con attributo `.sys`
    (non un dict) — Service._is_sys_model legge `component.sys` diretto,
    coerente con un vero record ORM."""

    def __init__(self, sys_by_model):
        self._sys_by_model = sys_by_model

    def get_domain(self, query):
        return query

    async def by_name(self, name):
        if name not in self._sys_by_model:
            return None
        return SimpleNamespace(rec_name=name, sys=self._sys_by_model[name])


def _owner_only_record_rule(model="modulo_dati_persona", app_code="demo"):
    """Riga model_fields_rule rule_type=record — la stessa iniettata di
    default da normalize_component_properties su ogni component non-
    identity (owner_uid == user.uid -> read/create/update/delete=True)."""
    return {
        "app_code": app_code,
        "model": model,
        "rule_type": "record",
        "group": "",
        "restricted_fields": [],
        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
        "read": True,
        "create": True,
        "update": True,
        "delete": True,
        "active": True,
        "deleted": 0,
    }


def test_load_record_non_sys_denies_non_owner():
    """Task: "readonly regole per apertura/accesso ai documenti di cui non
    sei proprietario, altrimenti nascosto o readonly". Model non-sys, non
    owner, nessuna regola matcha -> fail-closed, record nascosto (404)."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u2", "name": "Other"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="u1"),
    )
    service = Service(env)

    try:
        asyncio.run(service.load_record("modulo_dati_persona", "d1"))
        assert False, "expected HTTPException 404"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_load_record_non_sys_owner_full_access():
    """Stesso model, ma l'utente e' owner: regola matcha, read/update=True
    -> record visibile ed editable."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u1", "name": "Mine"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("modulo_dati_persona", "d1"))

    assert response.content.readable is True
    assert response.content.editable is True


def test_record_rulse_group_scoped_restricts_only_that_group():
    """record_rulse con `group` valorizzato (righe generate da un'entry
    con "groups": [...] in model_rules_sync, non piu' sempre group="")
    si applica SOLO alle sessioni membre di quel gruppo — vedi
    Service._get_record_rulse. gdpr limitato ai propri record (owner_uid
    check), manager limitato a un altro criterio (rec_name) — le due entry
    non si "vedono" a vicenda: un utente gdpr non owner resta negato anche
    se esiste una entry manager che matcherebbe (non e' nel gruppo)."""
    gdpr_scoped_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "gdpr",
        "filters": {"owner_uid": {"$eq": {"var": "user.uid"}}},
        "read": True,
        "create": True,
        "update": True,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    manager_scoped_rule = {
        "app_code": "demo",
        "model": "modulo_dati_persona",
        "rule_type": "record",
        "group": "manager",
        "filters": {"rec_name": {"$eq": "d1"}},
        "read": True,
        "create": False,
        "update": False,
        "delete": False,
        "active": True,
        "deleted": 0,
    }
    rules = [gdpr_scoped_rule, manager_scoped_rule]
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "owner1", "name": "Other"}],
    )

    def _env(uid, groups):
        return _Env(
            {
                "modulo_dati_persona": docs,
                "model_fields_rule": _ModelFieldsRuleModel(rules),
                "component": _SysFlagComponentModel(
                    {"modulo_dati_persona": False}
                ),
            },
            session=_session(uid=uid, groups=groups),
        )

    # gdpr, non owner -> negato (l'unica riga per il SUO gruppo richiede
    # ownership; la entry manager esiste ma non lo riguarda).
    try:
        asyncio.run(Service(_env("u1", ["gdpr"])).load_record(
            "modulo_dati_persona", "d1"
        ))
        assert False, "expected HTTPException 404"
    except HTTPException as exc:
        assert exc.status_code == 404

    # manager -> letto (la sua entry matcha su rec_name, indipendente da
    # ownership).
    response = asyncio.run(Service(_env("u2", ["manager"])).load_record(
        "modulo_dati_persona", "d1"
    ))
    assert response.content.readable is True
    assert response.content.editable is False

    # gdpr, owner -> letto e editable (match sulla propria riga).
    owned_docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d2", "owner_uid": "u3", "name": "Mine"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": owned_docs,
            "model_fields_rule": _ModelFieldsRuleModel(rules),
            "component": _SysFlagComponentModel({"modulo_dati_persona": False}),
        },
        session=_session(uid="u3", groups=["gdpr"]),
    )
    response = asyncio.run(Service(env).load_record("modulo_dati_persona", "d2"))
    assert response.content.readable is True
    assert response.content.editable is True


def test_load_record_non_sys_matched_rule_readonly_when_update_false():
    """Una regola puo' matchare e concedere solo read (update=False) ->
    record visibile ma readonly, non hidden."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u2", "name": "Shared"}],
    )
    read_only_rule = {
        **_owner_only_record_rule(),
        "filters": {"rec_name": {"$eq": "d1"}},
        "update": False,
    }
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel([read_only_rule]),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("modulo_dati_persona", "d1"))

    assert response.content.readable is True
    assert response.content.editable is False


def test_load_record_non_sys_admin_does_not_bypass_ownership():
    """Admin NON bypassa piu' l'enforcement record-level su model non-sys
    (coerente col fields_rule GDPR-style, che non concede bypass admin):
    nessuna regola matcha per l'admin -> fail-closed, record nascosto."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u2", "name": "Other"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="admin1", is_admin=True),
    )
    service = Service(env)

    try:
        asyncio.run(service.load_record("modulo_dati_persona", "d1"))
        assert False, "expected HTTPException 404"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_load_record_non_sys_admin_access_when_rule_matches():
    """Admin, come chiunque altro, ottiene accesso record-level se una
    regola matcha davvero (qui: admin1 e' owner del record)."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "admin1", "name": "Mine"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="admin1", is_admin=True),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("modulo_dati_persona", "d1"))

    assert response.content.readable is True
    assert response.content.editable is True


def test_load_record_sys_model_bypasses_record_rulse():
    """Model sys (config condivisa: action/menu_group/settings/user...) non
    e' soggetto all'enforcement hide/readonly anche se ha lo stesso
    record_rulse owner-only iniettato di default — l'ownership per-record
    non ha senso su config condivisa, gia' regolata da models_groups."""
    docs = _RecordModel(
        "settings", rows=[{"rec_name": "s1", "owner_uid": "u2", "name": "Cfg"}]
    )
    env = _Env(
        {
            "settings": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule(model="settings")]
            ),
            "component": _SysFlagComponentModel({"settings": True}),
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("settings", "s1"))

    assert response.content.readable is True
    assert response.content.editable is True


def test_load_record_unknown_sys_flag_fails_open_bypasses_enforcement():
    """Se il lookup del component fallisce (non registrato/errore), il
    model e' trattato come sys (enforcement saltato) — fail-open per questa
    feature specifica: il rischio di bloccare per errore config condivisa e'
    peggiore del rischio di non restringere un record realmente non-sys."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[{"rec_name": "d1", "owner_uid": "u2", "name": "Other"}],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            # "component" non registrato: env.get("component") solleva KeyError.
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(service.load_record("modulo_dati_persona", "d1"))

    assert response.content.readable is True
    assert response.content.editable is True


def test_list_records_non_sys_hides_non_owned_rows():
    """list_records su model non-sys: le righe non-owned e non coperte da
    nessuna regola spariscono dalla lista (non solo oscurate a livello di
    campo) — narrowing lato domain, non post-filter in Python."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[
            {"rec_name": "d1", "owner_uid": "u1", "name": "Mine"},
            {"rec_name": "d2", "owner_uid": "u2", "name": "Other"},
        ],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel(
                [_owner_only_record_rule()]
            ),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(
        service.list_records(
            model_name="modulo_dati_persona",
            query={},
            order="",
            skip=0,
            limit=10,
        )
    )

    rec_names = {row["rec_name"] for row in response.content.data}
    assert rec_names == {"d1"}
    assert response.content.total_count == 1


def test_list_records_non_sys_no_rule_at_all_unrestricted():
    """Se il model non ha ALCUN record_rulse configurato, resta senza
    restrizioni (nessuna regressione su model senza fields_rule/record_
    rulse mai configurate)."""
    docs = _RecordModel(
        "modulo_dati_persona",
        rows=[
            {"rec_name": "d1", "owner_uid": "u1", "name": "Mine"},
            {"rec_name": "d2", "owner_uid": "u2", "name": "Other"},
        ],
    )
    env = _Env(
        {
            "modulo_dati_persona": docs,
            "model_fields_rule": _ModelFieldsRuleModel([]),
            "component": _SysFlagComponentModel(
                {"modulo_dati_persona": False}
            ),
        },
        session=_session(uid="u1", groups=["user"]),
    )
    service = Service(env)

    response = asyncio.run(
        service.list_records(
            model_name="modulo_dati_persona",
            query={},
            order="",
            skip=0,
            limit=10,
        )
    )

    rec_names = {row["rec_name"] for row in response.content.data}
    assert rec_names == {"d1", "d2"}
    assert response.content.total_count == 2


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
