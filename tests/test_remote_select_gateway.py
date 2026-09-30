import asyncio
from types import SimpleNamespace

import pytest

from app.services import remote_service
from app.services.components.selectComponentService import (
    build_remote_select_request,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    """Registra l'ultima GET: serve a verificare COSA e' stato spedito."""

    calls: list[dict] = []

    status_code = 200

    def __init__(self, *args, **kwargs):
        self._payload = FakeClient.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, headers):
        FakeClient.calls.append(
            {"method": "GET", "url": url, "headers": dict(headers)}
        )
        return FakeResponse(self._payload, FakeClient.status_code)

    async def post(self, url, headers, json=None):
        FakeClient.calls.append(
            {"method": "POST", "url": url, "headers": dict(headers), "json": json}
        )
        return FakeResponse(self._payload, FakeClient.status_code)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.status_code = 200
    FakeClient.payload = []
    monkeypatch.setattr(remote_service.httpx, "AsyncClient", FakeClient)
    yield


def _settings(hosts=(), token="s3cret", header="x-ozon-s2s-token"):
    return SimpleNamespace(
        remote_select_allowed_hosts=list(hosts),
        remote_select_s2s_token=token,
        remote_select_s2s_header=header,
    )


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr(
        remote_service, "_remote_select_settings", lambda: settings
    )


def test_legacy_bare_list_still_passes_through(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["people.ininrim.it"]))
    FakeClient.payload = [{"id": "R1", "name": "Stanza 1"}]

    out = asyncio.run(
        remote_service._fetch_remote_data(
            url="https://people.ininrim.it/api/get_rooms"
        )
    )

    assert out == [{"id": "R1", "name": "Stanza 1"}]


def test_s2s_token_sent_only_to_allowlisted_host(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["ext_gateway"]))

    asyncio.run(
        remote_service._fetch_remote_data(url="http://ext_gateway:8080/x")
    )

    assert FakeClient.calls[0]["headers"]["x-ozon-s2s-token"] == "s3cret"


def test_s2s_token_not_sent_when_allowlist_empty(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=[]))

    asyncio.run(
        remote_service._fetch_remote_data(url="https://people.ininrim.it/x")
    )

    # Allowlist vuota = nessun controllo sul fetch (default non-breaking
    # per i deploy che oggi non impostano la var): la GET deve partire,
    # ma senza token.
    assert len(FakeClient.calls) == 1
    assert "x-ozon-s2s-token" not in FakeClient.calls[0]["headers"]


def test_non_allowlisted_host_is_not_fetched(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["ext_gateway"]))

    out = asyncio.run(
        remote_service._fetch_remote_data(url="http://evil.example/x")
    )

    assert out == []
    assert FakeClient.calls == []


def test_non_http_scheme_rejected(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=[]))

    out = asyncio.run(remote_service._fetch_remote_data(url="file:///etc/passwd"))

    assert out == []
    assert FakeClient.calls == []


def test_empty_header_value_key_skips_global_params_lookup(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["ext_gateway"]))
    called: list[str] = []

    async def fake_get_global_param(service, name):
        called.append(name)
        return {}

    monkeypatch.setattr(
        remote_service, "get_global_param", fake_get_global_param
    )

    asyncio.run(
        remote_service.remote_data_select_response(
            service=None,
            url="http://ext_gateway:8080",
            path_value="people/rooms",
            header_key="",
            header_value_key="",
        )
    )

    assert called == []
    assert FakeClient.calls[0]["url"] == "http://ext_gateway:8080/people/rooms"


def test_legacy_header_credential_still_applied(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["people.ininrim.it"]))

    async def fake_get_global_param(service, name):
        assert name == "people"
        return {"key": "api-key-value"}

    monkeypatch.setattr(
        remote_service, "get_global_param", fake_get_global_param
    )

    asyncio.run(
        remote_service.remote_data_select_response(
            service=None,
            url="https://people.ininrim.it/api/get_rooms",
            path_value="",
            header_key="x-key",
            header_value_key="people",
        )
    )

    assert FakeClient.calls[0]["headers"]["x-key"] == "api-key-value"


def test_allowlist_entry_with_port_restricts_to_that_port(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["ext_gateway:8080"]))

    ok = asyncio.run(
        remote_service._fetch_remote_data(url="http://ext_gateway:8080/x")
    )
    assert FakeClient.calls[0]["headers"]["x-ozon-s2s-token"] == "s3cret"
    assert ok == []

    out = asyncio.run(
        remote_service._fetch_remote_data(url="http://ext_gateway:9999/x")
    )
    assert out == []
    assert len(FakeClient.calls) == 1


