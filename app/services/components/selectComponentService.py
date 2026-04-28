import logging
from typing import Any, Dict
from pydantic import BaseModel

logger = logging.getLogger("uvicorn.error")


class RemoteSelectHeader(BaseModel):
    url: str
    path_value: str = ""
    header_key: str = ""
    header_value_key: str = ""
    tocken: str = ""


def build_remote_select_header(data: Dict[str, Any]) -> RemoteSelectHeader:
    """Normalizza i metadati remoti da payload/formio in un header unico."""

    headers = data.get("headers", [])
    first_header = headers[0] if headers else {}
    path_value = data.get("path_value") or data.get("pathValue") or ""
    header_key = data.get("header_key") or data.get("headerKey") or ""
    header_value_key = (
        data.get("header_value_key")
        or data.get("headerValueKey")
        or ""
    )
    if not header_key and isinstance(first_header, dict):
        header_key = first_header.get("key", "")
    if not header_value_key and isinstance(first_header, dict):
        header_value_key = first_header.get("value", "")

    logger.info(
        "build_remote_select_header url=%s path_value=%s has_header=%s",
        data.get("url", ""),
        path_value,
        bool(header_key and header_value_key),
    )

    return RemoteSelectHeader(
        url=data.get("url", ""),
        path_value=path_value,
        header_key=header_key,
        header_value_key=header_value_key,
    )
