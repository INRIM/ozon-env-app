# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.


import logging
import os
import pathlib
import sys

from fastapi import FastAPI
from fastapi import Request, Header, HTTPException, Depends
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from starlette.middleware.authentication import AuthenticationMiddleware

from ozonenv_app.core.db.mongodb import connect_db, close_db
from ozonenv_app.core.ozon.OzonInterceptor import InterceptorBase
from ozonenv_app.core.ozon.OzonModelApp import OzonModelApp
from ozonenv_app.core.security.AuthService import AuthService
from ozonenv_app.core.security.OzonAuthenticationBackend import (
    OzonAuthenticationBackend,
)
from ozonenv_app.core.services.ServiceRenderer import ServiceRenderer
from ozonenv_app.core.utils.Codec import check_parse_json
from ozonenv_app.core.utils.config_app import tags_metadata

logger = logging.getLogger(__name__)

sys.path.append(str(pathlib.Path(__file__).parent.resolve()))


auth_class = AuthService


app = FastAPI(
    title=os.getenv("APP_TITLE"),
    description=os.getenv("APP_DESC"),
    version=os.getenv("APP_VERSION"),
    openapi_tags=tags_metadata,
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.mount(
    "/static",
    StaticFiles(directory=os.getenv("APP_WEB_STATIC")),
    name="static",
)

app.add_event_handler("startup", connect_db)
app.add_event_handler("shutdown", close_db)
app.add_middleware(
    AuthenticationMiddleware, backend=OzonAuthenticationBackend()
)


def check_response_data(res_data: dict) -> dict:
    if res_data.get("status") and res_data.get("status") == "error":
        raise HTTPException(status_code=422, detail=res_data['message'])
    else:
        return res_data


@app.middleware("http")
async def add_interceptor(request: Request, call_next):
    interceptor = InterceptorBase()
    request = await interceptor.before_request(request)
    logger.info(f" rrrrr {request.scope.get('ozon')}")
    response = await call_next(request)
    response = await interceptor.before_response(request, response)
    return response


@app.get("/status", tags=["Beacon"])
async def service_status():
    """
    Ritorna lo stato del servizio
    """
    return {"status": "live"}


@app.get("/", tags=["Beacon"])
async def init(request: Request, request_type: str = Header(None)):
    return RedirectResponse(url="/dashboard")


@app.get("/login", tags=["Auth"])
async def loging(request: Request):
    logger.info(" --> Login ")
    render = ServiceRenderer(request)

    return await render.render_layout(
        form_schema="login", submit="/login", page="form"
    )


@app.post("/login", tags=["Auth"])
async def loginp(request: Request):
    logger.info(" User --> Login ")
    payload = await request.json()
    data = payload['data']
    form_data: OAuth2PasswordRequestForm = OAuth2PasswordRequestForm(
        username=data['username'], password=data['password']
    )
    auth_service = auth_class(request)
    return await auth_service.login(form_data)


@app.post("/token", tags=["Auth"])
async def login_for_access_token(
    request: Request, form_data: OAuth2PasswordRequestForm = Depends()
):
    logger.info(" User --> Login ")
    auth_service = auth_class(request)
    return await auth_service.login(form_data)


@app.get("/logout", tags=["Auth"])
async def logout(request: Request):
    auth_service = auth_class(request)
    resp = await auth_service.logout()
    return resp


@app.get("/dashboard", tags=["Dashboard"])
async def dashboard(request: Request):
    render = ServiceRenderer(request)
    return await render.render_layout(form_schema="", page="dashboard")


@app.get("/dashboard/{menu_group}", tags=["Dashboard"])
async def dashboardmnu(request: Request, menu_group: str):
    render = ServiceRenderer(request)
    return await render.render_layout(form_schema="", page="dashboard")


@app.get("/schema/{model}", tags=["Form"])
async def schmamodel(request: Request, model: str):
    logger.info(" User --> needed ")
    ozon = request.scope['ozon']
    return {"schema": await ozon.component_schema(model), "data": {}}


@app.get("/schema/{model}/{name}", tags=["Form"])
async def schmamodel(request: Request, model: str, name: str):
    logger.info(" User --> needed ")
    ozon = request.scope['ozon']
    compo, data = await ozon.component_schema_data(model, name)
    return {"schema": compo, "data": data}


@app.get("/module/{model}", tags=["View Form"])
async def viewmodeltype(request: Request, model: str):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_layout(
        form_schema=model, submit=f"/submit/{model}", page="form"
    )


@app.get("/module/{model}/{name}", tags=["View Form"])
async def viewmodeltype(request: Request, model: str, name: str):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_module(
        form_schema=model, rec_name=name, page="form"
    )


@app.post("/submit/{model}", tags=["View Form"])
async def schmamodelpost(request: Request, model: str):
    data = await request.json()
    ozon = request.scope['ozon']
    _model = ozon.env.get(model)
    todo = await _model.new(data['data'])
    exist = await _model.by_name(todo.rec_name)
    if not exist:
        await _model.insert(todo)
    else:
        await _model.update(todo)
    return await ozon.component_schema(model)


@app.get("/models/distinct", tags=["Form Data"])
async def model_distinct(request: Request):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    listpatams = list(params.keys())
    model = "component"
    distinct = "rec_name"
    compute_label = "title"
    queryd = {}
    if "model" in listpatams:
        model = params['model']
    if "compute_label" in listpatams:
        compute_label = params['compute_label']
    if "query" in listpatams:
        queryd = check_parse_json(params['query'])
    compo: OzonModelApp = ozon.env.get(model)
    query = compo.default_domain
    if isinstance(queryd, dict):
        query.update(queryd)
    listd = await compo.search_all_distinct(
        distinct=distinct,
        query=query,
        limit=int(params.get("limit", 0)),
        skip=int(params.get("skip", 0)),
        compute_label=compute_label,
    )
    return listd


@app.get("/data/{model}", tags=["Form Data"])
async def list_data_model(request: Request, model: str):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    listpatams = list(params.keys())
    queryd = {}
    if "query" in listpatams:
        queryd = check_parse_json(params['query'])
    compo: OzonModelApp = ozon.env.get(model)
    query = compo.default_domain
    if isinstance(queryd, dict):
        query.update(queryd)
    listd = await compo.find(
        domain=query,
        limit=int(params.get("limit", 0)),
        skip=int(params.get("skip", 0)),
    )
    return listd


@app.get("/form/{model}/submission", tags=["Form Data"])
async def model_submissions(request: Request, model: str):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    listpatams = list(params.keys())
    queryd = {}
    if "query" in listpatams:
        queryd = check_parse_json(params['query'])
    _model: OzonModelApp = ozon.env.get(model)
    query = _model.default_domain
    if isinstance(queryd, dict):
        query.update(queryd)
    listd = await _model.find(
        query,
        limit=int(params.get("limit", 0)),
        skip=int(params.get("skip", 0)),
    )
    return listd


@app.get("/form/{model}/submission/{name}", tags=["Form Data"])
async def model_submissions(request: Request, model: str, name: str):
    ozon = request.scope['ozon']
    _model: OzonModelApp = ozon.env.get(model)
    return await _model.by_name(name)


@app.post("/builder_mode/{mode}", tags=["Form Builder"])
async def set_builder_mode(request: Request, mode: int):
    app_code = request.scope['ozon'].session.app.get('app_code', "")
    request.scope['ozon'].session.apps[app_code]['builder'] = mode > 0
    request.scope['ozon'].session.app['builder'] = 1 == mode
    request.scope['ozon'].session.app['save_session '] = True
    return {"status": "ok"}


@app.get("/builder/{pagetype}", tags=["Form Builder"])
async def builderframebase(
    request: Request,
    pagetype: str = "form",
):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_builder(
        ptype=pagetype, rec_name="", page="builder"
    )


@app.get("/builder/{pagetype}/{rec_name}", tags=["Form Builder"])
async def builderframe(
    request: Request,
    pagetype: str = "form",
    rec_name: str = "",
):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_builder(
        ptype=pagetype, rec_name=rec_name, page="builder"
    )


@app.get("/design/builder/{pagetype}", tags=["Form Builder"])
async def builder_desgin_new(
    request: Request,
    pagetype: str = "form",
):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_builder(
        ptype=pagetype, rec_name="", page="builder_page"
    )


@app.post("/action/builder/{name}/{rec_name}", tags=["Form Builder"])
async def builderaction(request: Request, name: str, rec_name: str = ""):
    data = await request.json()
    ozon = request.scope['ozon']
    _model: OzonModelApp = ozon.env.get("component")
    todo = await _model.new(data)
    if rec_name and name == "delete":
        await _model.set_to_delete(todo)
        return {"link": "/dashboard"}
    elif not rec_name:
        await _model.insert(todo)
        return {"link": f"/builder/{todo.type}/{todo.rec_name}"}
    elif rec_name:
        await _model.update(todo)
        if name == "preview":
            return {"link": f"/module/{todo.rec_name}", "reload": True}
        elif name == "update":
            return {"link": f"#", "reload": True}


@app.get("/design/builder/{pagetype}/{rec_name}", tags=["Form Builder"])
async def builder_desgin(
    request: Request,
    pagetype: str = "form",
    rec_name: str = "",
):
    logger.info(" --> m ")
    render = ServiceRenderer(request)
    return await render.render_builder(
        ptype=pagetype, rec_name=rec_name, page="builder_page"
    )


# /form?type=resource&limit=1000000&select=_id,title&limit=100&skip=0
@app.get("/form", tags=["Form Builder"])
async def model_submissions(request: Request):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    listpatams = list(params.keys())
    model = "component"
    distinct = "rec_name"
    compute_label = "title"
    projection = {}
    pitem = {"$group": {}}
    queryd = {}
    if "model" in listpatams:
        model = params['model']
    if "compute_label" in listpatams:
        compute_label = params['compute_label']
    if "query" in listpatams:
        queryd = check_parse_json(params['query'])
    compo: OzonModelApp = ozon.env.get(model)
    query = compo.default_domain
    if "type" in listpatams:
        ftype = params['type']
        query.update({"type": ftype})
    if 'select' in params:
        fields = params['select']
        for item in fields.split(","):
            if item == "_id":
                projection["rec_name"] = 1
                pitem['$group'][item] = '$rec_name'
            else:
                projection[item] = 1
                pitem['$group'][item] = {"$first": f'${item}'}
    if isinstance(queryd, dict):
        query.update(queryd)
    listd = await compo.find_raw(
        domain=query,
        pipeline_items=[pitem],
        limit=int(params.get("limit", 0)),
        skip=int(params.get("skip", 0)),
        fields=projection,
    )
    return listd


# http://localhost:8002/form/menu_group?limit=100&skip=0
@app.get("/form/{model}", tags=["Form Builder"])
async def model_data_list(request: Request, model: str):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    listparams = list(params.keys())
    queryd = {}
    if "query" in listparams:
        queryd = check_parse_json(params['query'])
    _model: OzonModelApp = ozon.env.get(model)
    query = _model.default_domain
    if isinstance(queryd, dict) and queryd:
        query.update(queryd)
    listd = await _model.find(
        query,
        limit=int(params.get("limit", 100)),
        skip=int(params.get("skip", 0)),
    )
    return listd
