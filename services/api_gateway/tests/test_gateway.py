import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from api_gateway.app import create_app
from api_gateway.config import GatewayConfig, load_upstreams, parse_upstreams
from api_gateway.upstream import UpstreamClient

TOKEN = "s3cret-token"
AUTH = {"x-ozon-s2s-token": TOKEN}
ENV = {"PEOPLE_URL": "https://people.test", "PEOPLE_KEY": "people-key"}

RAW = {
    "upstreams": {
        "people": {
            "base_url": "${PEOPLE_URL}",
            "headers": {"x-key": "${PEOPLE_KEY}"},
            "cache_ttl": 300,
            "resources": {
                "rooms": {"path": "/api/get_rooms"},
                "sectors": {"path": "/api/getsectors/"},
                "by_workmodes": {
                    "path": "/api/get_persons_by_workmodes",
                    "allow_post": True,
                },
            },
        }
    }
}


def _config(**over):
    base = dict(
        s2s_token=TOKEN,
        s2s_header="x-ozon-s2s-token",
        config_path="unused",
        http_timeout=5.0,
    )
    base.update(over)
    return GatewayConfig(**base)


class Recorder:
    """Handler httpx: registra le richieste verso l'upstream e risponde a
    copione per path."""

    def __init__(self, routes, status_code=200):
        self.routes = routes
        self.status_code = status_code
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        payload = self.routes.get(request.url.path, [])
        return httpx.Response(self.status_code, json=payload)


def _client(routes, raw=RAW, status_code=200):
    recorder = Recorder(routes, status_code=status_code)
    upstreams = parse_upstreams(raw, ENV)
    clients = {
        code: UpstreamClient(
            code,
            up,
            timeout=5.0,
            client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        )
        for code, up in upstreams.items()
    }
    app = create_app(_config(), upstreams=upstreams, clients=clients)
    return TestClient(app), recorder


def test_get_passes_upstream_payload_through():
    # Passa TUTTO: il filtro dei campi lo fa l'app, il gateway non decide
    # a priori quali dati servono.
    rows = [
        {"id": 523, "name": "Ap000b", "parent_building_code": "Ap0", "extra": 1}
    ]
    client, rec = _client({"/api/get_rooms": rows})

    res = client.get("/people/rooms", headers=AUTH)

    assert res.status_code == 200
    # Nessuna decodifica lato gateway: label/value li risolve l'app dalle
    # properties del component.
    assert res.json() == rows
    assert str(rec.calls[0].url) == "https://people.test/api/get_rooms"
    assert rec.calls[0].headers["x-key"] == "people-key"


def test_wrapped_payload_passes_through():
    payload = {"result": [{"id": 1, "name": "DG", "sign": "DG"}]}
    client, _ = _client({"/api/getsectors/": payload})

    assert client.get("/people/sectors", headers=AUTH).json() == payload


def test_query_string_forwarded():
    client, rec = _client({"/api/get_rooms": []})

    client.get("/people/rooms?building=Ap0&q=x", headers=AUTH)

    assert rec.calls[0].url.params.multi_items() == [
        ("building", "Ap0"),
        ("q", "x"),
    ]


def test_unknown_upstream_or_resource_404():
    client, rec = _client({})

    assert client.get("/nope/rooms", headers=AUTH).status_code == 404
    assert client.get("/people/nope", headers=AUTH).status_code == 404
    assert rec.calls == []


def test_missing_or_wrong_token_401():
    client, rec = _client({"/api/get_rooms": []})

    assert client.get("/people/rooms").status_code == 401
    assert (
        client.get("/people/rooms", headers={"x-ozon-s2s-token": "x"}).status_code
        == 401
    )
    assert rec.calls == []


def test_post_rejected_when_not_allowed():
    client, rec = _client({"/api/get_rooms": []})

    res = client.post("/people/rooms", headers=AUTH, json={"a": 1})

    assert res.status_code == 405
    assert rec.calls == []


