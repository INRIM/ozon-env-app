# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.

import logging

from fastapi.responses import Response

from ozonenv_app.core.db.mongodb import get_db
from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp

logger = logging.getLogger(__name__)


class InterceptorBase:
    def __init__(self):
        self.ozon: OzonEnvApp = None

    async def before_request(self, request):
        self.ozon = OzonEnvApp()
        await self.ozon.new_app(db=get_db())
        request.scope['ozon'] = self.ozon
        return request

    async def before_response(self, request, response: Response):
        if self.ozon and self.ozon.session:
            if self.ozon.session.app['save_session']:
                await self.ozon.update_session()

            # response.headers.append("authtoken", self.ozon.session.token)
            # response.set_cookie(
            #     "authtoken", self.ozon.session.token.encode("utf-8"))
            # await self.ozon.env.close_env()
            # print("Response coockies")

        return response
