from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.tools.tool import ToolAnnotations

from .config import SearchConfig
from .gateway import MissingAuthorizationError
from .gateway import OzonSearchGateway

_TOOL_DESCRIPTION = """Read-only search against ozon-env-app, on behalf of the \
caller's own identity (their Authorization bearer token, forwarded as-is).

The request runs through the SAME access control the user has in the app: \
model-level grants, row-level ownership rules, and field-level masking. \
There is no elevated access here and no write capability of any kind.

Check the `ozon://models` resource first to see which model names exist \
before calling this tool -- `model` must be one of those.

`query` is a MongoDB find()-style filter dict -- NOT an aggregation \
pipeline. Only these operators are accepted, everything else (including \
$where, $expr, $function, $accumulator, $text, geo operators) is rejected \
with a 403: $eq $ne $in $nin $gt $gte $lt $lte $and $or $nor $not $exists \
$all $size $elemMatch $regex $options.

Referencing a field the caller cannot read (masked/denied by field ACL) in \
either `query` or `order` is also rejected with a 403, even if the operator \
itself is allowed -- filtering or sorting on a field is treated the same as \
reading it.

`order` is `field:asc|desc[,field2:asc|desc]`.
"""

_MODELS_RESOURCE_DESCRIPTION = """List of model names known to ozon-env-app \
(static + dynamic, i.e. every collection with a component schema), fetched \
on behalf of the caller's own identity via the same forwarded bearer token \
used by `find_records`.

This list is NOT pre-filtered by the caller's model-level ACL -- it is a \
plain catalog of what models exist, not what this caller can query. \
`find_records` still enforces the real per-model/per-row/per-field access \
control; a name appearing here can still be rejected with a 403 there.
"""


def _tool_error_from_gateway_result(result: dict[str, Any]) -> ToolError:
    return ToolError(
        f"ozon-env-app rejected the request (HTTP {result['status_code']}): "
        f"{result['detail']}"
    )


async def find_records_core(
    gateway: OzonSearchGateway,
    config: SearchConfig,
    *,
    authorization: str,
    model: str,
    query: dict[str, Any] | None = None,
    order: str = "",
    skip: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Business logic of the `find_records` tool, isolated from MCP/HTTP
    header extraction so it can be unit tested with an explicit
    `authorization` value instead of a live fastmcp HTTP request context."""
    effective_limit = config.default_limit if limit is None else limit
    try:
        result = await gateway.find(
            authorization=authorization,
            model=model,
            query=query,
            order=order,
            skip=skip,
            limit=min(max(effective_limit, 1), config.max_limit),
        )
    except MissingAuthorizationError as exc:
        raise ToolError(
            "No Authorization header was forwarded to this MCP server -- "
            "the caller's real bearer token must be passed through by "
            "the MCP client/host, this tool never uses its own identity."
        ) from exc
    if not result["ok"]:
        raise _tool_error_from_gateway_result(result)
    return result["response"]


def _resource_error_from_gateway_result(result: dict[str, Any]) -> ResourceError:
    return ResourceError(
        f"ozon-env-app rejected the request (HTTP {result['status_code']}): "
        f"{result['detail']}"
    )


async def list_models_core(
    gateway: OzonSearchGateway, *, authorization: str
) -> str:
    """Business logic of the `ozon://models` resource, isolated from MCP/HTTP
    header extraction for the same reason as `find_records_core`."""
    try:
        result = await gateway.list_models(authorization=authorization)
    except MissingAuthorizationError as exc:
        raise ResourceError(
            "No Authorization header was forwarded to this MCP server -- "
            "the caller's real bearer token must be passed through by "
            "the MCP client/host, this resource never uses its own identity."
        ) from exc
    if not result["ok"]:
        raise _resource_error_from_gateway_result(result)
    response = result["response"]
    models = response.get("content", {}).get("data", []) if isinstance(
        response, dict
    ) else []
    return json.dumps(models, ensure_ascii=False)


def build_server(config: SearchConfig, gateway: OzonSearchGateway) -> FastMCP:
    mcp: FastMCP = FastMCP("ozon-env-search")

    @mcp.tool(
        name="find_records",
        description=_TOOL_DESCRIPTION,
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def find_records(
        model: str,
        query: dict[str, Any] | None = None,
        order: str = "",
        skip: int = 0,
        limit: int = config.default_limit,
    ) -> dict[str, Any]:
        headers = get_http_headers(include={"authorization"})
        return await find_records_core(
            gateway,
            config,
            authorization=headers.get("authorization", ""),
            model=model,
            query=query,
            order=order,
            skip=skip,
            limit=limit,
        )

    @mcp.resource(
        "ozon://models",
        name="models",
        title="Available models",
        description=_MODELS_RESOURCE_DESCRIPTION,
        mime_type="application/json",
    )
    async def models() -> str:
        headers = get_http_headers(include={"authorization"})
        return await list_models_core(
            gateway, authorization=headers.get("authorization", "")
        )

    return mcp