def test_post_forwarded_with_body_when_allowed():
    client, rec = _client(
        {"/api/get_persons_by_workmodes": [{"uid": "mrossi"}]}
    )

    res = client.post(
        "/people/by_workmodes", headers=AUTH, json={"workmodes": [1, 2]}
    )

    assert res.status_code == 200
    assert res.json() == [{"uid": "mrossi"}]
    assert rec.calls[0].method == "POST"
    assert json.loads(rec.calls[0].content) == {"workmodes": [1, 2]}


def test_post_invalid_json_400():
    client, rec = _client({})

    res = client.post(
        "/people/by_workmodes",
        headers={**AUTH, "content-type": "application/json"},
        content=b"{not json",
    )

    assert res.status_code == 400
    assert rec.calls == []


def test_upstream_error_502():
    client, _ = _client({"/api/get_rooms": []}, status_code=500)

    assert client.get("/people/rooms", headers=AUTH).status_code == 502


def test_get_cached_per_query_string():
    client, rec = _client({"/api/get_rooms": [{"id": 1}]})

    client.get("/people/rooms", headers=AUTH)
    client.get("/people/rooms", headers=AUTH)
    client.get("/people/rooms?b=1", headers=AUTH)

    assert len(rec.calls) == 2


def test_post_never_cached():
    client, rec = _client({"/api/get_persons_by_workmodes": []})

    client.post("/people/by_workmodes", headers=AUTH, json={})
    client.post("/people/by_workmodes", headers=AUTH, json={})

    assert len(rec.calls) == 2


def test_index_lists_resources():
    client, _ = _client({})

    paths = {r["path"]: r["allow_post"] for r in client.get("/", headers=AUTH).json()["result"]}

    assert paths["people/rooms"] is False
    assert paths["people/by_workmodes"] is True


def test_missing_env_var_fails_at_startup():
    with pytest.raises(ValueError, match="PEOPLE_KEY"):
        parse_upstreams(RAW, {"PEOPLE_URL": "https://people.test"})


def test_unknown_resource_key_rejected():
    raw = json.loads(json.dumps(RAW))
    raw["upstreams"]["people"]["resources"]["rooms"]["method"] = "POST"
    with pytest.raises(ValueError):
        parse_upstreams(raw, ENV)


def test_missing_s2s_token_fails():
    with pytest.raises(ValueError, match="API_GATEWAY_S2S_TOKEN"):
        create_app(_config(s2s_token=""), upstreams={})


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_upstreams_loaded_from_separate_files(tmp_path):
    _write(tmp_path / "gateway.json", {"upstreams": {"people": "people.json"}})
    _write(tmp_path / "people.json", RAW["upstreams"]["people"])

    upstreams = load_upstreams(str(tmp_path / "gateway.json"), ENV)

    assert upstreams["people"].base_url == "https://people.test"
    assert upstreams["people"].headers == {"x-key": "people-key"}
    assert upstreams["people"].resources["by_workmodes"].allow_post is True


def test_missing_upstream_file_fails(tmp_path):
    _write(tmp_path / "gateway.json", {"upstreams": {"people": "people.json"}})

    with pytest.raises(ValueError, match="config non trovato"):
        load_upstreams(str(tmp_path / "gateway.json"), ENV)


@pytest.mark.parametrize("ref", ["../people.json", "/etc/passwd", "sub/people.json"])
def test_upstream_file_must_stay_in_config_dir(tmp_path, ref):
    _write(tmp_path / "gateway.json", {"upstreams": {"people": ref}})

    with pytest.raises(ValueError, match="nome file"):
        load_upstreams(str(tmp_path / "gateway.json"), ENV)


def test_gateway_example_is_valid_and_empty():
    assert load_upstreams(str(CONFIG_DIR / "gateway.json.example"), {}) == {}


def test_upstream_example_has_expected_shape():
    example = json.loads((CONFIG_DIR / "upstream.json.example").read_text())

    # Template da riempire: stessa forma di UpstreamConfig, base_url vuoto
    # (quindi da solo NON avvia il gateway).
    assert set(example) == {"base_url", "headers", "cache_ttl", "resources"}
    with pytest.raises(ValueError, match="base_url"):
        parse_upstreams({"upstreams": {"x": example}}, {})
