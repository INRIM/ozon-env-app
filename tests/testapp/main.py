import json
import logging

import httpx
from fastapi import Request

from ozonenv_app.core.appinit import app
from ozonenv_app.core.ozon.OzonModelApp import OzonModelApp

logger = logging.getLogger(__name__)

async def get_remote_data(
     headers: dict = None, header_key="", header_value="", url=""
):
    if headers is None:
        headers = {}

    logger.info(
        f"server get_remote_data --> {url}, header_key:{header_key},"
        f" header_value:{header_value} "
    )
    if header_key and header_value:
        headers.update(
            {"Content-Type": "application/json", header_key: header_value}
        )
    else:
        headers.update(
            {
                "Content-Type": "application/json",
            }
        )

    async with httpx.AsyncClient(timeout=None) as client:
        res = await client.get(url=url, headers=headers)

    if res.status_code == 200:
        logger.info(f"server get_remote_data --> {url} SUCCESS ")
        datar = res.json()
        data = datar.copy()
        if isinstance(datar, dict) and datar.get("result"):
            if isinstance(datar.get("result"), dict) and datar.get(
                "result"
            ).get("select_list"):
                data = datar.get("result", {}).get("select_list", [])
            if isinstance(datar.get("result"), list):
                data = datar.get("result", [])
    else:
        logger.info(
            f"server get_remote_data --> {url} Error {res.status_code} "
        )
        data = {}
    return data


@app.get("/ext/{service}/{path:path}", tags=["Structural Data"])
async def model_submissions(request: Request, service: str, path: str):
    ozon = request.scope['ozon']
    params = request.query_params._dict
    model: OzonModelApp = ozon.env.get("global_params")
    param = await model.by_name(service)
    value = json.loads(param.value)
    url = f"{value['url']}/{path}"
    return await get_remote_data(
        {}, value['header_key'], value['key'], url
    )
