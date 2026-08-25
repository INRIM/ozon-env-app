import logging
from typing import Any

import httpx
from fastapi import status

from app.services.common import get_global_param
from app.services.utils import extract_remote_data

logger = logging.getLogger("uvicorn.error")




def _append_path(base_url: str, path_value: Any) -> str:
    """Concatena il path solo se valido, evitando eccezioni su tipi inattesi."""

    if not path_value or not isinstance(path_value, str):
        return base_url
    return f"{base_url.rstrip('/')}/{path_value.lstrip('/')}"


# La select remota parla con API esterne dichiarate sul component: un
# timeout esplicito evita che un host che non risponde tenga occupato un
# worker per sempre (`timeout=None` era illimitato), e i redirect
# disattivati impediscono che un 302 sposti la richiesta — insieme al
# token nell'header custom — verso un host non previsto.
_REMOTE_FETCH_TIMEOUT_SECONDS = 15.0


async def _fetch_remote_data(
        headers: dict[str, str] | None = None,
        header_key: str = "",
        header_value: str = "",
        url: str = "",
) -> Any:
    logger.info("remote fetch start url=%s custom_header=%s", url, header_key)
    req_headers = headers if headers else {}
    req_headers["Content-Type"] = "application/json"
    if header_key and header_value:
        req_headers[header_key] = header_value

    try:
        async with httpx.AsyncClient(
            timeout=_REMOTE_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            res = await client.get(url=url, headers=req_headers)
    except Exception as exc:
        logger.exception("get_remote_data error: %s", exc)
        return []

    if res.status_code != status.HTTP_200_OK:
        logger.warning(
            "remote fetch non-200 status_code=%s url=%s",
            res.status_code,
            url,
        )
        return []

    try:
        datar = res.json()
    except ValueError:
        logger.warning("remote fetch invalid json url=%s", url)
        return []
    logger.info("remote fetch completed url=%s", url)
    return extract_remote_data(datar)

async def remote_data_select_response(
        service: Any,
        url: str,
        path_value: str,
        header_key: str,
        header_value_key: str,
) -> list[Any]:
    remote_url = _append_path(url, path_value)
    logger.info("remote select response start url=%s", remote_url)
    rec_cfg = await get_global_param(service, header_value_key)

    header_val = rec_cfg.get("key") if isinstance(rec_cfg, dict) else rec_cfg

    remote_data = await _fetch_remote_data(
        headers={},
        header_key=header_key,
        header_value=header_val,
        url=remote_url
    )

    data = remote_data if isinstance(remote_data, list) else []
    logger.info("remote select response count=%s", len(data))
    return data
