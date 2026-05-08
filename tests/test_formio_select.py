import asyncio

from app.services.formio import make_resource_list
from app.services.formio import get_formio_select_options


def test_make_resource_list_no_label_property_uses_label_value_shape():
    field = {"src": "url", "properties": {}}
    data = [{"label": "Fornitore A", "value": "SUP-1"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "Fornitore A", "value": "SUP-1"}]


def test_make_resource_list_no_label_property_uses_kv_shape():
    field = {"src": "url", "properties": {}}
    data = [{"k": "SUP-2", "v": "Fornitore B"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "Fornitore B", "value": "SUP-2"}]


def test_make_resource_list_label_key_missing_falls_back_without_keyerror():
    field = {"src": "url", "properties": {"label": "missing_label", "id": "id"}}
    data = [{"id": "SUP-3", "name": "Fornitore C"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "Fornitore C", "value": "SUP-3"}]


def test_make_resource_list_multi_label_joins_available_values_only():
    field = {
        "src": "url",
        "properties": {"label": "code,description", "id": "id"},
    }
    data = [{"id": "SUP-4", "code": "C-44"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "C-44", "value": "SUP-4"}]


def test_make_resource_list_resource_template_path_error_falls_back():
    field = {
        "key": "supplier",
        "src": "resource",
        "multi": True,
        "template": "<span>{{ item.code.value }}</span>",
        "properties": {"label": "name", "id": "id"},
    }
    data = [{"id": "SUP-8", "code": "C-8", "name": "Fornitore H"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "Fornitore H", "value": "SUP-8"}]


def test_make_resource_list_fallbacks_to_field_key_when_no_id_label_props():
    field = {"key": "lista_rda", "src": "url", "properties": {}}
    data = [{"lista_rda": "RDA-001"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "RDA-001", "value": "RDA-001"}]


def test_make_resource_list_uses_properties_value_as_value_key():
    field = {
        "key": "supplier",
        "src": "url",
        "properties": {"label": "descrizione", "value": "codice"},
    }
    data = [{"codice": "SUP-10", "descrizione": "Fornitore X"}]

    out = make_resource_list(field, data)

    assert out == [{"label": "Fornitore X", "value": "SUP-10"}]


def test_get_formio_select_options_missing_field_returns_empty_list():
    class FakeModel:
        def select_fields(self):
            return {}

    class FakeEnvEntry:
        model = FakeModel()

    class FakeEnv:
        def get(self, _):
            return FakeEnvEntry()

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

    class FakeCliSession:
        def __init__(self):
            self.service = FakeService()

    out = asyncio.run(
        get_formio_select_options(
            FakeCliSession(),
            curr_model="customer",
            field_key="missing",
        )
    )

    assert out == []


def test_get_formio_select_options_parses_string_field_definition():
    class FakeModel:
        def select_fields(self):
            return {
                "supplier": '{"src":"values","properties":{"label":"label",'
                '"id":"value"}}'
            }

        def select_options(self, _):
            return [{"label": "Fornitore A", "value": "SUP-1"}]

    class FakeEnvEntry:
        model = FakeModel()

    class FakeEnv:
        def get(self, _):
            return FakeEnvEntry()

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

    out = asyncio.run(
        get_formio_select_options(
            FakeService(),
            curr_model="customer",
            field_key="supplier",
        )
    )

    assert out == [{"label": "Fornitore A", "value": "SUP-1"}]


def test_get_formio_select_options_local_url_missing_model_returns_empty():
    class FakeModel:
        def select_fields(self):
            return {
                "supplier": {
                    "src": "url",
                    "url": "/api/local/suppliers",
                    "properties": {},
                }
            }

    class FakeEnvEntry:
        model = FakeModel()

    class FakeEnv:
        def get(self, _):
            return FakeEnvEntry()

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

        async def get_distinct(self, *_args, **_kwargs):
            raise AssertionError("get_distinct should not be called")

    class FakeCliSession:
        def __init__(self):
            self.service = FakeService()

    out = asyncio.run(
        get_formio_select_options(
            FakeCliSession(),
            curr_model="customer",
            field_key="supplier",
        )
    )

    assert out == []


def test_get_formio_select_options_parses_string_select_fields():
    class FakeModel:
        def select_fields(self):
            return (
                '{"supplier":{"src":"values","properties":{"label":"label",'
                '"id":"value"}}}'
            )

        def select_options(self, _):
            return [{"label": "Fornitore A", "value": "SUP-1"}]

    class FakeEnvEntry:
        model = FakeModel()

    class FakeEnv:
        def get(self, _):
            return FakeEnvEntry()

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

    out = asyncio.run(
        get_formio_select_options(
            FakeService(),
            curr_model="customer",
            field_key="supplier",
        )
    )

    assert out == [{"label": "Fornitore A", "value": "SUP-1"}]


def test_get_formio_select_options_invalid_select_fields_type_returns_empty():
    class FakeModel:
        def select_fields(self):
            return "not-json"

        def select_options(self, _):
            raise AssertionError("select_options should not be called")

    class FakeEnvEntry:
        model = FakeModel()

    class FakeEnv:
        def get(self, _):
            return FakeEnvEntry()

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

    class FakeCliSession:
        def __init__(self):
            self.service = FakeService()

    out = asyncio.run(
        get_formio_select_options(
            FakeCliSession(),
            curr_model="customer",
            field_key="supplier",
        )
    )

    assert out == []


def test_get_formio_select_options_internal_error_returns_empty():
    class FakeEnv:
        def get(self, _):
            raise RuntimeError("model not available")

    class FakeService:
        def __init__(self):
            self.env = FakeEnv()

    class FakeCliSession:
        def __init__(self):
            self.service = FakeService()

    out = asyncio.run(
        get_formio_select_options(
            FakeCliSession(),
            curr_model="customer",
            field_key="supplier",
        )
    )

    assert out == []
