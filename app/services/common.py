from typing import Any

from ozonenv.core.OzonModel import OzonModelBase
from pydantic import BaseModel
from pydantic import Field

from app.services.utils import check_parse_json


class ListRequest(BaseModel):
    """Payload usato dagli endpoint di listing paginato."""

    query: dict[str, Any] = Field(default_factory=dict)
    order: str = ""
    skip: int = 0
    limit: int = 100


class RemoteSelectHeaderEntry(BaseModel):
    """Coppia chiave/valore usata per valorizzare un header remoto."""

    key: str = ""
    value: str = ""

    class Config:
        extra = "allow"


class RemoteSelectData(BaseModel):
    """Sotto-oggetto `data` inviato dal client per select remote."""

    url: str = ""
    path_value: str = Field(default="", alias="pathValue")
    headers: list[RemoteSelectHeaderEntry] = Field(default_factory=list)
    header_key: str = Field(default="", alias="headerKey")
    header_value_key: str = Field(default="", alias="headerValueKey")

    class Config:
        extra = "allow"
        validate_by_name = True


class RemoteSelectProperties(BaseModel):
    """Sotto-oggetto `properties` con i metadati del componente select."""

    model: str = ""
    domain: dict[str, Any] = Field(default_factory=dict)
    compute_label: str = ""
    src: str = ""
    label: str = ""
    id: str = ""

    class Config:
        extra = "allow"


class RemoteSelectRequest(BaseModel):
    """Payload principale per endpoint di select remota."""

    key: str = ""
    curr_model: str = ""
    data: RemoteSelectData = Field(default_factory=RemoteSelectData)
    properties: RemoteSelectProperties = Field(
        default_factory=RemoteSelectProperties
    )

    class Config:
        extra = "allow"

    def has_properties(self) -> bool:
        """Ritorna True se `properties` contiene almeno un valore utile."""

        if hasattr(self.properties, "model_dump"):
            props = self.properties.model_dump(exclude_none=True)
        else:
            props = self.properties.dict(exclude_none=True)
        cleaned = {
            key: value
            for key, value in props.items()
            if value not in ("", None, [], {}, ())
        }
        return bool(cleaned)

class ResponseObjectData(BaseModel):
    mode: str # can be form,list,list_stream
    data: Any
    readable: bool = True
    editable: bool = True
    can_create: bool = True
    model: str = ""
    query: dict = Field(default_factory=dict)
    obfucated_fields: list[str] = Field(default_factory=list)
    editable_fields: list[str] = Field(default_factory=list)
    schema: Any = []
    rec_name: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    columns: dict[str, str] = Field(default_factory=dict)
    filter_kyes: dict[str, str] = Field(default_factory=dict)
    batch_size: int = 0
    total_count: int = 0

class ResponseObject(BaseModel):
    content: ResponseObjectData
    fail: bool = False
    message: str = ""


def _extract_rec_name(data: Any) -> str:
    """Estrae `rec_name` da oggetti eterogenei senza assumere attributi."""

    if data is None:
        return ""
    if isinstance(data, dict):
        value = data.get("rec_name", "")
    else:
        value = getattr(data, "rec_name", "")
    if value in (None, ""):
        return ""
    return str(value)


def make_response_object(
    model: OzonModelBase = None,
    mode: str = None,
    data: Any = None,
    query: dict = None,
    fields: list[str] = None,
    batch_size: int = 0,
    readable: bool = True,
    editable: bool = True,
    can_create: bool = True,
    total_count: int = 0,
) -> ResponseObject:
    """Costruisce una risposta API uniforme per form/list/stream."""

    content = ResponseObjectData(
        mode=mode,
        data=data if data else [],
        total_count=total_count,
    )
    if model and not model.status.fail:
        component = model.model.schema() if model else {}
        schema = component.get("components", [])
        content = ResponseObjectData(
            mode=mode,
            data=data,
            model=model.data_model,
            schema=schema,
            rec_name=_extract_rec_name(data),
            query=query if query else {},
            readable=readable,
            can_create=can_create,
            editable=editable,
            batch_size=batch_size,
            fields=fields if fields else {},
            total_count=total_count,
        )
        if mode in ["list_stream", "list"]:
            content.columns = model.table_columns
            content.filter_kyes = model.model.filter_keys()

    return ResponseObject(
        fail=model.status.fail if model else False,
        message=model.status.msg if model else "",
        content=content,
    )

async def get_global_param(cli_session, name: str) -> Any:

    params = await cli_session.service.by_name("global_params", name)
    if not params:
        return {}
    vals = check_parse_json(params.value)
    if isinstance(vals, dict):
        return vals.copy()
    else:
        if isinstance(vals, str):
            return {}
        return vals
