import asyncio
import types

from app.services.service import Service
from app.services.common import ResponseObjectData


class DummyModel:
    def __init__(self, name: str, rows=None, count_value: int = 0):
        self._name = name
        self._rows = rows or []
        self._count_value = count_value
        self.last_count_domain = None

    def str_name(self) -> str:
        return self._name

    async def find(self, domain, sort="list_order:asc,rec_name:asc", limit=0):
        return self._rows

    async def count(self, domain):
        self.last_count_domain = domain
        return self._count_value

    async def by_name(self, name: str):
        return {}

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
        return self._rows_by_name.get(name)


class DummyComponentModel(DummyModel):
    def __init__(self, rows_by_name=None):
        super().__init__("component")
        self._rows_by_name = rows_by_name or {}

    async def by_name(self, name: str):
        return self._rows_by_name.get(name, {})


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
    env = DummyEnv(is_admin=True, uid="plain.user", admins=["admin.user"])
    service = Service(env)

    res = asyncio.run(service.service_get_menu(parent="root"))

    assert res.mode == "menu"
    assert res.data == [{}]
    assert res.query == {"admin": True, "parent": "root"}


def test_service_get_menu_uses_settings_admins_membership():
    env = DummyEnv(
        is_admin=False,
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
    service = Service(env)

    async def fake_get_basic_menu_list(self, parent=""):
        return [{"model": "orders", "menu_group": "grp1", "label": "Group 1"}]

    async def fake_find_base(self, model, query, sort="list_order:asc,rec_name:asc", limit=0):
        and_items = query.get("$and", []) if isinstance(query, dict) else []
        for item in and_items:
            if isinstance(item, dict) and item.get("action_type") == "menu":
                return [
                    {
                        "model": "orders",
                        "button_icon": "it-folder",
                        "action_type": "menu",
                        "action_root_path": "/action",
                        "rec_name": "open_orders",
                        "title": "Open Orders",
                        "mode": "",
                    }
                ]
            if (
                isinstance(item, dict)
                and isinstance(item.get("action_type"), dict)
                and "window" in item.get("action_type", {}).get("$in", [])
            ):
                return [
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
                ]
        return []

    service._get_basic_menu_list = types.MethodType(fake_get_basic_menu_list, service)
    service._find_base = types.MethodType(fake_find_base, service)

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
            {
                "model": "orders",
                "button_icon": "it-list",
                "action_type": "window",
                "action_root_path": "/action",
                "rec_name": "orders_list",
                "title": "Orders List",
                "mode": "list",
                "list_query": {"owner": "_user_user_uid"},
            },
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
    assert action["rec_name"] == "form_form_ordine"


def test_action_get_exposes_submit_and_abandon_sequence():
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
    assert res.fields["abandon_action_name"] == "list_ordine"
    assert res.fields["action_sequence"] == {
        "current_action": "form_form_ordine",
        "submit_action": "submit_ordine",
        "submit_next_action": "list_ordine",
        "abandon_action": "list_ordine",
    }


def test_form_action_context_actions_include_implicit_save_and_cancel():
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
            return [rows_by_name["list_action"]]
        return [
            rows_by_name["submit_action"],
            rows_by_name["copy_action"],
            rows_by_name["list_action"],
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
    assert res.fields["abandon_action_name"] == "list_action"
    context_by_name = {item["rec_name"]: item for item in res.context_actions}
    assert {"submit_action", "copy_action", "cancel"} <= set(context_by_name)
    assert context_by_name["submit_action"]["action_type"] == "save"
    assert context_by_name["submit_action"]["url_action"] == (
        "/action/submit_action/submit_action"
    )
    assert context_by_name["cancel"]["url_action"] == "/action/list_action"


def test_form_action_hides_cancel_when_component_no_cancel_is_enabled():
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

    assert res.fields["abandon_action_name"] == ""
    assert res.fields["action_sequence"]["abandon_action"] == ""
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
            "list_order": "",
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
                "list_order": "name:asc",
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
            "list_order": "created_at:desc",
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
                "list_order": "name:asc",
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
                "list_order": "name:asc",
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

    async def fake_upsert(model_name, data, rec_name="", data_value=None, trnf_config=None, fields_parser=None):
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

    async def fake_upsert(model_name, data, rec_name="", data_value=None, trnf_config=None, fields_parser=None):
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
