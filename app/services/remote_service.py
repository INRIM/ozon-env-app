import logging
from typing import Any
from urllib.parse import urlsplit

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


def _remote_select_settings() -> Any:
    from app.app_settings import get_env_settings

    return get_env_settings()


def _host_allowed(url: str, allowed_hosts: list[str]) -> tuple[bool, bool]:
    """(fetch consentito, host in allowlist).

    Allowlist vuota = nessun controllo sul fetch, ma l'host NON e' in
    allowlist: il token S2S resta a terra. Cosi' abilitare il token e'
    una scelta esplicita per host, non un effetto collaterale.
    """

    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        logger.warning("remote fetch rejected malformed url=%s", url)
        return False, False
    host = parts.hostname.lower()
    netloc = parts.netloc.lower()
    if not allowed_hosts:
        return True, False
    # Voce senza ":" = match sul solo host, qualunque porta. Con ":" =
    # match esatto host:porta, per restringere un gateway alla porta su
    # cui espone davvero le select.
    if host in allowed_hosts or netloc in allowed_hosts:
        return True, True
    logger.warning(
        "remote fetch rejected host not allowlisted host=%s url=%s", host, url
    )
    return False, False


async def _fetch_remote_data(
        headers: dict[str, str] | None = None,
        header_key: str = "",
        header_value: str = "",
        url: str = "",
        method: str = "GET",
        body: Any = None,
) -> Any:
    logger.info(
        "remote fetch start method=%s url=%s custom_header=%s",
        method,
        url,
        header_key,
    )
    settings = _remote_select_settings()
    allowed_hosts = list(
        getattr(settings, "remote_select_allowed_hosts", []) or []
    )

    # Ordine obbligatorio: prima si decide se l'host e' ammesso, solo dopo
    # si costruiscono gli header. Comporre la credenziale prima del check
    # significherebbe averla gia' pronta per un host non autorizzato.
    allowed, in_allowlist = _host_allowed(url, allowed_hosts)
    if not allowed:
        return []

    req_headers = dict(headers) if headers else {}
    req_headers["Content-Type"] = "application/json"
    if header_key and header_value:
        req_headers[header_key] = header_value

    s2s_token = str(getattr(settings, "remote_select_s2s_token", "") or "")
    s2s_header = str(
        getattr(settings, "remote_select_s2s_header", "") or ""
    )
    if in_allowlist and s2s_token and s2s_header:
        req_headers[s2s_header] = s2s_token

    try:
        async with httpx.AsyncClient(
            timeout=_REMOTE_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            if method == "POST":
                res = await client.post(url=url, headers=req_headers, json=body)
            else:
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
        method: str = "GET",
        body: Any = None,
) -> list[Any]:
    remote_url = _append_path(url, path_value)
    logger.info("remote select response start url=%s", remote_url)

    # Nessun lookup quando il component non dichiara la credenziale: con
    # i gateway header_value_key e' vuoto e `by_name` costerebbe una query
    # Mongo a vuoto ad ogni apertura di select.
    header_val = ""
    if header_value_key:
        rec_cfg = await get_global_param(service, header_value_key)
        raw_val = rec_cfg.get("key") if isinstance(rec_cfg, dict) else rec_cfg
        # `global_params.value` puo' contenere JSON di qualunque forma:
        # senza coercizione una lista finirebbe dritta in un header httpx.
        header_val = "" if raw_val in (None, "") else str(raw_val)

    remote_data = await _fetch_remote_data(
        headers={},
        header_key=header_key,
        header_value=header_val,
        url=remote_url,
        method=method,
        body=body,
    )

    data = remote_data if isinstance(remote_data, list) else []
    logger.info("remote select response count=%s", len(data))
    return data
