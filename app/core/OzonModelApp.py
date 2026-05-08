from datetime import datetime, timedelta
import logging
import uuid
from typing import Union
from zoneinfo import ZoneInfo

from ozonenv.core.BaseModels import CoreModel
from ozonenv.core.DateEngine import DateEngine
from ozonenv.core.OzonOrm import OzonModel, OzonOrm

from app.app_settings import EnvSettings
from app.core.session import AppSession

logger = logging.getLogger("uvicorn.error")

class DateEngineApp(DateEngine):

    def gen_datetime_delta_hours_from_now(self, deltat):
        return (datetime.now() + timedelta(hours=deltat)).replace(
            tzinfo=ZoneInfo("UTC")
        )

    def gen_datetime_min_max_hours(
        self, min_hours_delata_date_from=0, max_hours_delata_date_to=1
    ):
        min = self.gen_datetime_delta_hours_from_now(
            min_hours_delata_date_from
        )
        max = self.gen_datetime_delta_hours_from_now(
            max_hours_delata_date_to
        )
        return min, max

class OzonModelApp(OzonModel):
    def __init__(
        self,
        model_name,
        orm: OzonOrm,
        data_model="",
        session_model=False,
        virtual=False,
        static: CoreModel = None,
        schema={},
        app_settings: EnvSettings = None,
    ):
        super(OzonModelApp, self).__init__(
            model_name=model_name,
            orm=orm,
            data_model=data_model,
            virtual=virtual,
            static=static,
            schema=schema,
        )
        self.session_model = session_model
        # app_code is fixed by application settings and must be available on every model.
        self.app_code = str(
            getattr(self.setting_app, "app_code", "")
            or getattr(app_settings, "app_code", "")
            or ""
        )
        self.dte = DateEngineApp(TZ=self.setting_app.tz)

    async def update_create_session(
        self, record: CoreModel = None, user: dict = None
    ) -> Union[AppSession, None]:
        logger.info("update/create session start app_code=%s", self.app_code)
        if self.session_model:
            collection = self.db.engine.get_collection("user")
            session = record
            if user:
                self.token = str(uuid.uuid4())
                min_dt, max_dt = self.dte.gen_datetime_min_max_hours(
                    max_hours_delata_date_to=self.settings.session_expire_hours
                )
                session = AppSession(
                    token=self.token,
                    uid=user["uid"],
                    user=user.copy(),
                    create_datetime=min_dt,
                    expire_datetime=max_dt,
                    app_code=self.app_code,
                )
            session.last_update = datetime.now().timestamp()
            new_session = await collection.replace_one(
                {"uid": session.uid},
                session.model_dump(),
                upsert=True,
            )
            logger.info("update/create session completed app_code=%s", self.app_code)
            return new_session
        logger.warning("update/create session skipped session_model=False")
        return None
