# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import logging
import os
import typing

from fastapi.responses import JSONResponse
from ozonenv.core.BaseModels import Session
from starlette.authentication import (
    SimpleUser,
    AuthenticationBackend,
    AuthCredentials,
)
from starlette.requests import HTTPConnection, Request

# from ozonenv_app.core.db.mongodb import get_db
from ozonenv_app.core.ozon.BaseModel import ResponseDataErr
from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.security.SessionMain import SessionMain
from ozonenv_app.core.utils.config_app import public_endpoint, ui_endpoint, \
    session_free_endpoit

logger = logging.getLogger(__name__)


class AuthUser(SimpleUser):
    def __init__(self, session: Session) -> None:
        self.session: Session = session
        super().__init__(self.session.uid)

class PingUser(SimpleUser):
    def __init__(self) -> None:
        super().__init__("pong")

class OzonAuthenticationBackend(AuthenticationBackend):
    def __init__(self):
        self.ozon: OzonEnvApp = None
        self.conn: HTTPConnection = None
        self.request: Request = None
        self.session: Session = None
        self.app_code = os.getenv("APP_CODE")
        self.ws_request = False
        self.token = ""

    async def check_default_token_header(self):
        self.token = False
        authtoken = self.conn.cookies.get("authtoken", "")
        if not authtoken:
            authtoken = self.conn.headers.get("authtoken", "")
        apitoken = self.conn.headers.get("apitoken", False)
        token = self.conn.query_params.get("token", False)
        if authtoken:
            self.token = authtoken
        if token and not self.token:
            self.token = token
        if self.token is False and apitoken:
            # TODO handle here ws-users token | self.ws_request = True
            logger.debug(f"ws_request {apitoken}")
            self.conn.scope['api_request'] = True
            self.token = apitoken
            logger.info(f" Is WS {self.ws_request} with token {self.token}")

    async def init_public_session(self):
        session_main = SessionMain(self.ozon)
        self.session = await session_main.init_public_session()
        self.ozon.env.orm.user_session = self.session
        self.token = self.ozon.env.orm.user_session.token
        self.ozon.env.user_session = self.ozon.env.orm.user_session
        self.ozon.session = self.ozon.env.orm.user_session
        self.ozon.env.session_token = self.token

    async def check_session(self):

        await self.check_default_token_header()
        # self.ozon = OzonEnvApp()
        # await self.ozon.new_app(db=get_db())
        #  = self.ozon
        self.ozon = self.conn.scope['ozon']
        self.app_code = self.ozon.env.app_code
        if not self.token:
            await self.init_public_session()
        else:
            if not await self.ozon.chek_token_valid(self.token):
                await self.init_public_session()
                self.conn.cookies.update(
                    {"authtoken": self.ozon.session.token})
            else:
                await self.ozon.env.orm.init_session(self.token)
                self.ozon.env.session_token = self.token
                chk_session = await self.ozon.app_run_session(
                    self.token, self.ws_request
                )
                logger.info(f"Session User: {self.ozon.session.uid}")
                self.session = self.ozon.session
                if chk_session.fail:
                    await self.init_public_session()
                    self.conn.cookies.update(
                        {"authtoken": self.ozon.session.token})


    async def authenticate(
        self, conn: HTTPConnection
    ) -> typing.Optional[typing.Tuple["AuthCredentials", "BaseUser"]]:
        self.conn = conn
        self.conn.scope['api_request'] = False
        if not self.is_ui_endpoint():
            self.conn.scope['api_request'] = True
        if self.is_session_free():
            return AuthCredentials(), PingUser()
        await self.check_session()
        logger.info("authenticate chk End")
        return AuthCredentials(), AuthUser(self.session)

    def is_public_endpoint(self) -> bool:
        if self.conn.url.path in public_endpoint:
            return True
        return False

    def is_ui_endpoint(self) -> bool:
        if self.conn.url.path in ui_endpoint:
            return True
        return False

    def is_session_free(self) -> bool:
        for item in session_free_endpoit:
            if self.conn.url.path.startswith(item):
                return True
        return False

    @staticmethod
    def default_on_error(conn: HTTPConnection, exc: Exception) -> JSONResponse:
        return JSONResponse(ResponseDataErr(message=str(exc)), status_code=400)
