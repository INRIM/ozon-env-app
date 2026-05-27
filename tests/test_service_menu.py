import asyncio
import types

from app.services.service import Service
from app.services.common import ResponseObjectData


class DummyRecord:
    def __init__(self, data=None):
        self._data = dict(data or {})
        for key, value in self._data.items():
            setattr(self, key, value)

    def __getattr__(self, item):
        return ""

    def model_dump(self):
        return dict(self._data)

    def get_dict(self):
        return dict(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)


def _make_dummy_record(data):
    if isinstance(data, DummyRecord) or data is None:
        return data
    if isinstance(data, dict):
        return DummyRecord(data)
    return data


class DummyModel:
    def __init__(self, name: str, rows=None, count_value: int = 0):
        self._name = name
        self._rows = rows or []
        self._count_value = count_value
        self.last_count_domain = None
        self.last_load_domain = None
        self.status = types.SimpleNamespace(fail=False, msg="")
        self.data_model = name
        self.table_columns = {}
        self.model = types.SimpleNamespace(
            schema=lambda: {"components": []},
            filter_keys=lambda: [],
        )

    def str_name(self) -> str:
        return self._name

    async def find(self, domain, sort="list_order:asc,rec_name:asc", limit=0):
        return [_make_dummy_record(row) for row in self._rows]

    async def count(self, domain):
        self.last_count_domain = domain
        return self._count_value

    async def by_name(self, name: str):
        return None

    async def load(self, domain):
        self.last_load_domain = domain
        if not self._rows:
            return None
        return _make_dummy_record(self._rows[0])

    def get_domain(self, query):
        return {"wrapped": query}


class DummyEnv:
    def __init__(
        self,
        app_code="demo",
        is_admin=True,
        uid="admin.user",
        admins=None,
        models=None,
    ):
        self.user_session = types.SimpleNamespace(
            app_code=app_code,
            is_admin=is_admin,
            uid=uid,
            user={"uid": uid},
        )
        self.orm = types.SimpleNamespace(
            app_settings=types.SimpleNamespace(
                module_name="demo",
                version="1.0.0",
                logo_img_url="",
                admins=list(admins or []),
            )
        )
        self._models = models or {}

    def get(self, model_name: str):
        return self._models[model_name]


class DummyActionModel(DummyModel):
    def __init__(self, rows_by_name=None, rows=None):
        super().__init__("action", rows=rows or [])
        self._rows_by_name = rows_by_name or {}

    async def by_name(self, name: str):
        return _make_dummy_record(self._rows_by_name.get(name))


class DummyComponentModel(DummyModel):
    def __init__(self, rows_by_name=None):
        super().__init__("component")
        self._rows_by_name = rows_by_name or {}

    async def by_name(self, name: str):
        return _make_dummy_record(self._rows_by_name.get(name))


class DashboardService(Service):
    def __init__(self, env, menu_list, menu_rows=None, window_rows=None):
        super().__init__(env)
        self._dashboard_menu_list = list(menu_list)
        self._dashboard_menu_rows = list(menu_rows or [])
        self._dashboard_window_rows = list(window_rows or [])

    async def _get_basic_menu_list(self, parent=""):
        return list(self._dashboard_menu_list)

    async def _find_base(
        self,
        model,
        query,
        sort="list_order:asc,rec_name:asc",
        limit=0,
    ):
        if model.str_name() != "action":
            return await super()._find_base(model, query, sort=sort, limit=limit)

        and_items = query.get("$and", []) if isinstance(query, dict) else []
        for item in and_items:
            if isinstance(item, dict) and item.get("action_type") == "menu":
                return [_make_dummy_record(row) for row in self._dashboard_menu_rows]
            if (
                isinstance(item, dict)
                and isinstance(item.get("action_type"), dict)
                and "window" in item.get("action_type", {}).get("$in", [])
            ):
                return [
                    _make_dummy_record(row) for row in self._dashboard_window_rows
                ]
        return []


