from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ozonenv.core.BaseModels import CoreModel
from ozonenv.core.DateEngine import DateEngine
from ozonenv.core.OzonOrm import OzonModel, OzonOrm

from app.app_settings import AppSettings
from app.app_settings import EnvSettings


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
        app_settings: AppSettings | EnvSettings | None = None,
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
