from __future__ import annotations

import json

import httpx
import pytest

from mcp_search.gateway import MissingAuthorizationError
from mcp_search.gateway import OzonSearchGateway


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://ozon-env-app:8000",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_find_forwards_authorization_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "fail": False,
                "message": "",
                "content": {"mode": "list", "data": [{"rec_name": "c1"}]},
            },
        )

    gateway = OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=_client(handler),
    )

    result = await gateway.find(
        authorization="Bearer u1-token",
        model="customer",
        query={"status": {"$eq": "open"}},
        order="rec_name:asc",
        skip=0,
        limit=10,
    )

    assert seen["path"] == "/list/customer"
    assert seen["params"] == {"stream": "false"}
    assert seen["authorization"] == "Bearer u1-token"
    assert seen["body"] == {
        "query": {"status": {"$eq": "open"}},
        "order": "rec_name:asc",
        "skip": 0,
        "limit": 10,
    }
    assert result == {
        "ok": True,
        "status_code": 200,
        "response": {
            "fail": False,
            "message": "",
            "content": {"mode": "list", "data": [{"rec_name": "c1"}]},
        },
    }


@pytest.mark.asyncio
async def test_find_surfaces_acl_denied_without_raising():
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

    gateway = OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=_client(handler),
    )

    result = await gateway.find(
        authorization="Bearer u1-token",
        model="customer",
        query={"salary": {"$gt": 1}},
    )

    assert result["ok"] is False
    assert result["status_code"] == 403
    assert result["detail"]["fields"] == ["salary"]


@pytest.mark.asyncio
async def test_find_without_authorization_raises_before_any_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    gateway = OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=_client(handler),
    )

    with pytest.raises(MissingAuthorizationError):
        await gateway.find(authorization="", model="customer")

    assert called is False


@pytest.mark.asyncio
async def test_list_models_forwards_authorization():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "fail": False,
                "message": "",
                "content": {
                    "mode": "list",
                    "data": ["customer", "user", "order"],
                },
            },
        )

    gateway = OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=_client(handler),
    )

    result = await gateway.list_models(authorization="Bearer u1-token")

    assert seen["path"] == "/models/distinct"
    assert seen["authorization"] == "Bearer u1-token"
    assert result["ok"] is True
    assert result["response"]["content"]["data"] == [
        "customer",
        "user",
        "order",
    ]


@pytest.mark.asyncio
async def test_list_models_without_authorization_raises_before_any_request():
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    gateway = OzonSearchGateway(
        base_url="http://ozon-env-app:8000",
        http_timeout=5,
        http_client=_client(handler),
    )

    with pytest.raises(MissingAuthorizationError):
        await gateway.list_models(authorization="")

    assert called is False
