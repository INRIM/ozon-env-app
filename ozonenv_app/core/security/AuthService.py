# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
from datetime import datetime
import logging
import uuid

from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from starlette.requests import Request
from starlette.responses import Response

from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.ozon.OzonModelApp import CoreModel, OzonModelApp
from ozonenv_app.core.security.SessionMain import SessionMain, Session
from ozonenv_app.core.utils.config_app import pwd_context

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, request: Request):
        self.ozon: OzonEnvApp = None
        self.request: Request = request
        self.app_code = ""
        self.pwd_context = pwd_context
        self.user: CoreModel = None
        self.ws_request = False
        self.token = ""
        self.session: Session = None
        self.user_m: OzonModelApp = None
        self.settings_m: OzonModelApp = None
        self.params_m: OzonModelApp = None
        self.session_cookie = "authtoken"

    async def check_auth(self, username: str = "", password: str = "") -> bool:
        user = await self.user_m.by_name(username)
        if not user:
            logger.warning(f"user {username} not found")
            return False
        verify = self.verify_password(password, user.password)
        if not verify:
            return False
        return True

    async def get_user_info(self, username: str) -> dict:
        user = await self.user_m.by_name(username)
        return user.get_dict()

    async def init_session(
        self, username: str, token=None, parent_session_token=""
    ):
        if not token:
            token = str(uuid.uuid4())
        session_service = SessionMain(self.ozon)
        self.session = await session_service.make_session(
            user_info=await self.get_user_info(username),
            token=token,
            parent_token=parent_session_token,
        )
        logger.info(f"self.session.token {self.session.token}")
        await self.ozon.app_run_session(
            self.session.token, self.request.scope['api_request']
        )
        return self.session

    # TODO handle multiple instance of same user with req_id
    async def login(self, form_data: OAuth2PasswordRequestForm) -> Response:
        self.ozon = self.request.scope['ozon']
        self.user_m = self.ozon.env.get("user")
        self.username = form_data.username.strip()
        password = form_data.password.strip()
        login_ok = await self.check_auth(self.username, password)
        logger.info(f"login {self.username} --> {login_ok}")
        if login_ok:
            self.session = await self.init_session(self.username)
            logger.info(f"self.session.token {self.session.token}")
            return self.login_complete()
        else:
            return self.login_error()

    def set_response_coockies(self, res: Response) -> Response:
        res.set_cookie(
            self.session_cookie,
            self.session.token
        )
        return res

    def login_complete(self) -> Response:
        logger.info(f"self.session.token {self.session.token}")
        response = JSONResponse(
            {
                "access_token": self.session.token,
                "token_type": "bearer",
                "url": "/dashboard",
            }
        )

        return self.set_response_coockies(response)

    def login_error(self) -> JSONResponse:
        response = JSONResponse(
            {
                "content": {
                    "status": "error",
                    "message": f"Errore login utente o password non validi",
                    "model": 'login',
                }
            }
        )
        return response

    async def logout(self):
        self.ozon = self.request.scope['ozon']
        self.ozon.session.active = False
        self.ozon.session.last_update = datetime.now().timestamp()
        return await self.ozon.update_session()

    def verify_password(self, plain_password, hashed_password):
        return self.pwd_context.verify(plain_password, hashed_password)

    def get_password_hash(self, password):
        return self.pwd_context.hash(password)