class MenuGroupDashboardService(Service):
    def __init__(self, env, menu_groups=None, action_rows=None):
        super().__init__(env)
        self._dashboard_menu_groups = list(menu_groups or [])
        self._dashboard_action_rows = list(action_rows or [])

    async def _find_base(
        self,
        model,
        query,
        sort="list_order:asc,rec_name:asc",
        limit=0,
    ):
        model_name = model.str_name()
        if model_name == "menu_group":
            return [_make_dummy_record(row) for row in self._dashboard_menu_groups]
        if model_name == "action":
            return [_make_dummy_record(row) for row in self._dashboard_action_rows]
        return await super()._find_base(model, query, sort=sort, limit=limit)


def test_default_query_for_menu_group_adds_expected_filters():
    env = DummyEnv()
    service = Service(env)
    model = DummyModel("menu_group")

    query = asyncio.run(
        service._default_query(
            model,
            {"$and": [{"admin": True}]},
            parent="root",
            model_type="layout",
        )
    )

    assert query["deleted"] == 0
    assert query["active"] is True
    assert query["parent"] == {"$eq": "root"}
    assert query["type"] == {"$eq": "layout"}
    assert query["$or"][0]["apps"]["$in"] == ["demo"]


def test_service_get_menu_non_admin_returns_empty_group():
    env = DummyEnv(is_admin=False, uid="plain.user", admins=["admin.user"])
    service = Service(env)

    res = asyncio.run(service.service_get_menu(parent="root"))

    assert res.mode == "menu"
    assert res.data == [{}]
    assert res.query == {"admin": True, "parent": "root"}


def test_service_get_menu_uses_session_is_admin():
    env = DummyEnv(
        is_admin=True,
        uid="admin.user",
        admins=["admin.user"],
        models={
            "menu_group": DummyModel(
                "menu_group",
                rows=[{"rec_name": "root", "label": "Admin", "admin": True}],
            ),
            "action": DummyModel(
                "action",
                rows=[
                    {
                        "rec_name": "settings",
                        "title": "Settings",
                        "button_icon": "it-settings",
                        "action_type": "menu",
                        "action_root_path": "/action",
                        "builder_enabled": False,
                        "model": "settings",
                    }
                ],
            ),
        },
    )
    service = Service(env)

    res = asyncio.run(service.service_get_menu())

    assert res.mode == "menu"
    assert res.data == [
        {
            "Admin": [
                {
                    "model": "settings",
                    "key": "settings",
                    "type": "button",
                    "label": "Settings",
                    "leftIcon": "it-settings",
                    "btn_action_type": None,
                    "action_type": "menu",
                    "url_action": "/action/settings",
                    "builder": False,
                }
            ]
        }
    ]


def test_service_load_uses_explicit_model_name():
    ordine_model = DummyModel(
        "ordine",
        rows=[{"rec_name": "ordine_1", "title": "Ordine 1"}],
    )
    env = DummyEnv(models={"ordine": ordine_model})
    service = Service(env)

    res = asyncio.run(service.load("ordine", {"rec_name": "ordine_1"}))

    assert ordine_model.last_load_domain == {"rec_name": "ordine_1"}
    assert res.content.model == "ordine"
    assert res.content.data.rec_name == "ordine_1"


def test_service_get_dashboard_builds_card_payload():
    action_model = DummyModel("action")
    orders_model = DummyModel("orders", count_value=7)
    env = DummyEnv(
        models={
            "action": action_model,
            "menu_group": DummyModel("menu_group"),
            "orders": orders_model,
        }
    )
    service = DashboardService(
        env,
        menu_list=[{"model": "orders", "menu_group": "grp1", "label": "Group 1"}],
        menu_rows=[
            {
                "model": "orders",
                "button_icon": "it-folder",
                "action_type": "menu",
                "action_root_path": "/action",
                "rec_name": "open_orders",
                "title": "Open Orders",
                "mode": "",
                "sys": False,
            }
        ],
        window_rows=[
            {
                "model": "orders",
                "button_icon": "it-list",
                "action_type": "window",
                "action_root_path": "/action",
                "rec_name": "orders_list",
                "title": "Orders List",
                "mode": "list",
                "list_query": "{}",
            }
        ],
    )

    res = asyncio.run(service.service_get_dashboard(parent="root"))

    assert res.mode == "card"
    assert res.model == "action"
    assert res.query == {"parent": "root"}
    assert len(res.data) == 1
    card = res.data[0]
    assert card["group_id"] == "grp1"
    assert len(card["buttons"]) == 2
    assert {b["label"] for b in card["buttons"]} == {"Open Orders", "Orders List"}
    list_button = [b for b in card["buttons"] if b["label"] == "Orders List"][0]
    assert list_button["number"] == 7


