import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ozonenv.core.BaseModels import CoreModel
from ozonenv.core.DateEngine import DateEngine
from ozonenv.core.OzonOrm import OzonModel, OzonOrm

from app.app_settings import AppSettings
from app.app_settings import EnvSettings


_RELATIVE_OFFSET_TOKEN_RE = re.compile(r"([+-])(\d+(?:\.\d+)?)([a-zA-Z]+)")
_RELATIVE_OFFSET_UNITS = {
    "s": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "second": "seconds",
    "seconds": "seconds",
    "m": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "minute": "minutes",
    "minutes": "minutes",
    "h": "hours",
    "hr": "hours",
    "hrs": "hours",
    "hour": "hours",
    "hours": "hours",
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
}


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

    def resolve_relative_expr(self, expr: str) -> datetime | None:
        """
        Risolve espressioni relative del tipo "now", "now-3h", "now+3d-3h"
        in un datetime UTC aware, applicando gli offset in sequenza da
        sinistra a destra. Unita' supportate: s(econds), m(inutes), h(ours),
        d(ays), w(eeks). Ritorna None se l'espressione non e' valida (cosi'
        il chiamante puo' decidere se applicare un default o ignorarla).
        """
        normalized = str(expr or "").strip()
        if not normalized.startswith("now"):
            return None
        rest = normalized[3:]
        result = datetime.now(ZoneInfo("UTC"))
        if not rest:
            return result
        consumed = 0
        for match in _RELATIVE_OFFSET_TOKEN_RE.finditer(rest):
            consumed += len(match.group(0))
            sign, amount, unit = match.groups()
            unit_key = _RELATIVE_OFFSET_UNITS.get(unit.lower())
            if unit_key is None:
                return None
            delta = timedelta(**{unit_key: float(amount)})
            result = result + delta if sign == "+" else result - delta
        if consumed != len(rest):
            return None
        return result


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
