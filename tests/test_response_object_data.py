from app.services.common import ResponseObjectData


def test_schema_alias_preserves_api_contract_without_shadowing_base_model() -> None:
    payload = ResponseObjectData(
        mode="form",
        data={},
        schema=[{"key": "name"}],
    )

    assert payload.response_schema == [{"key": "name"}]
    assert "schema" not in ResponseObjectData.model_fields
    assert payload.model_dump()["schema"] == [{"key": "name"}]


def test_schema_can_be_set_using_internal_field_name() -> None:
    payload = ResponseObjectData(
        mode="form",
        data={},
        response_schema=[{"key": "title"}],
    )

    assert payload.model_dump()["schema"] == [{"key": "title"}]