def test_service_get_dashboard_skips_admin_group_with_only_system_menus():
    action_model = DummyModel("action")
    orders_model = DummyModel("orders", count_value=3)
    env = DummyEnv(
        models={
            "action": action_model,
            "menu_group": DummyModel("menu_group"),
            "orders": orders_model,
        }
    )
    service = DashboardService(
        env,
        menu_list=[
            {
                "model": "orders",
                "menu_group": "admin_group",
                "label": "Admin Group",
            }
        ],
        menu_rows=[
            {
                "model": "orders",
                "button_icon": "it-settings",
                "action_type": "menu",
                "action_root_path": "/action",
                "rec_name": "admin_orders",
                "title": "Admin Orders",
                "mode": "",
                "sys": True,
            }
        ],
        window_rows=[
            {
                "model": "orders",
                "button_icon": "it-list",
                "action_type": "window",
                "action_root_path": "/action",
                "rec_name": "orders_list",
                "title": "Orders List",
                "mode": "list",
                "list_query": "{}",
            }
        ],
    )

    res = asyncio.run(service.service_get_dashboard(parent="root"))

    assert res.mode == "card"
    assert res.data == []


def test_service_get_dashboard_skips_admin_only_menu_group():
    action_model = DummyModel("action")
    orders_model = DummyModel("orders", count_value=3)
    env = DummyEnv(
        models={
            "action": action_model,
            "menu_group": DummyModel("menu_group"),
            "orders": orders_model,
        }
    )
    service = MenuGroupDashboardService(
        env,
        menu_groups=[
            {
                "rec_name": "admin_group",
                "label": "Admin Group",
                "admin": True,
            }
        ],
        action_rows=[
            {
                "model": "orders",
                "button_icon": "it-settings",
                "action_type": "menu",
                "action_root_path": "/action",
                "rec_name": "admin_orders",
                "title": "Admin Orders",
                "mode": "",
                "sys": False,
            }
        ],
    )

    res = asyncio.run(service.service_get_dashboard(parent="root"))

    assert res.mode == "card"
    assert res.data == []


def test_make_menu_item_count_uses_model_domain_and_user_placeholders():
    model = DummyModel("orders", count_value=11)
    env = DummyEnv(
        models={
            "orders": model,
        }
    )
    # session field usato da placeholder `_user_...`
    env.user_session.user_uid = "U-100"
    service = Service(env)

    item = asyncio.run(
        service._make_menu_item(
            {"model": "orders"},
            _make_dummy_record({
                "model": "orders",
                "button_icon": "it-list",
                "action_type": "window",
                "action_root_path": "/action",
                "rec_name": "orders_list",
                "title": "Orders List",
                "mode": "list",
                "list_query": {"owner": "_user_user_uid"},
            }),
        )
    )

    assert item["number"] == 11
    assert model.last_count_domain == {
        "wrapped": {"owner": "U-100", "deleted": 0, "active": True}
    }


def test_get_action_record_uses_action_rec_name():
    action_model = DummyActionModel(
        rows_by_name={
            "form_form_ordine": {
                "rec_name": "form_form_ordine",
                "mode": "form",
                "model": "ordine",
            }
        }
    )
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    action = asyncio.run(service._get_action_record("form_form_ordine"))

    assert action is not None
    assert action.rec_name == "form_form_ordine"


