from types import SimpleNamespace

from app.services.common import make_response_object


class _FakeSchemaModel:
    def schema(self):
        return {"components": [{"key": "rec_name", "type": "textfield"}]}

    def filter_keys(self):
        return {"rec_name": "rec_name"}


class _FakeOzonModel:
    def __init__(self):
        self.status = SimpleNamespace(fail=False, msg="")
        self.data_model = "customer"
        self.model = _FakeSchemaModel()
        self.table_columns = {"rec_name": "Rec Name"}


class _FakeRecord:
    def __init__(self, rec_name: str):
        self.rec_name = rec_name


def test_make_response_object_list_data_does_not_require_rec_name_attr():
    model = _FakeOzonModel()
    payload = [{"rec_name": "CUST-1"}, {"rec_name": "CUST-2"}]

    resp = make_response_object(model=model, mode="list", data=payload)

    assert resp.fail is False
    assert resp.content.mode == "list"
    assert resp.content.data == payload
    assert resp.content.rec_name == ""
    assert resp.content.columns == {"rec_name": "Rec Name"}


def test_make_response_object_form_extracts_rec_name_from_dict():
    model = _FakeOzonModel()
    payload = {"rec_name": "CUST-77", "name": "Mario"}

    resp = make_response_object(model=model, mode="form", data=payload)

    assert resp.content.mode == "form"
    assert resp.content.rec_name == "CUST-77"
    assert resp.content.data["name"] == "Mario"


def test_make_response_object_form_extracts_rec_name_from_object():
    model = _FakeOzonModel()
    payload = _FakeRecord("CUST-99")

    resp = make_response_object(model=model, mode="form", data=payload)

    assert resp.content.mode == "form"
    assert resp.content.rec_name == "CUST-99"
