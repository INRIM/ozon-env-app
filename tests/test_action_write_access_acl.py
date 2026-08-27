"""ACL scrittura sulle action: `write_access: true` richiede il permesso
CRUD di scrittura (create|update) sul model target, non basta il gruppo.

Caso di riferimento (default models_groups): il gruppo `user` ha
read=True, create/update=False — quindi vede le action di lettura ma non
quelle con `write_access`, e il form di nuovo record si apre readonly.
"""
import asyncio
from types import SimpleNamespace

from app.services.action_runtime import ActionRuntime
from app.services.common import ResponseObjectData
from app.services.service import Service


NO_ACCESS = {
    "read": False,
    "create": False,
    "update": False,
    "delete": False,
    "export": False,
}
READ_ONLY = {**NO_ACCESS, "read": True}
READ_WRITE = {**NO_ACCESS, "read": True, "create": True, "update": True}


def _runtime(access_by_model, *, is_admin=False, groups=None, is_public=False):
    session = SimpleNamespace(
        is_admin=is_admin,
        is_public=is_public,
        uid="u1",
        app_code="test_app",
        user={"uid": "u1", "groups": groups or []},
    )
    # Service reale senza __init__: i gate (_assert_model_operation/
    # _assert_record_operation) sono quelli di produzione, stubbata solo
    # la sorgente dei permessi.
    service = Service.__new__(Service)
    service.session = session

    async def _access(model_name):
        if session.is_admin:
            return {key: True for key in NO_ACCESS}
        return dict(access_by_model.get(model_name, NO_ACCESS))

    service._get_model_group_access = _access
    return ActionRuntime(service)


def _action(**kwargs):
    data = {
        "rec_name": "act",
        "admin": False,
        "sys": False,
        "model": "documento",
        "groups": [],
        "write_access": False,
    }
    data.update(kwargs)
    return SimpleNamespace(**data)