def test_action_get_exposes_submit_sequence():
    rows_by_name = {
        "form_form_ordine": {
            "rec_name": "form_form_ordine",
            "mode": "form",
            "model": "ordine",
            "action_type": "window",
            "next_action_name": "submit_ordine",
            "type": "data",
            "component_type": "",
        },
        "submit_ordine": {
            "rec_name": "submit_ordine",
            "mode": "form",
            "model": "ordine",
            "action_type": "save",
            "next_action_name": "list_ordine",
            "type": "data",
            "component_type": "",
        },
        "list_ordine": {
            "rec_name": "list_ordine",
            "mode": "list",
            "model": "ordine",
            "action_type": "menu",
            "next_action_name": "form_form_ordine",
            "type": "data",
            "component_type": "",
        },
    }
    action_model = DummyActionModel(
        rows_by_name=rows_by_name,
        rows=[rows_by_name["list_ordine"]],
    )
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    async def fake_compo_by_name(model: str, name: str):
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=name,
                data={"rec_name": ""},
                fields={},
            )
        )

    service.compo_by_name = fake_compo_by_name

    res = asyncio.run(service.service_handle_action_get("form_form_ordine"))

    assert res.mode == "form"
    assert res.fields["submit_action_name"] == "submit_ordine"
    assert res.fields["next_action_name"] == "submit_ordine"
    assert res.fields["action_sequence"] == {
        "current_action": "form_form_ordine",
        "submit_action": "submit_ordine",
        "submit_next_action": "list_ordine",
    }
    assert "abandon_action_name" not in res.fields


def test_action_get_does_not_expose_abandon_fields():
    rows_by_name = {
        "form_form_ordine": {
            "rec_name": "form_form_ordine",
            "mode": "form",
            "model": "ordine",
            "action_type": "window",
            "next_action_name": "submit_ordine",
            "type": "data",
            "component_type": "",
        },
        "submit_ordine": {
            "rec_name": "submit_ordine",
            "mode": "form",
            "model": "ordine",
            "action_type": "save",
            "next_action_name": "list_ordine",
            "type": "data",
            "component_type": "",
        },
        "list_alt_ordine": {
            "rec_name": "list_alt_ordine",
            "mode": "list",
            "model": "ordine",
            "action_type": "menu",
            "next_action_name": "other_form",
            "type": "data",
            "component_type": "",
        },
        "list_ordine": {
            "rec_name": "list_ordine",
            "mode": "list",
            "model": "ordine",
            "action_type": "window",
            "next_action_name": "form_form_ordine",
            "type": "data",
            "component_type": "",
        },
    }
    action_model = DummyActionModel(
        rows_by_name=rows_by_name,
        rows=[rows_by_name["list_alt_ordine"], rows_by_name["list_ordine"]],
    )
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    async def fake_compo_by_name(model: str, name: str):
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=name,
                data={"rec_name": ""},
                fields={},
            )
        )

    service.compo_by_name = fake_compo_by_name

    res = asyncio.run(service.service_handle_action_get("form_form_ordine"))

    assert "abandon_action_name" not in res.fields
    assert "abandon_action" not in res.fields["action_sequence"]


