# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import copy
import sys
import os
from ozonenv.core.cache.cache import get_cache
from ozonenv_app.core.BaseModel import *
from ozonenv_app.core.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.ServiceModelSecurity import ServiceModelSecurity
from ozonenv.core.BaseModels import (
    default_list_metadata_fields,
    Settings,
    Session,
    CoreModel,
)
import logging
import httpx
import uuid

logger = logging.getLogger(__name__)


class ServiceMenuManager:
    btn_action_parser = {
        "save": "post",
        "copy": "post",
        "delete": "post",
        "window": False,
    }

    def __init__(self, app: OzonEnvApp):
        self.app: OzonEnvApp = app
        self.env = app.env
        self.app_settings: Settings = self.env.orm.app_settings
        self.session: Session = self.app.session
        self.app_code = self.env.app_code

        self.contextual_buttons = []
        self.contextual_actions = []
        self.action = None
        self.action_model = None
        self.acl: ServiceModelSecurity = ServiceModelSecurity(self.app)

    async def get_menu(self):
        if self.session.app.get('nemu'):
            return self.session.app.get('nemu').copy()

    async def make_query_user(self, base_query=[]):
        user_query = await self.acl.make_user_action_query()
        pre_list = base_query
        if user_query:
            and_list = pre_list + user_query
        else:
            and_list = pre_list[:]
        return and_list

    async def get_basic_menu_list(self, admin=False, parent=""):
        menu_group_model: OzonModelApp = await self.env.get("menu_group")
        self.action_model: OzonModelApp = await self.env.get("action")

        menu_grops_list = await menu_group_model.search_base(
            query=await menu_group_model.default_query(
                {"$and": [{"admin": admin}, {"parent": parent}]}
            )
        )
        menu_list = []
        model_done = []
        for i in menu_grops_list:
            found_item = await self.action_model.search_base(
                query=await self.action_model.default_query(
                    {
                        "$and": await self.make_query_user(
                            [{"menu_group": i.rec_name}]
                        )
                    }
                )
            )
            if found_item:
                if f"{i.rec_name}{found_item[0].model}" not in model_done:
                    model_done.append(f"{i.rec_name}{found_item[0].model}")
                    menu_list.append(
                        {
                            "model": found_item[0].model,
                            "menu_group": i.rec_name,
                            "label": i.label,
                        }
                    )
            else:
                sub_menus = await menu_group_model.search_base(
                    query=await menu_group_model.default_query(
                        {"$and": [{"deleted": 0}, {"parent": i['rec_name']}]}
                    )
                )
                if sub_menus:
                    number = len(sub_menus)
                    sub_menu_groups = [s.rec_name for s in sub_menus]
                    sub_menu_items = await self.action_model.search_base(
                        query=await self.action_model.default_query(
                            {
                                "$and": await self.make_query_user(
                                    [
                                        {"deleted": 0},
                                        {
                                            "menu_group": {
                                                "$in": sub_menu_groups
                                            }
                                        },
                                    ]
                                )
                            }
                        )
                    )
                    if sub_menu_items:
                        menu_list.append(
                            {
                                "model": False,
                                "menu_group": i.rec_name,
                                "label": i.label,
                                "dashboard": True,
                                "content": f"/dashboard/{i.rec_name}",
                                "action_type": "window",
                                "mode": "list",
                                "number": number,
                                "icon": "it-folder",
                            }
                        )

        return menu_list[:]

    async def make_main_menu(self) -> list:
        logger.debug(f"make_main_menu only admin")
        if not self.session.is_admin:
            return [{}]
        self.action_model: OzonModelApp = await self.env.get("action")
        menu_group_model: OzonModelApp = await self.env.get("menu_group")
        menu_grops_list = await menu_group_model.search_base(
            query=await menu_group_model.default_query({"admin": True})
        )
        self.contextual_buttons = await self.make_buttons(
            menu_grops_list, group_by_field="mode"
        )
        logger.debug(f"make_main_menu - > Done")
        return self.contextual_buttons[:]

    async def make_menu_item(self, card, rec_b):
        card_btn = BaseClass(**rec_b)
        has_model_access = (
            card['model'] in self.session.app['model_write_access']
        )
        writable = card_btn.write_access
        add = True
        if writable and has_model_access:
            add = self.session.app['model_write_access'].get(card['model'])
        cc_model = await self.mdata.gen_model(card_btn.model)
        if add and cc_model:
            if card_btn.mode:
                link = f"{card_btn.action_root_path}/{card_btn.rec_name}"
            else:
                link = f"{card_btn.action_root_path}"
            number = 0
            if card_btn.mode == "list":
                # list_query
                list_query = {}
                if cc_model:
                    if card_btn.list_query:
                        list_query = ujson.loads(card_btn.list_query)
                    q = await self.qe.default_query(cc_model, list_query)
                    number = await self.mdata.count_by_filter(cc_model, q)
            return {
                "model": card_btn.model,
                "icon": card_btn.button_icon,
                "action_type": card_btn.action_type,
                "content": link,
                "label": card_btn.title,
                "mode": card_btn.mode,
                "number": number,
            }.copy()
        return False

    async def make_dashboard_menu(self, parent=""):
        logger.debug(f"make_dashboard_menu {parent}")
        menu_list = await self.get_basic_menu_list(parent=parent)
        list_cards = []
        group = {}
        for card in menu_list:
            if card['model']:
                c_model = await self.mdata.gen_model(card['model'])
                if c_model:
                    q_menu_user = await self.make_query_user(
                        [
                            {"action_type": "menu"},
                            {
                                "component_type": {
                                    '$in': ["form", "resource", "layout"]
                                }
                            },
                            {"$and": [{"menu_group": card['menu_group']}]},
                        ]
                    )

                    q_user = await self.make_query_user(
                        [
                            {"action_type": "window"},
                            {
                                "component_type": {
                                    '$in': ["form", "resource", "layout"]
                                }
                            },
                            {"$and": [{"menu_group": card['menu_group']}]},
                        ]
                    )

                    q_menu = await self.qe.default_query(
                        self.action_model, {"$and": q_menu_user}
                    )
                    q = await self.qe.default_query(
                        self.action_model, {"$and": q_user}
                    )

                    menu_list = await self.mdata.get_list_base(
                        self.action_model, query=q_menu
                    )
                    act_list = await self.mdata.get_list_base(
                        self.action_model, query=q
                    )
                    # logger.info(f"act_list: {act_list}")
                    card_buttons = []

                    for rec_b in menu_list:
                        item = await self.make_menu_item(card, rec_b.copy())
                        if item:
                            card_buttons.append(item)

                    for rec_b in act_list:
                        item = await self.make_menu_item(card, rec_b.copy())
                        if item:
                            card_buttons.append(item)

                    card_m = {
                        "model": card['model'],
                        "group_id": card['menu_group'],
                        "title": card['label'],
                        "buttons": card_buttons,
                    }
                    list_cards.append(card_m)
            else:
                card_m = {
                    "model": card['menu_group'],
                    "group_id": card['menu_group'],
                    "title": card['label'],
                    "buttons": [card.copy()],
                }
                list_cards.append(card_m)

        logger.debug(f"make_dashboard_menu - > Done")

        return list_cards[:]

    async def make_action_buttons(self, list_actions, rec_name=""):
        logger.debug(f"make_action_buttons -> {len(list_actions)} items")
        list_buttons = []
        group = {}
        for rec in list_actions:
            if isinstance(rec, dict):
                item = BaseClass(**rec)
            else:
                item = rec
            rec_name_action = item.rec_name
            writable = item.write_access
            has_model_access = (
                item.model in self.session.app['model_write_access']
            )
            add = True
            if writable and has_model_access:
                add = self.session.app['model_write_access'].get(item.model)
            if add:
                if rec_name:
                    rec_name_action = rec_name
                btn_action_type = self.btn_action_parser.get(item.action_type)

                if item.action_type in self.btn_action_parser:
                    url_action = f"{item.action_root_path}/{item.rec_name}/{rec_name_action}"
                else:
                    url_action = f"{item.action_root_path}/{rec_name_action}/{item.rec_name}"

                if (
                    item.rec_name == rec_name_action
                    or item.action_type not in self.btn_action_parser
                ):
                    url_action = f"{item.action_root_path}/{rec_name_action}"
                    # TODO case item.rec_name == rec_name_action
                    # TODO is new element and need to be save before run other action type
                    # TODO exlude button type:  delete, copy, update, print, export, ecc...
                button = {
                    "model": item.model,
                    "key": item.rec_name,
                    "type": "button",
                    "label": item.title,
                    "leftIcon": item.button_icon,
                    "authtoken": self.session.token,
                    "req_id": self.session.req_id,
                    "btn_action_type": self.btn_action_parser.get(
                        item.action_type
                    ),
                    "action_type": item.action_type,
                    "url_action": url_action,
                    "builder": item.builder_enabled,
                }

                list_buttons.append(button)

        if not list_buttons:
            list_buttons.append(group)
        return list_buttons

    async def make_button_main_menu(self, item: CoreModel):
        rec_name_action = item.rec_name
        # btn_action_type = self.btn_action_parser.get(item.action_type)

        if item.action_type in self.btn_action_parser:
            url_action = (
                f"{item.action_root_path}/{item.rec_name}/{rec_name_action}"
            )
        else:
            url_action = (
                f"{item.action_root_path}/{rec_name_action}/{item.rec_name}"
            )

        if (
            item.rec_name == rec_name_action
            or item.action_type not in self.btn_action_parser
        ):
            url_action = f"{item.action_root_path}/{rec_name_action}"

        button = {
            "model": item.model,
            "key": item.rec_name,
            "type": "button",
            "label": item.title,
            "leftIcon": item.button_icon,
            "authtoken": self.session.token,
            "req_id": self.session.req_id,
            "btn_action_type": self.btn_action_parser.get(item.action_type),
            "url_action": url_action,
            "builder": item.builder_enabled,
        }
        return button

    async def make_buttons(self, list_actions, group_by_field="", rec_name=""):
        logger.debug(f"make_buttons list_actions -> {len(list_actions)} items")
        list_buttons = []
        group = {}
        mg_done = []
        for mnu in list_actions:
            q_menu = await self.make_query_user(
                [{"action_type": "menu"}, {"menu_group": mnu.rec_name}]
            )
            q_menud = await self.action_model.default_query({"$and": q_menu})

            menu_list = await self.action_model.search_base(query=q_menud)
            if menu_list:
                val = mnu.label
                if not val:
                    val = "No Menu"
                if not group.get(val):
                    group[val] = []
                for rec_item in menu_list:
                    button = await self.make_button_main_menu(rec_item)
                    group[val].append(button)

        if not list_buttons:
            list_buttons.append(group)
        return list_buttons
