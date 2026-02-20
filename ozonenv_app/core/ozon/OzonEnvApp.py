import logging
from datetime import datetime
from typing import Any

from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.BaseModels import Session, Component, CoreModel

from BaseModel import LayoutSettings, ResponseDataErr
from OzonModelApp import OzonModelApp

logger = logging.getLogger(__name__)


class OzonEnvApp:
    def __init__(self):
        self.env = OzonEnv(cls_model=OzonModelApp)
        self.session: Session = None

    def init_params(self, current_session_token="", session_is_api=False):
        self.env.params['current_session_token'] = current_session_token
        self.env.params['session_is_api'] = session_is_api

    async def new(self, current_session_token="", session_is_api=False) -> Any:
        await self.env.init_env()
        self.init_params(current_session_token, session_is_api)
        res = await self.env.session_app()
        if res.fail:
            return ResponseDataErr(message='Authentication error')
        for name, model in self.env.models.items():
            model.init_engine()
        self.session = self.env.user_session

    async def new_app(self, db=None) -> Any:
        await self.env.init_env(db=db)
        if not self.env.upload_folder:
            self.env.upload_folder = self.env.orm.app_settings.upload_folder

    async def update_session(self):
        session_m = self.env.get("session")
        return await session_m.update(self.session)

    async def insert_session(self):
        session_m = self.env.get("session")
        sess = await session_m.by_name(self.session.token)
        if not sess:
            self.session = await session_m.insert(self.session)
            if session_m.status.fail:
                logger.error(f"{session_m.status.msg}")
        return self.session

    async def app_run_session(
        self, current_session_token, session_is_api=False
    ):
        self.init_params(current_session_token, session_is_api)
        res = await self.env.session_app()
        self.session = self.env.user_session
        if res.fail:
            return res
        if (
            not self.session.is_public
            and self.session.expire_datetime < datetime.now()
        ):
            self.session.active = False
            await self.ozon.update_session()
            self.session = None
            return self.env.fail_response(
                _("Token %s expired") % self.session_token
            )
        return res

    async def app_init_session(self, token: str, is_api: bool):
        self.init_params(token, is_api)

    async def new_app_db(
        self, db, app_code="", current_session_token="", session_is_api=False
    ) -> Any:
        if app_code:
            self.env.app_code = app_code
        await self.env.init_env(db=db)
        self.init_params(current_session_token, session_is_api)
        res = await self.env.session_app()
        if res.fail:
            return ResponseDataErr(message='Authentication error')

    def get_layout_response(self) -> LayoutSettings:
        return LayoutSettings(
            module_name=self.env.orm.app_settings.rec_name,
            version=self.env.orm.app_settings.version,
            logo_img_url=self.env.orm.app_settings.logo_img_url,
        )

    async def component_schema(self, name) -> Component:
        componets: OzonModelApp = self.env.get("component")
        assert componets.user_session
        return await componets.by_name(name)

    async def component_schema_data(
            self, modelname, recname) -> (Component, CoreModel):
        componets: OzonModelApp = self.env.get("component")
        assert componets.user_session
        model: OzonModelApp = self.env.get(modelname)
        schema = await componets.by_name(modelname)
        data = await model.by_name(recname)
        user =  componets.orm.user_session
        return schema, data, user


    async def chek_token_valid(self, token) -> bool:
        session_model: OzonModelApp = self.env.get("session")
        query = session_model.default_domain
        query['token'] = token
        founds = await session_model.find(query)
        return len(founds) > 0