def test_write_access_denied_when_group_has_no_write():
    """Gruppo dell'action matcha, ma il gruppo non ha create/update."""
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    action = _action(groups=["user"], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is False


def test_read_action_allowed_for_same_group_without_write():
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    action = _action(groups=["user"], write_access=False)
    assert asyncio.run(runtime._is_action_allowed(action)) is True


def test_write_access_allowed_when_group_has_write():
    runtime = _runtime({"documento": READ_WRITE}, groups=["operator"])
    action = _action(groups=["operator"], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is True


def test_write_access_allowed_with_update_only():
    access = {**NO_ACCESS, "read": True, "update": True}
    runtime = _runtime({"documento": access}, groups=["operator"])
    action = _action(groups=["operator"], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is True


def test_write_access_without_model_is_fail_closed():
    runtime = _runtime({}, groups=["operator"])
    action = _action(model="", groups=["operator"], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is False


def test_write_access_admin_bypass():
    runtime = _runtime({"documento": NO_ACCESS}, is_admin=True, groups=[])
    action = _action(groups=["operator"], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is True


def test_write_access_gate_applies_to_action_without_groups():
    """Action senza `groups`: prima passava sempre, ora il write gate vale."""
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    action = _action(groups=[], write_access=True)
    assert asyncio.run(runtime._is_action_allowed(action)) is False


def test_write_access_flag_as_string_is_enabled():
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    action = _action(groups=["user"], write_access="true")
    assert asyncio.run(runtime._is_action_allowed(action)) is False


def test_context_actions_hide_write_buttons_without_write_permission():
    """I pulsanti di contesto con write_access spariscono senza scrittura."""
    records = [
        _action(
            rec_name="open_edit",
            groups=["user"],
            write_access=True,
            context_button_mode=["list"],
            action_type="window",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Modifica",
            button_icon="",
            modal=False,
            list_order=1,
        ),
        _action(
            rec_name="open_view",
            groups=["user"],
            write_access=False,
            context_button_mode=["list"],
            action_type="window",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Vedi",
            button_icon="",
            modal=False,
            list_order=2,
        ),
    ]

    class FakeActionModel:
        async def find(self, domain=None, sort=None, limit=0, **kwargs):
            return records

    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())

    buttons = asyncio.run(runtime._get_context_actions("documento", "list"))
    assert [b["rec_name"] for b in buttons] == ["open_view"]

    # Con permesso di scrittura tornano entrambi.
    runtime = _runtime({"documento": READ_WRITE}, groups=["operator"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    for record in records:
        record.groups = ["operator"]
    buttons = asyncio.run(runtime._get_context_actions("documento", "list"))
    assert [b["rec_name"] for b in buttons] == ["open_edit", "open_view"]


def _form_runtime(access, *, groups):
    """Runtime per handle_get in mode=form su nuovo record (no rec_name)."""
    runtime = _runtime({"documento": access}, groups=groups)
    service = runtime.service

    class FakeSchemaRecord:
        components = []
        properties = {}
        title = "Documento"
        no_cancel = False

    async def _get_component_record(name):
        return FakeSchemaRecord()

    service._get_component_record = _get_component_record
    service._parse_query_dict = lambda val: {}
    service._resolve_query_json_logic_vars = lambda data: data

    action = _action(
        rec_name="new_documento",
        groups=groups,
        write_access=True,
        mode="form",
        view_name="",
        component_type="",
        type="data",
        list_query="",
        query="",
        listOrderString="",
        action_type="window",
        title="Nuovo",
        next_action_name="",
    )

    async def _get_action_record(name):
        return action

    async def _sequence(name, act):
        return {}

    async def _context_actions(*args, **kwargs):
        return []

    runtime.get_action_record = _get_action_record
    runtime._resolve_action_sequence = _sequence
    runtime._get_context_actions = _context_actions
    return runtime, action


def test_new_record_form_is_readonly_without_create():
    """Form nuovo record: senza `create` non deve aprirsi editabile.

    Il gate su `_is_action_allowed` e' bypassato qui apposta: si verifica
    il solo calcolo di editable/can_create sul ramo nuovo record.
    """
    runtime, action = _form_runtime(READ_ONLY, groups=["user"])
    action.write_access = False
    res = asyncio.run(runtime.handle_get(action_name="new_documento"))
    assert isinstance(res, ResponseObjectData)
    assert res.mode == "form"
    assert res.editable is False
    assert res.can_create is False


def test_new_record_form_editable_with_create():
    runtime, _action_rec = _form_runtime(READ_WRITE, groups=["operator"])
    res = asyncio.run(runtime.handle_get(action_name="new_documento"))
    assert res.editable is True
    assert res.can_create is True


def test_write_access_form_opens_readonly_instead_of_403():
    """`write_access` su mode=form: il form si apre, ma in sola lettura.

    Il pulsante che porta qui e' comunque nascosto (`_get_context_actions`
    usa il gate completo); chi arriva per URL diretto vede il record senza
    poterlo modificare, invece di prendere 403.
    """
    runtime, action = _form_runtime(READ_ONLY, groups=["user"])
    assert action.write_access is True
    res = asyncio.run(runtime.handle_get(action_name="new_documento"))
    assert res.mode == "form"
    assert res.editable is False
    assert res.can_create is False


def test_write_access_form_with_rec_name_opens_readonly():
    """Record esistente: load_record decide readable/editable, il gate
    write_access puo' solo togliere l'editabilita', mai concederla."""
    import types

    runtime, action = _form_runtime(READ_ONLY, groups=["user"])

    async def _load_record(model, rec_name):
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model,
                data={"rec_name": rec_name},
                readable=True,
                editable=True,
            )
        )

    runtime.service.load_record = _load_record
    res = asyncio.run(
        runtime.handle_get(action_name="new_documento", rec_name="doc-1")
    )
    assert res.readable is True
    assert res.editable is False


def test_write_access_non_form_action_is_denied():
    """Fuori dal form non c'e' niente da degradare: 403."""
    import pytest

    runtime, action = _form_runtime(READ_ONLY, groups=["user"])
    action.mode = "list"

    async def _list_records(model_name, query, order, **kwargs):
        raise AssertionError("list_records non deve essere raggiunto")

    runtime.service.list_records = _list_records
    with pytest.raises(Exception) as exc:
        asyncio.run(runtime.handle_get(action_name="new_documento"))
    assert getattr(exc.value, "status_code", None) == 403


def test_is_action_visible_ignores_write_access():
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    action = _action(groups=["user"], write_access=True)
    assert asyncio.run(runtime._is_action_visible(action)) is True
    assert asyncio.run(runtime._is_action_allowed(action)) is False


def test_form_mode_context_buttons_gated_by_action_type():
    """save/copy/delete hanno `write_access: false` nei dati base: il
    permesso richiesto lo deriva `action_type` (save -> create|update,
    copy -> create, delete -> delete). Senza, i pulsanti Salva/Elimina
    restavano visibili su un form readonly."""
    records = [
        _action(
            rec_name="save_documento",
            groups=["user"],
            write_access=False,
            context_button_mode=["form"],
            action_type="save",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Salva",
            button_icon="",
            modal=False,
        ),
        _action(
            rec_name="delete_documento",
            groups=["user"],
            write_access=False,
            context_button_mode=["form"],
            action_type="delete",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Elimina",
            button_icon="",
            modal=False,
        ),
    ]

    class FakeActionModel:
        async def find(self, domain=None, sort=None, limit=0, **kwargs):
            return records

    # Sola lettura: nessun pulsante di scrittura.
    runtime = _runtime({"documento": READ_ONLY}, groups=["user"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    assert asyncio.run(runtime._get_context_actions("documento", "form")) == []

    # create/update ma non delete: solo Salva.
    runtime = _runtime({"documento": READ_WRITE}, groups=["operator"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    for record in records:
        record.groups = ["operator"]
    buttons = asyncio.run(runtime._get_context_actions("documento", "form"))
    assert [b["rec_name"] for b in buttons] == ["save_documento"]

    # CRUD completo: entrambi.
    runtime = _runtime(
        {"documento": {**READ_WRITE, "delete": True}}, groups=["operator"]
    )
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    buttons = asyncio.run(runtime._get_context_actions("documento", "form"))
    assert [b["rec_name"] for b in buttons] == [
        "save_documento",
        "delete_documento",
    ]


def test_copy_button_requires_create():
    records = [
        _action(
            rec_name="copy_documento",
            groups=["operator"],
            write_access=False,
            context_button_mode=["form"],
            action_type="copy",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Duplica",
            button_icon="",
            modal=False,
        ),
    ]

    class FakeActionModel:
        async def find(self, domain=None, sort=None, limit=0, **kwargs):
            return records

    update_only = {**NO_ACCESS, "read": True, "update": True}
    runtime = _runtime({"documento": update_only}, groups=["operator"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    assert asyncio.run(runtime._get_context_actions("documento", "form")) == []

    runtime = _runtime({"documento": READ_WRITE}, groups=["operator"])
    runtime.service.env = SimpleNamespace(get=lambda name: FakeActionModel())
    buttons = asyncio.run(runtime._get_context_actions("documento", "form"))
    assert [b["rec_name"] for b in buttons] == ["copy_documento"]


# --- gate CRUD su delete/copy (non passano da Service.upsert) ---


class _FakeRecord(SimpleNamespace):
    def get_dict(self):
        return dict(self.__dict__)


class _FakeDataModel:
    def __init__(self, records):
        self.records = records
        self.deleted = []
        self.upserted = []
        self.status = SimpleNamespace(fail=False, msg="")
        self.data_model = "documento"
        self.table_columns = {}
        self.model = SimpleNamespace(
            schema=lambda: {"components": [], "properties": {}},
            filter_keys=lambda: {},
        )

    async def by_name(self, name):
        for rec in self.records:
            if rec.rec_name == name:
                return rec
        return None

    async def set_to_delete(self, record):
        self.deleted.append(record.rec_name)
        return record

    async def copy(self, domain):
        return _FakeRecord(rec_name=f"{domain['rec_name']}_copy")

    async def upsert(self, record):
        self.upserted.append(record.rec_name)
        return record


def _crud_runtime(access, *, record_rules=None, is_sys_model=True, action=None):
    runtime = _runtime({"documento": access}, groups=["user"])
    service = runtime.service
    data_model = _FakeDataModel([_FakeRecord(rec_name="doc-1", owner_uid="altro")])

    service.env = SimpleNamespace(get=lambda name: data_model)

    async def _get_record_rules(model_name, **kwargs):
        return record_rules or []

    async def _is_sys_model(model_name):
        return is_sys_model

    service._get_record_rules = _get_record_rules
    service._is_sys_model = _is_sys_model
    service._resolve_query_json_logic_vars = lambda data: data

    act = action or _action(
        rec_name="delete_documento",
        action_type="delete",
        mode="form",
        groups=["user"],
        write_access=False,
        next_action_name="",
        builder_enabled=False,
    )

    async def _get_action_record(name):
        return act

    runtime.get_action_record = _get_action_record
    return runtime, data_model


def test_delete_denied_without_model_delete_permission():
    import pytest

    runtime, data_model = _crud_runtime(READ_WRITE)  # create/update ma non delete
    with pytest.raises(Exception) as exc:
        asyncio.run(
            runtime.handle_delete(action_name="delete_documento", rec_name="doc-1")
        )
    assert getattr(exc.value, "status_code", None) == 403
    assert data_model.deleted == []


def test_delete_allowed_with_model_delete_permission():
    access = {**READ_WRITE, "delete": True}
    runtime, data_model = _crud_runtime(access)
    res = asyncio.run(
        runtime.handle_delete(action_name="delete_documento", rec_name="doc-1")
    )
    assert res.data == {"status": "ok"}
    assert data_model.deleted == ["doc-1"]


def test_delete_denied_by_record_rules_on_non_sys_model():
    """Permesso di delete sul model, ma le record rule non coprono il record."""
    import pytest

    access = {**READ_WRITE, "delete": True}
    rules = [
        {
            "filters": {"owner_uid": "u1"},
            "actions": {
                "read": True,
                "create": True,
                "update": True,
                "delete": True,
            },
        }
    ]
    runtime, data_model = _crud_runtime(
        access, record_rules=rules, is_sys_model=False
    )
    with pytest.raises(Exception) as exc:
        asyncio.run(
            runtime.handle_delete(action_name="delete_documento", rec_name="doc-1")
        )
    assert getattr(exc.value, "status_code", None) == 403
    assert data_model.deleted == []


def _copy_action():
    return _action(
        rec_name="copy_documento",
        action_type="copy",
        mode="form",
        groups=["user"],
        write_access=False,
        next_action_name="",
        builder_enabled=False,
    )


def test_copy_denied_without_model_create_permission():
    import pytest

    runtime, data_model = _crud_runtime(READ_ONLY, action=_copy_action())
    with pytest.raises(Exception) as exc:
        asyncio.run(
            runtime.handle_post(
                action_name="copy_documento",
                data={"rec_name": "doc-1"},
                rec_name="doc-1",
            )
        )
    assert getattr(exc.value, "status_code", None) == 403
    assert data_model.upserted == []


def test_copy_allowed_with_model_create_permission():
    runtime, data_model = _crud_runtime(READ_WRITE, action=_copy_action())
    res = asyncio.run(
        runtime.handle_post(
            action_name="copy_documento",
            data={"rec_name": "doc-1"},
            rec_name="doc-1",
        )
    )
    assert data_model.upserted == ["doc-1_copy"]
    assert res.mode == "form"


def test_save_response_carries_context_actions():
    """Le response di scrittura devono portare i pulsanti: `[]` sul client
    significa "nessun pulsante calcolato", non "nessun pulsante permesso"."""
    buttons = [
        _action(
            rec_name="submit_documento",
            groups=["operator"],
            write_access=False,
            context_button_mode=["form"],
            action_type="save",
            component_type="",
            no_public_user=False,
            action_root_path="/action",
            title="Salva",
            button_icon="",
            modal=False,
        ),
    ]

    class FakeActionModel:
        async def find(self, domain=None, sort=None, limit=0, **kwargs):
            return buttons

    save_action = _action(
        rec_name="submit_documento",
        action_type="save",
        mode="form",
        groups=["operator"],
        write_access=False,
        component_type="",
        next_action_name="",
        builder_enabled=False,
    )
    runtime = _runtime({"documento": READ_WRITE}, groups=["operator"])
    service = runtime.service
    service.env = SimpleNamespace(get=lambda name: FakeActionModel())

    async def _record_rules(model_name, **kwargs):
        return []

    async def _is_sys(model_name):
        return True

    service._get_record_rules = _record_rules
    service._is_sys_model = _is_sys
    service._resolve_query_json_logic_vars = lambda data: data

    async def _get_action_record(name):
        return save_action

    async def _upsert(model, payload, rec_name="", **kwargs):
        return SimpleNamespace(
            content=ResponseObjectData(mode="form", model=model, data=payload)
        )

    runtime.get_action_record = _get_action_record
    service.upsert = _upsert

    res = asyncio.run(
        runtime.handle_post(
            action_name="submit_documento",
            data={"rec_name": "doc-1"},
            rec_name="doc-1",
        )
    )

    assert [b["rec_name"] for b in res.context_actions] == ["submit_documento"]
    # flag autoritativi anche sulla response di scrittura (prima default True)
    assert res.editable is True
    assert res.can_create is True


def test_save_response_flags_reflect_permissions():
    """Salvataggio consentito (create) ma senza update: il form che torna
    non deve dichiararsi editabile."""
    create_only = {**NO_ACCESS, "read": True, "create": True}

    class FakeActionModel:
        async def find(self, domain=None, sort=None, limit=0, **kwargs):
            return []

    save_action = _action(
        rec_name="submit_documento",
        action_type="save",
        mode="form",
        groups=["operator"],
        write_access=False,
        component_type="",
        next_action_name="",
        builder_enabled=False,
    )
    runtime = _runtime({"documento": create_only}, groups=["operator"])
    service = runtime.service
    service.env = SimpleNamespace(get=lambda name: FakeActionModel())

    async def _record_rules(model_name, **kwargs):
        return []

    async def _is_sys(model_name):
        return True

    async def _get_action_record(name):
        return save_action

    async def _upsert(model, payload, rec_name="", **kwargs):
        return SimpleNamespace(
            content=ResponseObjectData(mode="form", model=model, data=payload)
        )

    service._get_record_rules = _record_rules
    service._is_sys_model = _is_sys
    service._resolve_query_json_logic_vars = lambda data: data
    service.upsert = _upsert
    runtime.get_action_record = _get_action_record

    res = asyncio.run(
        runtime.handle_post(
            action_name="submit_documento",
            data={"rec_name": "doc-1"},
            rec_name="doc-1",
        )
    )

    assert res.can_create is True
    assert res.editable is False
