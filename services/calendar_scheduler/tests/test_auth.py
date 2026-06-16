import httpx

from calendar_scheduler.auth import M2MTokenProvider


def _provider(handler, **over):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    kwargs = dict(
        token_url="http://kc/token",
        client_id="scheduler",
        client_secret="secret",
        http_client=client,
    )
    kwargs.update(over)
    return M2MTokenProvider(**kwargs)


async def test_fetches_and_caches_token():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = dict(httpx.QueryParams(request.content.decode()))
        assert body["grant_type"] == "client_credentials"
        assert body["client_id"] == "scheduler"
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 300})

    prov = _provider(handler)
    assert await prov.authorization() == "Bearer tok"
    # seconda chiamata usa la cache, niente nuova POST
    assert await prov.token() == "tok"
    assert calls["n"] == 1


async def test_refetches_when_expired():
    tokens = iter(["t1", "t2"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"access_token": next(tokens), "expires_in": 10}
        )

    # skew 30s > expires_in 10s -> sempre "scaduto" -> refetch a ogni token()
    prov = _provider(handler, refresh_skew_seconds=30)
    assert await prov.token() == "t1"
    assert await prov.token() == "t2"


async def test_passes_audience_when_set():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"access_token": "x", "expires_in": 300})

    prov = _provider(handler, audience="ozon-api")
    await prov.token()
    assert seen["audience"] == "ozon-api"
