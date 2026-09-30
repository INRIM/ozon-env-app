import logging
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("uvicorn.error")


class RemoteSelectHeader(BaseModel):
    url: str
    path_value: str = ""
    header_key: str = ""
    header_value_key: str = ""


class SelectOption(BaseModel):
    """Singola opzione di select nel formato atteso dal client formio."""

    label: str
    value: Any


class SelectListResult(BaseModel):
    select_list: List[SelectOption] = Field(default_factory=list)


class RemoteSelectResponse(BaseModel):
    """Contratto di risposta dei service gateway per le select remote.

    Un gateway che risponde con questa forma salta l'estrazione euristica
    del payload (`extract_remote_data`). Le righe passano comunque da
    `formio.make_resource_list` -> `_normalize_label_and_value`, che le
    risolve sui campi `label`/`value`: il component non deve dichiarare
    `properties.label` / `properties.id`, e se ne ha di legacy (puntati sui
    campi della API esterna) si ricade comunque su `label`/`value`.
    """

    result: SelectListResult


def build_select_response(options: List[Dict[str, Any]]) -> RemoteSelectResponse:
    """Helper per i gateway: costruisce la response dal formato label/value."""

    return RemoteSelectResponse(
        result=SelectListResult(select_list=[SelectOption(**opt) for opt in options])
    )


def parse_remote_select_response(payload: Any) -> List[Dict[str, Any]] | None:
    """Riconosce il contratto gateway; None se il payload non lo rispetta.

    None NON e' un errore: significa "sorgente legacy", e il chiamante
    prosegue con `extract_remote_data` + normalizzazione euristica (serve
    per le select remote gia' configurate su API esterne che rispondono
    con liste di forma arbitraria).
    """

    if not isinstance(payload, dict):
        return None
    try:
        parsed = RemoteSelectResponse.model_validate(payload)
    except ValidationError:
        return None
    return [opt.model_dump() for opt in parsed.result.select_list]


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
