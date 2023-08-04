import logging
import os
import uuid

from ozonenv.core.BaseModels import Session, default_fields

from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.services.DateEngine import DateEngine

logger = logging.getLogger(__name__)


class SessionMain:
    def __init__(self, ozon: OzonEnvApp):
        self.ozon: OzonEnvApp = ozon
        self.app_settings = self.ozon.env.orm.app_settings
        self.app_code = os.getenv("APP_CODE")
        self.session: Session = None
        self.token = ""
        self.user = {}

    async def init_public_session(self) -> Session:
        if not self.token:
            self.token = str(uuid.uuid4())
        self.user['uid'] = f"user.public"
        self.user['nome'] = "public"
        self.user['full_name'] = "Public User"
        self.uid = self.user.get("uid")
        dte = DateEngine()
        min, max = dte.gen_datetime_min_max_hours(
            max_hours_delata_date_to=self.app_settings.session_expire_hours
        )
        self.session = Session(
            rec_name=self.token,
            token=self.token,
            app_code=self.app_code,
            full_name="Public User",
            uid=self.uid,
            user=self.user.copy(),
            create_datetime=min,
            expire_datetime=max,
        )

        self.session.is_admin = False
        self.session.use_auth = False
        self.session.is_public = True
        await self.set_current_app()

        self.ozon.session = self.session
        self.ozon.env.user_session = self.session
        self.ozon.env.orm.user_session = self.session
        self.session = await self.ozon.insert_session()
        logger.info(
            f"** Session Auth Free---> name: {self.session.full_name} "
            f"token {self.session.token}"
        )
        return self.session

    async def make_session(
        self, user_info, token=None, parent_token=""
    ) -> Session:
        if not self.token:
            self.token = str(uuid.uuid4())
        dte = DateEngine()
        min, max = dte.gen_datetime_min_max_hours(
            max_hours_delata_date_to=self.app_settings.session_expire_hours
        )
        self.session = Session(
            rec_name=self.token,
            parent_token=parent_token,
            app_code=self.app_code,
            token=self.token,
            uid=user_info.get('uid'),
            user=user_info,
            create_datetime=min,
            expire_datetime=max,
        )

        await self.set_current_app()

        logger.info(
            f" App: {self.app_code} token: {self.token}"
            f" user: {user_info.get('uid')} "
        )

        self.ozon.session = self.session
        self.ozon.env.user_session = self.session
        self.ozon.env.orm.user_session = self.session
        self.session = await self.ozon.insert_session()
        return await self.ozon.insert_session()

    async def set_current_app(self):
        if self.app_settings.app_code not in list(self.session.apps.keys()):
            logger.info("reset App")
            await self.reset_app()
        logger.info(f"self.app_code {self.app_code}")
        if not self.app_code == self.session.app.get('app_code', ""):
            self.session.app = self.session.apps[self.app_code].copy()
            self.session.app_code = self.app_code
            self.session.app['settings'] = self.app_settings.get_dict()
            # self.session.app['save_session'] = False
            # if not self.session.is_public:
            self.session.app['save_session'] = True
            self.session.is_admin = (
                self.session.uid in self.app_settings.admins
            )

    async def reset_app(self):
        app_modes = ["standard"]
        if self.session.is_admin:
            app_modes = ["standard", "maintenance"]
        self.session.apps.update(
            {
                self.app_code: {
                    "modes": app_modes,
                    "app_code": self.app_code,
                    "mode": "standard",
                    "layout": "standard",
                    "action_model": "action",
                    "default_fields": default_fields[:],
                    "model_write_access": {},
                    "model_read_access": {},
                    "model_write_access_fields": {},
                    "fast_search": {},
                    "queries": {},
                    "settings": {},
                    "action_name": "",
                    "component_name": "",
                    "submissison_name": "",
                    "can_build": self.session.use_auth,
                    "builder": False,
                    "save_session": False,
                    "data": {},
                    "breadcrumb": {},
                }
            }
        )