def test_legacy_non_string_global_param_is_coerced(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["people.ininrim.it"]))

    async def fake_get_global_param(service, name):
        return ["not", "a", "string"]

    monkeypatch.setattr(
        remote_service, "get_global_param", fake_get_global_param
    )

    asyncio.run(
        remote_service.remote_data_select_response(
            service=None,
            url="https://people.ininrim.it/api/get_rooms",
            path_value="",
            header_key="x-key",
            header_value_key="people",
        )
    )

    assert isinstance(FakeClient.calls[0]["headers"]["x-key"], str)


def test_gateway_401_yields_empty_select_not_an_error(monkeypatch):
    """Modalita' di guasto piu' probabile in produzione: gateway configurato
    ma REMOTE_SELECT_ALLOWED_HOSTS vuota -> nessun token -> 401 -> select
    vuota, senza errore visibile all'utente. Qui resta almeno documentata
    (il rimedio e' in .env.example: allowlist con l'host del gateway)."""

    _patch_settings(monkeypatch, _settings(hosts=[]))
    FakeClient.status_code = 401
    FakeClient.payload = {"detail": "invalid service token"}

    out = asyncio.run(
        remote_service._fetch_remote_data(url="http://api-gateway:8080/people/rooms")
    )

    assert out == []
    assert "x-ozon-s2s-token" not in FakeClient.calls[0]["headers"]


def test_gateway_raw_rows_decoded_by_properties(monkeypatch):
    """Percorso completo: il gateway inoltra le righe grezze dell'API
    esterna, label/value li decide `properties` del component."""
    from app.services.formio import make_resource_list

    _patch_settings(monkeypatch, _settings(hosts=["api-gateway"]))
    FakeClient.payload = [
        {"id": 523, "name": "Ap000b", "parent_building_code": "Ap0"},
        # People mette false dove ci si aspetta null.
        {"id": 524, "name": "Ap001", "parent_building_code": False},
    ]

    rows = asyncio.run(
        remote_service._fetch_remote_data(
            url="http://api-gateway:8080/people/rooms"
        )
    )
    field = {
        "key": "stanza",
        "src": "url",
        "url": "http://api-gateway:8080/people/rooms",
        "properties": {"label": "name,parent_building_code", "id": "id"},
    }

    assert make_resource_list(field, rows) == [
        {"label": "Ap000b Ap0", "value": 523},
        {"label": "Ap001", "value": 524},
    ]


def test_wrapped_result_rows_unwrapped(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["api-gateway"]))
    FakeClient.payload = {"result": [{"id": 1, "name": "DG"}]}

    rows = asyncio.run(
        remote_service._fetch_remote_data(
            url="http://api-gateway:8080/people/sectors"
        )
    )

    assert rows == [{"id": 1, "name": "DG"}]


def test_single_label_key_false_falls_back():
    from app.services.formio import _normalize_label_and_value

    label, value = _normalize_label_and_value(
        {"id": 1, "sign": False, "name": "DG"}, "sign", "id"
    )

    assert label == "DG"
    assert value == 1


def test_post_sends_body_from_properties(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["api-gateway"]))
    request = build_remote_select_request(
        {"method": "post", "body": '{"workmodes": [1, 2]}'}
    )

    asyncio.run(
        remote_service.remote_data_select_response(
            service=None,
            url="http://api-gateway:8080/people/persons_by_workmodes",
            path_value="",
            header_key="",
            header_value_key="",
            method=request.method,
            body=request.body,
        )
    )

    call = FakeClient.calls[0]
    assert call["method"] == "POST"
    assert call["json"] == {"workmodes": [1, 2]}
    assert call["headers"]["x-ozon-s2s-token"] == "s3cret"


@pytest.mark.parametrize(
    "props",
    [
        {},
        {"method": "GET", "body": '{"a": 1}'},
        {"method": "DELETE"},
        # Body non JSON: niente POST con un body diverso da quello voluto.
        {"method": "POST", "body": "{not json"},
        "non-dict",
    ],
)
def test_request_defaults_to_get_without_body(props):
    request = build_remote_select_request(props)

    assert request.method == "GET"
    assert request.body is None


def test_post_body_as_object_accepted():
    request = build_remote_select_request({"method": "POST", "body": {"a": 1}})

    assert (request.method, request.body) == ("POST", {"a": 1})
