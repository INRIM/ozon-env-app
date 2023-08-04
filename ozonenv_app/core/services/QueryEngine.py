# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import logging
import re
from typing import Any, Iterable

from ozonenv.core.BaseModels import Settings, Session

from ozonenv_app.core.services.DateEngine import DateEngine

logger = logging.getLogger(__name__)


class QueryEngine:
    def __init__(
            self, session: Session, app_settings: Settings, app_code: str
    ):
        self.session: Session = session
        self.app_settings: Settings = app_settings
        self.app_code = app_code
        self.dte = DateEngine(
            SERVER_DTTIME_MASK=self.app_settings.server_datetime_mask
        )
        # for dt --> 2021-08-11T17:22:04
        self.isodate_regex = re.compile(
            '(\d{4}-\d{2}-\d{2})[A-Z]+(\d{2}:\d{2}:\d{2})'
        )
        self.autodate_parser = {
            "year": lambda y=0: self.dte.year_range(year=y),
            "month": lambda y=0, m=0, me=0: self.dte.month_range(
                year=y, month=m, monthe=me
            ),
            "today": lambda d=0: self.dte.today(days=d),
            "now": lambda: self.dte.now,
        }
        # for dt --> 2021-08-11T17:22:04.51+01:00

    def get_today_first_last(self):
        return {}

    def get_now(self):
        return {}

    def _check_update_date(self, obj_val: Any):
        if not isinstance(obj_val, str):
            return obj_val
        if self.isodate_regex.match(obj_val):
            val = self.dte.strdatetime_server_to_datetime(obj_val)
            return val
        elif "_date_" in obj_val:
            val = self.dte.eval_date_filter(obj_val)
            return val
        else:
            return obj_val

    def _check_update_user(self, obj_val: str) -> Any:
        if not isinstance(obj_val, str):
            return obj_val
        if "_user_" in obj_val:
            x = obj_val.replace("_user_", "")
            return getattr(self.session, x)
        else:
            return obj_val

    def get_query_date(self, strcode):
        pass

    def _check_update_auto_date(self, obj_val: Any):
        """
        :param obj_val: possible config
            year  --> return range current year
            year-2020  --> return range for year 2020
            month  --> return range current year and current month
            month-6  --> return today date after 6 month
            month-1-0  --> return range current year for January
            month-1-3  --> return range current year frm 1st January  and 31st March
            month-1-3-2020  --> return range frm 1st January  and 31st March 2020
            today --> return date today at 00:00:00 (TZ)
            today-1 --> return date tommorrow at 00:00:00 (TZ)
            today_n_1 --> return  n means negative date yesterday at 00:00:00 (TZ)
            now --> return date today at this tick time (TZ)
        :return: date range or date objects
        """
        if not isinstance(obj_val, str):
            return obj_val
        if "_date_" in obj_val:
            # logger.info(f" render {obj_val}")
            x = obj_val.replace("_date_", "")
            return getattr(self.session, x)  # self.session.get(x, "")
        else:
            return obj_val

    def update_query(self, data: Any) -> dict:
        if isinstance(data, dict):
            for k, v in copy.deepcopy(data).items():
                if isinstance(v, dict):  # For DICT
                    data[k] = self.update_query(v)
                elif isinstance(v, list):  # For LIST
                    data[k] = [self.update_query(i) for i in v]
                else:  # Update Key-Value
                    data[k] = self.update_query(v)
                    # logger.info(f"updated data[k] {data}")
        else:
            # update metakey to set correct data like user, date etc..
            data = self._check_update_user(data)
            return data
        return data.copy()

    def scan_find_key(self, data: dict, key: str) -> list:
        res = []
        if isinstance(data, dict):
            for k, v in data.items():
                res.append(k == key)
                if isinstance(v, dict):  # For DICT
                    res.append(self.scan_find_key(v, key))
                elif isinstance(v, list):  # For LIST
                    for i in v:
                        res.append(self.scan_find_key(i, key))
        return res[:]

    def flatten(self, lst: list) -> Iterable:
        for item in lst:
            try:
                yield from self.flatten(item)
            except TypeError:
                yield item

    def check_key(self, data: dict, key: str) -> bool:
        res_l = self.scan_find_key(data, key)
        res_flat = list(self.flatten(res_l))
        try:
            i = res_flat.index(True)
            return True
        except ValueError:
            return False

    async def default_query(
            self,
            model_name: str,
            query: dict,
            parent: str = "",
            model_type: str = "",
    ) -> dict:
        if model_name.lower() in ["menu_group"] and self.app_code:
            query.update(
                {
                    "$or": [
                        {'apps': {'$in': [self.app_code]}},
                        {'apps': []},
                        {'apps': None},
                    ]
                }
            )

        if not self.check_key(query, "deleted"):
            query.update({"deleted": 0})

        if not self.check_key(query, "active"):
            query.update({"active": True})

        if not self.check_key(query, "parent") and parent:
            query.update({"parent": {"$eq": parent}})

        if not self.check_key(query, "type") and model_type:
            query.update({"type": {"$eq": model_type}})

        q = self.update_query(query)
        return q
