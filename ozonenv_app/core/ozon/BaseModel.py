from typing import List, Dict, Any
from pydantic import BaseModel, Field


class SelectItem(BaseModel):
    value: Any
    label: str


class SelectElemensts(BaseModel):
    values: List[SelectItem] = []


class LayoutSettings(BaseModel):
    module_name: str = ""
    version: str = ""
    logo_img_url: str = ""


class BaseResponse(BaseModel):
    mode: str = ""
    schema_: Dict = Field({}, alias="schema")
    data: Any
    model: str = ""
    editable: bool


class DataResponse(BaseResponse):
    context_buttons: list = []
    query: Dict = {}
    is_domain_query: bool = False
    limit: int = (0,)
    skip: int = (0,)
    sort: str = ("",)
    recordsTotal: int = 0
    recordsFiltered: int = 0
    action_url: str = ""
    action_name: str = ""
    related_name: str = ""
    builder: bool = False
    component_type: str = ""
    title: str = ""
    fast_search: Dict = {}


class ResponseData(BaseModel):
    status: str = "ok"
    link: str = ""
    reload: bool = False
    action: str = ""
    message: str = ""
    rec_name: str = ""
    model: str = ""
    settings: LayoutSettings | None = None
    menu: Dict = {}
    data: Any
    content: DataResponse | None = None


class ResponseDataErr(BaseModel):
    status: str = "error"
    message: str = ""
    model: str = ""
    rec_name: str = ""
    link: str = ""
    reload: bool = False
    action: str = ""


class LayoutResponse(BaseModel):
    mode: str = "system"
    settings: LayoutSettings | None = None
    menu: Dict = {}
    schema_: Dict = Field({}, alias="schema")


class SchemaModelResponse(BaseResponse):
    fields: list = []
    metadata: list = []
