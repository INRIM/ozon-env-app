import asyncio
from types import SimpleNamespace

import pytest

from app.services import remote_service
from app.services.components.selectComponentService import (
    build_select_response,
    parse_remote_select_response,
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
        FakeClient.calls.append({"url": url, "headers": dict(headers)})
        return FakeResponse(self._payload, FakeClient.status_code)


@pytest.fixture(autouse=True)
def fake_http(monkeypatch):
    FakeClient.calls = []
    FakeClient.status_code = 200
    FakeClient.payload = {"result": {"select_list": []}}
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


def test_gateway_contract_returns_label_value_without_heuristics(monkeypatch):
    _patch_settings(monkeypatch, _settings(hosts=["ext_gateway"]))
    FakeClient.payload = {
        "result": {
            "select_list": [
                {"label": "Stanza 1", "value": "R1"},
                {"label": "Stanza 2", "value": "R2"},
            ]
        }
    }

    out = asyncio.run(
        remote_service._fetch_remote_data(
            url="http://ext_gateway:8080/people/rooms"
        )
    )

    assert out == [
        {"label": "Stanza 1", "value": "R1"},
        {"label": "Stanza 2", "value": "R2"},
    ]


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


def test_parse_remote_select_response_rejects_non_contract():
    assert parse_remote_select_response([{"label": "a", "value": 1}]) is None
    assert parse_remote_select_response({"result": ["a"]}) is None


def test_build_select_response_round_trips():
    resp = build_select_response([{"label": "Stanza 1", "value": "R1"}])

    assert parse_remote_select_response(resp.model_dump()) == [
        {"label": "Stanza 1", "value": "R1"}
    ]


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


@pytest.mark.parametrize(
    "properties",
    [
        {},
        # Component legacy ripuntato sul gateway: properties ancora sui
        # campi della API esterna, assenti nelle righe del contratto.
        {"label": "name", "id": "id"},
    ],
)
def test_gateway_rows_resolved_by_make_resource_list(monkeypatch, properties):
    """Percorso completo: fetch col contratto gateway -> make_resource_list
    (formio) deve restituire esattamente label/value del gateway."""
    from app.services.formio import make_resource_list

    _patch_settings(monkeypatch, _settings(hosts=["api-gateway"]))
    FakeClient.payload = build_select_response(
        [
            {"label": "Ap000b (Ap0)", "value": 523},
            {"label": "Mario Rossi", "value": "mrossi"},
        ]
    ).model_dump()

    rows = asyncio.run(
        remote_service._fetch_remote_data(
            url="http://api-gateway:8080/people/rooms"
        )
    )
    field = {
        "key": "stanza",
        "src": "url",
        "url": "http://api-gateway:8080",
        "properties": properties,
    }

    assert make_resource_list(field, rows) == [
        {"label": "Ap000b (Ap0)", "value": 523},
        {"label": "Mario Rossi", "value": "mrossi"},
    ]