def test_form_action_context_actions_hide_empty_context_button_mode():
    rows_by_name = {
        "form_form_action": {
            "rec_name": "form_form_action",
            "mode": "form",
            "model": "action",
            "action_type": "window",
            "next_action_name": "submit_action",
            "type": "data",
            "component_type": "",
        },
        "submit_action": {
            "rec_name": "submit_action",
            "mode": "form",
            "model": "action",
            "action_type": "save",
            "next_action_name": "list_action",
            "context_button_mode": [],
            "title": "Salva",
            "button_icon": "it-check",
            "type": "data",
            "component_type": "",
        },
        "secondary_save_action": {
            "rec_name": "secondary_save_action",
            "mode": "form",
            "model": "action",
            "action_type": "save",
            "next_action_name": "list_action",
            "context_button_mode": ["form"],
            "title": "Salva Esplicito",
            "button_icon": "it-check",
            "type": "data",
            "component_type": "",
        },
        "copy_action": {
            "rec_name": "copy_action",
            "mode": "form",
            "model": "action",
            "action_type": "copy",
            "next_action_name": "form_form_action",
            "context_button_mode": ["form"],
            "title": "Duplica",
            "button_icon": "it-copy",
            "type": "data",
            "component_type": "",
        },
        "list_action": {
            "rec_name": "list_action",
            "mode": "list",
            "model": "action",
            "action_type": "window",
            "next_action_name": "form_form_action",
            "context_button_mode": ["list"],
            "title": "Azioni",
            "button_icon": "it-list",
            "type": "data",
            "component_type": "",
        },
    }
    action_model = DummyActionModel(
        rows_by_name=rows_by_name,
        rows=[
            rows_by_name["submit_action"],
            rows_by_name["secondary_save_action"],
            rows_by_name["copy_action"],
            rows_by_name["list_action"],
        ],
    )
    async def fake_find(domain, sort="list_order:asc,rec_name:asc", limit=0):
        and_items = domain.get("$and", []) if isinstance(domain, dict) else []
        if any(
            isinstance(item, dict) and item.get("mode") == "list"
            for item in and_items
        ):
            return [_make_dummy_record(rows_by_name["list_action"])]
        return [
            _make_dummy_record(rows_by_name["submit_action"]),
            _make_dummy_record(rows_by_name["secondary_save_action"]),
            _make_dummy_record(rows_by_name["copy_action"]),
            _make_dummy_record(rows_by_name["list_action"]),
        ]

    action_model.find = fake_find
    component_model = DummyComponentModel(
        rows_by_name={
            "action": {
                "rec_name": "action",
                "title": "Action Model",
                "components": [{"key": "title"}],
                "no_cancel": "0",
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    res = asyncio.run(service.service_handle_action_get("form_form_action"))

    assert res.mode == "form"
    assert res.title == "Action Model"
    assert res.fields["submit_action_name"] == "submit_action"
    context_by_name = {item["rec_name"]: item for item in res.context_actions}
    assert {"secondary_save_action", "copy_action"} <= set(context_by_name)
    assert "submit_action" not in context_by_name
    assert "cancel" not in context_by_name
    assert context_by_name["secondary_save_action"]["action_type"] == "save"
    assert context_by_name["secondary_save_action"]["url_action"] == (
        "/action/secondary_save_action/secondary_save_action"
    )
    assert "abandon_action_name" not in res.fields


def test_form_action_does_not_add_implicit_cancel_button():
    rows_by_name = {
        "form_form_action": {
            "rec_name": "form_form_action",
            "mode": "form",
            "model": "action",
            "action_type": "window",
            "next_action_name": "submit_action",
            "type": "data",
            "component_type": "",
        },
        "submit_action": {
            "rec_name": "submit_action",
            "mode": "form",
            "model": "action",
            "action_type": "save",
            "next_action_name": "list_action",
            "context_button_mode": [],
            "title": "Salva",
            "button_icon": "it-check",
            "type": "data",
            "component_type": "",
        },
        "list_action": {
            "rec_name": "list_action",
            "mode": "list",
            "model": "action",
            "action_type": "window",
            "next_action_name": "form_form_action",
            "context_button_mode": ["list"],
            "title": "Azioni",
            "button_icon": "it-list",
            "type": "data",
            "component_type": "",
        },
    }
    action_model = DummyActionModel(
        rows_by_name=rows_by_name,
        rows=[rows_by_name["submit_action"], rows_by_name["list_action"]],
    )
    component_model = DummyComponentModel(
        rows_by_name={
            "action": {
                "rec_name": "action",
                "title": "Action Model",
                "components": [{"key": "title"}],
                "no_cancel": 1,
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    res = asyncio.run(service.service_handle_action_get("form_form_action"))

    assert "abandon_action_name" not in res.fields
    assert "abandon_action" not in res.fields["action_sequence"]
    assert "cancel" not in {item["rec_name"] for item in res.context_actions}


def test_list_action_uses_model_data_and_view_name_schema():
    rows_by_name = {
        "list_documento": {
            "rec_name": "list_documento",
            "mode": "list",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "window",
            "list_query": "{}",
            "listOrderString": "",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel(
        rows_by_name={
            "documento_beni_servizi": {
                "rec_name": "documento_beni_servizi",
                "components": [{"key": "beni_servizi_schema"}],
                "list_query": {"from_component": True},
                "listOrderString": "name:asc",
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"model_name": None, "query": None, "order": None}

    async def fake_list_records(model_name, query, order, skip, limit):
        called["model_name"] = model_name
        called["query"] = query
        called["order"] = order
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="list",
                model=model_name,
                data=[{"rec_name": "DOC-1"}],
                schema=[{"key": "model_schema"}],
                columns={"rec_name": "Rec Name"},
                total_count=1,
            )
        )

    service.list_records = fake_list_records

    res = asyncio.run(
        service.service_handle_action_get(
            "list_documento",
            query={"runtime": True},
        )
    )

    assert called["model_name"] == "documento"
    assert called["query"] == {
        "$and": [{"from_component": True}, {"runtime": True}]
    }
    assert called["order"] == "name:asc"
    assert res.model == "documento"
    assert res.schema == [{"key": "beni_servizi_schema"}]
    assert res.data == [{"rec_name": "DOC-1"}]
    assert "next_action_name" not in (res.fields if isinstance(res.fields, dict) else {})


def test_list_action_uses_action_query_and_order_over_component_defaults():
    rows_by_name = {
        "list_documento": {
            "rec_name": "list_documento",
            "mode": "list",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "window",
            "list_query": {"from_action": True},
            "listOrderString": "created_at:desc",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel(
        rows_by_name={
            "documento_beni_servizi": {
                "rec_name": "documento_beni_servizi",
                "components": [{"key": "beni_servizi_schema"}],
                "list_query": {"from_component": True},
                "listOrderString": "name:asc",
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"query": None, "order": None}

    async def fake_list_records(model_name, query, order, skip, limit):
        called["query"] = query
        called["order"] = order
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="list",
                model=model_name,
                data=[],
                total_count=0,
            )
        )

    service.list_records = fake_list_records

    asyncio.run(
        service.service_handle_action_get(
            "list_documento",
            query={"runtime": True},
        )
    )

    assert called["query"] == {
        "$and": [{"from_action": True}, {"runtime": True}]
    }
    assert called["order"] == "created_at:desc"


def test_list_action_ignores_non_string_action_order_and_uses_component_order():
    rows_by_name = {
        "list_documento": {
            "rec_name": "list_documento",
            "mode": "list",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "window",
            "list_query": {"from_action": True},
            "list_order": 10,
            "listOrderString": 10,
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel(
        rows_by_name={
            "documento_beni_servizi": {
                "rec_name": "documento_beni_servizi",
                "components": [{"key": "beni_servizi_schema"}],
                "listOrderString": "name:asc",
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"order": None}

    async def fake_list_records(model_name, query, order, skip, limit):
        called["order"] = order
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="list",
                model=model_name,
                data=[],
                total_count=0,
            )
        )

    service.list_records = fake_list_records

    asyncio.run(
        service.service_handle_action_get(
            "list_documento",
            query={},
        )
    )

    assert called["order"] == "name:asc"


def test_component_list_action_filters_builder_temporary_names():
    rows_by_name = {
        "list_form": {
            "rec_name": "list_form",
            "mode": "list",
            "model": "component",
            "action_type": "menu",
            "type": "component",
            "component_type": "form",
            "list_query": {},
            "listOrderString": "rec_name:asc",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel()
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"query": None}

    async def fake_list_records(model_name, query, order, skip, limit):
        called["query"] = query
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="list",
                model=model_name,
                data=[],
                total_count=0,
            )
        )

    service.list_records = fake_list_records

    asyncio.run(
        service.service_handle_action_get(
            "list_form",
            query={"runtime": True},
        )
    )

    assert called["query"] == {
        "$and": [
            {"deleted": 0},
            {"active": True},
            {"type": "form"},
            {"rec_name": {"$regex": r"^[A-Za-z][A-Za-z0-9_]*$"}},
            {"runtime": True},
        ]
    }


def test_form_action_uses_model_data_and_view_name_schema():
    rows_by_name = {
        "form_form_documento": {
            "rec_name": "form_form_documento",
            "mode": "form",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "window",
            "next_action_name": "submit_documento",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel(
        rows_by_name={
            "documento_beni_servizi": {
                "rec_name": "documento_beni_servizi",
                "components": [{"key": "beni_servizi_schema"}],
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"model_name": None, "rec_name": None}

    async def fake_load_record(model_name, rec_name):
        called["model_name"] = model_name
        called["rec_name"] = rec_name
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data={"rec_name": rec_name, "state": "draft"},
                schema=[{"key": "model_schema"}],
            )
        )

    service.load_record = fake_load_record

    res = asyncio.run(
        service.service_handle_action_get("form_form_documento", rec_name="DOC-1")
    )

    assert called["model_name"] == "documento"
    assert called["rec_name"] == "DOC-1"
    assert res.model == "documento"
    assert res.schema == [{"key": "beni_servizi_schema"}]
    assert res.data == {"rec_name": "DOC-1", "state": "draft"}


def test_post_action_with_view_name_writes_on_model():
    rows_by_name = {
        "submit_documento": {
            "rec_name": "submit_documento",
            "mode": "form",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "save",
            "next_action_name": "list_doc_beni_servizi",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    called = {"model_name": None, "rec_name": None, "data": None}

    async def fake_upsert(
        model_name,
        data,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
        sync_component_runtime=False,
        generate_component_defaults=False,
    ):
        called["model_name"] = model_name
        called["rec_name"] = rec_name
        called["data"] = data
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data=data,
            )
        )

    service.upsert = fake_upsert

    res = asyncio.run(
        service.service_handle_action_post(
            "submit_documento",
            {"rec_name": "DOC-9", "stato": "confermato"},
        )
    )

    assert called["model_name"] == "documento"
    assert called["rec_name"] == "DOC-9"
    assert res.model == "documento"


def test_builder_component_post_enables_runtime_sync():
    rows_by_name = {
        "save_edit_mode_resource": {
            "rec_name": "save_edit_mode_resource",
            "mode": "form",
            "model": "component",
            "action_type": "save",
            "builder_enabled": True,
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    called = {
        "model_name": None,
        "sync_component_runtime": None,
        "generate_component_defaults": None,
    }

    async def fake_upsert(
        model_name,
        data,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
        sync_component_runtime=False,
        generate_component_defaults=False,
    ):
        called["model_name"] = model_name
        called["sync_component_runtime"] = sync_component_runtime
        called["generate_component_defaults"] = generate_component_defaults
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data=data,
            )
        )

    service.upsert = fake_upsert

    asyncio.run(
        service.service_handle_action_post(
            "save_edit_mode_resource",
            {"rec_name": "demo_component", "type": "resource"},
        )
    )

    assert called["model_name"] == "component"
    assert called["sync_component_runtime"] is True
    assert called["generate_component_defaults"] is True


def test_non_builder_component_post_keeps_runtime_sync_disabled():
    rows_by_name = {
        "save_component_import": {
            "rec_name": "save_component_import",
            "mode": "form",
            "model": "component",
            "action_type": "save",
            "builder_enabled": False,
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    called = {
        "model_name": None,
        "sync_component_runtime": None,
        "generate_component_defaults": None,
    }

    async def fake_upsert(
        model_name,
        data,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
        sync_component_runtime=False,
        generate_component_defaults=False,
    ):
        called["model_name"] = model_name
        called["sync_component_runtime"] = sync_component_runtime
        called["generate_component_defaults"] = generate_component_defaults
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data=data,
            )
        )

    service.upsert = fake_upsert

    asyncio.run(
        service.service_handle_action_post(
            "save_component_import",
            {"rec_name": "demo_component", "type": "resource"},
        )
    )

    assert called["model_name"] == "component"
    assert called["sync_component_runtime"] is False
    assert called["generate_component_defaults"] is False


def test_form_form_doc_bene_servizi_uses_view_name_schema():
    rows_by_name = {
        "form_form_doc_bene_servizi": {
            "rec_name": "form_form_doc_bene_servizi",
            "mode": "form",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "window",
            "next_action_name": "submit_doc_bene_servizi",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    component_model = DummyComponentModel(
        rows_by_name={
            "documento_beni_servizi": {
                "rec_name": "documento_beni_servizi",
                "components": [{"key": "beni_servizi_schema"}],
            }
        }
    )
    env = DummyEnv(models={"action": action_model, "component": component_model})
    service = Service(env)

    called = {"model_name": None, "rec_name": None}

    async def fake_load_record(model_name, rec_name):
        called["model_name"] = model_name
        called["rec_name"] = rec_name
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data={"rec_name": rec_name, "stato": "bozza"},
            )
        )

    service.load_record = fake_load_record

    res = asyncio.run(
        service.service_handle_action_get(
            "form_form_doc_bene_servizi",
            rec_name="ORDINE63423",
        )
    )

    assert called["model_name"] == "documento"
    assert called["rec_name"] == "ORDINE63423"
    assert res.model == "documento"
    assert res.schema == [{"key": "beni_servizi_schema"}]
    assert res.data == {"rec_name": "ORDINE63423", "stato": "bozza"}


def test_post_form_form_doc_bene_servizi_commits_on_model():
    rows_by_name = {
        "form_form_doc_bene_servizi": {
            "rec_name": "form_form_doc_bene_servizi",
            "mode": "form",
            "model": "documento",
            "view_name": "documento_beni_servizi",
            "action_type": "save",
            "next_action_name": "list_doc_bene_servizi",
            "type": "data",
            "component_type": "",
        }
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    called = {"model_name": None, "rec_name": None, "data": None}

    async def fake_upsert(
        model_name,
        data,
        rec_name="",
        data_value=None,
        trnf_config=None,
        fields_parser=None,
        sync_component_runtime=False,
        generate_component_defaults=False,
    ):
        called["model_name"] = model_name
        called["rec_name"] = rec_name
        called["data"] = data
        return types.SimpleNamespace(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data=data,
            )
        )

    service.upsert = fake_upsert

    res = asyncio.run(
        service.service_handle_action_post(
            "form_form_doc_bene_servizi",
            {"rec_name": "ORDINE63423", "stato": "confermato"},
        )
    )

    assert called["model_name"] == "documento"
    assert called["rec_name"] == "ORDINE63423"
    assert res.model == "documento"


def test_service_get_next_action_redirect_with_and_without_rec_name():
    rows_by_name = {
        "form_form_ordine": {
            "rec_name": "form_form_ordine",
            "mode": "form",
            "model": "ordine",
            "next_action_name": "submit_ordine",
        },
        "submit_ordine": {
            "rec_name": "submit_ordine",
            "mode": "form",
            "model": "ordine",
        },
        "no_next": {
            "rec_name": "no_next",
            "mode": "form",
            "model": "ordine",
            "next_action_name": "",
        },
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    redirect_with_rec_name = asyncio.run(
        service.service_get_next_action_redirect(
            curr_action="form_form_ordine",
            rec_name="REC-1",
        )
    )
    redirect_without_rec_name = asyncio.run(
        service.service_get_next_action_redirect(
            curr_action="form_form_ordine",
            rec_name="",
        )
    )
    redirect_empty = asyncio.run(
        service.service_get_next_action_redirect(
            curr_action="no_next",
            rec_name="REC-1",
        )
    )

    assert redirect_with_rec_name == "/action/submit_ordine/REC-1"
    assert redirect_without_rec_name == "/action/submit_ordine"
    assert redirect_empty == ""


def test_service_get_next_action_redirect_returns_empty_when_next_missing():
    rows_by_name = {
        "form_form_ordine": {
            "rec_name": "form_form_ordine",
            "mode": "form",
            "model": "ordine",
            "next_action_name": "missing_next",
        },
    }
    action_model = DummyActionModel(rows_by_name=rows_by_name)
    env = DummyEnv(models={"action": action_model})
    service = Service(env)

    redirect = asyncio.run(
        service.service_get_next_action_redirect(
            curr_action="form_form_ordine",
            rec_name="REC-1",
        )
    )

    assert redirect == ""
