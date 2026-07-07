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


class FastSearchRequest(BaseModel):
    """Payload per l'endpoint /filter/fast_search/{action_name}."""

    query_fields: list[dict[str, Any]] = Field(default_factory=list)
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
    # mode: form | list | list_stream | redirect | status
    #   - redirect: il client naviga verso `next_action_url` (route-token, es.
    #     "#" = reload pagina corrente, "list_x" = vista named). Usato dalle
    #     response Camunda quando il task indica un avanzamento di pagina.
    #   - status: stato del processo (process_id + process_status), nessun form.
    mode: str
    data: Any
    readable: bool = True
    editable: bool = True
    can_create: bool = True
    model: str = ""
    query: dict = Field(default_factory=dict)
    obfucated_fields: list[str] = Field(default_factory=list)
    editable_fields: list[str] = Field(default_factory=list)
    schema: Any = []
    properties: dict = Field(default_factory=dict)
    sort: str = ""
    rec_name: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    columns: dict[str, str] = Field(default_factory=dict)
    filter_kyes: dict[str, str] = Field(default_factory=dict)
    batch_size: int = 0
    total_count: int = 0
    context_actions: list[dict[str, Any]] = Field(default_factory=list)
    title: str = ""
    # next_action_url: per mode=redirect, il route-token verso cui navigare
    # (es. "#" reload, "list_x"). Per le altre mode resta vuoto.
    next_action_url: str = ""
    # process_id / process_status: coordinate del processo Camunda (mode=status
    # o accompagnano form/redirect). process_status: started|running|completed|
    # terminated|timeout|error.
    process_id: str = ""
    process_status: str = ""

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


_COMPONENT_DEFAULT_COLUMNS: dict[str, str] = {
    "title": "Title",
    "rec_name": "Name",
    "sys": "Di Sistema",
    "type": "Tipo",
    "display": "Display",
    "demo": "Dati Demo",
    "projectId": "Progetto",
    "row_action": "Action",
}


def _extract_table_columns(schema: Any) -> dict[str, str]:
    columns: dict[str, str] = {}

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            if key and item.get("tableView") is True:
                columns[key] = str(item.get("label") or key)

            visit(item.get("components"))
            for col in item.get("columns") or []:
                if isinstance(col, dict):
                    visit(col.get("components"))
            for row in item.get("rows") or []:
                if isinstance(row, list):
                    for cell in row:
                        if isinstance(cell, dict):
                            visit(cell.get("components"))

    visit(schema)
    return columns


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
    next_action_url: str = "",
    process_id: str = "",
    process_status: str = "",
    fail: bool = False,
    message: str = "",
) -> ResponseObject:
    """Costruisce una risposta API uniforme per form/list/stream/redirect/status.

    `next_action_url`/`process_id`/`process_status` servono alle response Camunda
    (vedi ResponseObjectData). `fail`/`message` permettono di forzare uno stato
    d'errore anche quando non deriva dallo `status` del model (es. errore del
    task esterno Camunda)."""

    content = ResponseObjectData(
        mode=mode,
        data=data if data else [],
        total_count=total_count,
        next_action_url=next_action_url,
        process_id=process_id,
        process_status=process_status,
    )
    if model and not model.status.fail:
        component = model.model.schema() if model else {}
        schema = component.get("components", [])
        properties = component.get("properties", {})
        if isinstance(properties, str):
            try:
                import json
                properties = json.loads(properties)
            except Exception:
                properties = {}
        
        comp_sort = ""
        comp_query = {}
        if isinstance(properties, dict):
            comp_sort = str(properties.get("sort") or "").strip()
            q_val = properties.get("queryformeditable")
            if q_val:
                if isinstance(q_val, str):
                    try:
                        import json
                        comp_query = json.loads(q_val)
                    except Exception:
                        comp_query = {}
                elif isinstance(q_val, dict):
                    comp_query = q_val.copy()

        content = ResponseObjectData(
            mode=mode,
            data=data,
            model=model.data_model,
            schema=schema,
            properties=properties,
            sort=comp_sort,
            rec_name=_extract_rec_name(data),
            query=query if query else comp_query,
            readable=readable,
            can_create=can_create,
            editable=editable,
            batch_size=batch_size,
            fields=fields if fields else {},
            total_count=total_count,
            next_action_url=next_action_url,
            process_id=process_id,
            process_status=process_status,
        )
        if mode in ["list_stream", "list"]:
            content.columns = _extract_table_columns(schema) or model.table_columns
            if not content.columns and model.data_model == "component":
                content.columns = _COMPONENT_DEFAULT_COLUMNS
            content.filter_kyes = model.model.filter_keys()

    return ResponseObject(
        fail=fail or (model.status.fail if model else False),
        message=message or (model.status.msg if model else ""),
        content=content,
    )

async def get_global_param(service, name: str) -> Any:

    params = await service.by_name("global_params", name)
    if not params:
        return {}
    vals = check_parse_json(params.value)
    if isinstance(vals, dict):
        return vals.copy()
    else:
        if isinstance(vals, str):
            return {}
        return vals
