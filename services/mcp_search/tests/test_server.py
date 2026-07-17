from __future__ import annotations

import json

import httpx
import pytest
from fastmcp.exceptions import ResourceError
from fastmcp.exceptions import ToolError

from mcp_search.config import SearchConfig
from mcp_search.gateway import OzonSearchGateway
from mcp_search.server import build_server
from mcp_search.server import find_records_core
from mcp_search.server import list_models_core


def _config(**overrides) -> SearchConfig:
    base = dict(
        ozon_base_url="http://ozon-env-app:8000",
        http_timeout=5.0,
        mcp_host="0.0.0.0",
        mcp_port=8090,
        mcp_path="/mcp",
        default_limit=50,
        max_limit=200,
    )
    base.update(overrides)
    return SearchConfig(**base)


def _gateway(handler) -> OzonSearchGateway:
    return OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=httpx.AsyncClient(
            base_url="http://ozon-env-app:8000",
            transport=httpx.MockTransport(handler),
        ),
    )


@pytest.mark.asyncio
async def test_find_records_core_clamps_limit_to_config_max():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(
            200, json={"content": {"mode": "list", "data": []}}
        )

    result = await find_records_core(
        _gateway(handler),
        _config(max_limit=10),
        authorization="Bearer t",
        model="customer",
        limit=9999,
    )

    import json as _json

    assert _json.loads(seen["body"])["limit"] == 10
    assert result == {"content": {"mode": "list", "data": []}}


@pytest.mark.asyncio
async def test_find_records_core_defaults_limit_when_not_provided():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"content": {}})

    import json as _json

    await find_records_core(
        _gateway(handler),
        _config(default_limit=17),
        authorization="Bearer t",
        model="customer",
    )

    assert _json.loads(seen["body"])["limit"] == 17


@pytest.mark.asyncio
async def test_find_records_core_raises_tool_error_on_acl_denial():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "detail": {
                    "message": "Query references ACL-denied fields",
                    "fields": ["salary"],
                }
            },
        )

    with pytest.raises(ToolError):
        await find_records_core(
            _gateway(handler),
            _config(),
            authorization="Bearer t",
            model="customer",
            query={"salary": {"$gt": 1}},
        )


@pytest.mark.asyncio
async def test_find_records_core_raises_tool_error_without_authorization():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach ozon-env-app without a token")

    with pytest.raises(ToolError):
        await find_records_core(
            _gateway(handler),
            _config(),
            authorization="",
            model="customer",
        )


@pytest.mark.asyncio
async def test_build_server_registers_exactly_one_read_only_tool():
    mcp = build_server(
        _config(),
        OzonSearchGateway(base_url="http://x", http_timeout=1),
    )
    tools = await mcp.list_tools()
    assert [t.name for t in tools] == ["find_records"]
    assert tools[0].annotations.readOnlyHint is True
    assert tools[0].annotations.destructiveHint is False


@pytest.mark.asyncio
async def test_build_server_registers_models_resource():
    mcp = build_server(
        _config(),
        OzonSearchGateway(base_url="http://x", http_timeout=1),
    )
    resources = await mcp.list_resources()
    assert [str(r.uri) for r in resources] == ["ozon://models"]
    assert resources[0].mime_type == "application/json"


@pytest.mark.asyncio
async def test_list_models_core_returns_model_names_as_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": {
                    "mode": "list",
                    "data": ["customer", "user"],
                }
            },
        )

    result = await list_models_core(_gateway(handler), authorization="Bearer t")

    assert json.loads(result) == ["customer", "user"]


@pytest.mark.asyncio
async def test_list_models_core_raises_resource_error_without_authorization():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach ozon-env-app without a token")

    with pytest.raises(ResourceError):
        await list_models_core(_gateway(handler), authorization="")
