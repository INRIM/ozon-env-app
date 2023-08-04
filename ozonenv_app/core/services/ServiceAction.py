# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import copy
import sys
import os
from ozonenv.core.cache.cache import get_cache
from ozonenv_app.core.Codec import check_parse_json, encode_dataj
from ozonenv_app.core.BaseModel import *
from ozonenv_app.core.ServiceMain import OzonModelApp
from ozonenv_app.core.ServiceMenuManager import ServiceMenuManager
from ozonenv_app.core.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.ServiceModelSecurity import ServiceModelSecurity
from ozonenv.core.BaseModels import (
    default_list_metadata_fields,
    Settings,
    Session,
    CoreModel,
    Component,
)
import logging
import httpx
import json
import uuid

logger = logging.getLogger(__name__)


class ServiceAction:
    def __init__(
        self,
        app: OzonEnvApp,
        action_name: str,
        rec_name: str,
        parent: str,
        iframe: str,
        execute: bool,
        container_act: str = "",
    ):
        self.app: OzonEnvApp = app
        self.env = app.env
        self.app_settings: Settings = self.app.orm.app_settings
        self.session: Session = self.app.session
        self.app_code = self.app.app_code

        self.action_name = action_name
        self.curr_ref = rec_name
        self.parent = parent
        self.iframe = iframe
        self.execute = execute
        self.container_action = container_act
        self.data_model: OzonModelApp = None

        self.action_model: OzonModelApp = None
        self.component_type = ""
        self.action_query = {}
        self.models_query = {}
        self.data_model_query = {}

        self.computed_fields = {}
        self.model = None
        self.record = None
        self.action = None
        self.next_action = None
        self.ref_name = ""
        self.ref = ""
        self.contextual_actions = []
        self.contextual_buttons = []
        self.fast_search_model: CoreModel
        self.fast_search = {}
        self.fast_config = {}
        self.acl: ServiceModelSecurity = ServiceModelSecurity(self.app)
        self.menu_manager = ServiceMenuManager(self.app)
        self.compo_model: OzonModelApp = self.env.get('component')
        self.action_model: OzonModelApp = self.env.get('action')

    async def get_param(self, name: str) -> Any:
        return await get_param(name)

    # helper
    async def get_builder_config(self):
        if (
            self.action.builder_enabled
            and self.mode == "component"
            and self.action.mode == "form"
        ):
            return {
                "page_api_action": f"/action/{component_type}/formio_builder/"
            }
        else:
            return {}

    async def eval_context_button_query(self):
        builder_active = self.action.builder_enabled
        user_query = await self.acl.make_user_action_query()
        model = self.action.model
        if self.action.view_name not in ["", self.action.model]:
            model = self.action.view_name
        pre_list = [
            {"model": {"$eq": model}},
            {"context_button_mode": {"$elemMatch": {"$eq": self.action.mode}}},
            {"builder_enabled": {"$eq": self.action.builder_enabled}},
        ]
        if user_query:
            and_list = pre_list + user_query
        else:
            and_list = pre_list[:]
        if self.action.component_type:
            and_list.append(
                {"component_type": {"$eq": self.action.component_type}},
            )
        rel_name = self.aval_related_name()
        if builder_active:
            if rel_name:
                and_list.append(
                    {"ref": {"$exists": True, "$ne": ""}},
                )
            else:
                if self.action.mode == "list":
                    and_list.append(
                        {"ref": {"$in": ["", "self"]}},
                    )
                else:
                    and_list.append(
                        {"ref": {"$in": [""]}},
                    )

        query = {"$and": and_list}
        return query.copy()

    async def make_context_button(self):
        logger.info(f"make_context_button object model: {self.action_model}")
        if self.action.model:
            query = await self.action_model.default_query(
                await self.eval_context_button_query()
            )
            self.contextual_actions = await self.action_model.search_base(
                query=query
            )
            self.contextual_buttons = (
                await self.menu_manager.make_action_buttons(
                    self.contextual_actions, rec_name=self.curr_ref
                )
            )
        logger.debug(
            f"Done make_context_button  object model: {self.action_model} of "
            f"{len(self.contextual_buttons)} items"
        )

    async def eval_editable(self, model_schema: OzonModelApp, data) -> bool:
        can_edit = False
        if isinstance(data, CoreModel):
            can_edit = await self.acl.can_update(model_schema, data)
        elif isinstance(data, list):
            can_edit = not self.session.is_public
        return can_edit

    async def eval_delete(self, model_schema: OzonModelApp, data) -> bool:
        can_edit = await self.acl.can_delete(model_schema, data)

        return can_edit

    async def eval_editable_fields(
        self, model_schema: OzonModelApp, data
    ) -> list:
        fields = []
        if isinstance(data, BasicModel):
            fields = await self.acl.can_update_fields(model_schema, data)
        return fields

    async def eval_editable_and_context_button(
        self, model_schema: OzonModelApp, data
    ) -> bool:
        can_edit = await self.eval_editable(model_schema, data)
        if can_edit:
            await self.make_context_button()
        return can_edit

    def aval_related_name(self) -> str:
        related_name = self.curr_ref
        if not self.action.ref == "self" and not related_name:
            related_name = str(self.action.ref)
        logger.info(
            f"{self.curr_ref}, action.ref: {self.action.ref} ->  {related_name}"
        )

        return related_name

    async def compute_action_path(
        self, record: CoreModel = None, related_name=""
    ) -> str:
        act_path = f"{self.action.action_root_path}"
        if self.action.rec_name:
            act_path = f"{self.action.action_root_path}/{self.action.rec_name}"
        if self.next_action and self.next_action.rec_name:
            act_path = (
                f"{self.next_action.action_root_path}/"
                f"{self.next_action.rec_name}"
            )
        if self.action.ref:
            if self.action.ref == "self":
                if record and record.rec_name:
                    act_path = f"{act_path}/{record.rec_name}"
            else:
                act_path = f"{act_path}/{self.action.ref}"

        if self.action.parent and not self.action.ref:
            if (
                self.action.parent == "parent"
                and record
                and hasattr(record, self.action.parent)
            ):
                parent_field = getattr(record, self.action.parent)
                act_path = f"{act_path}"
                if parent_field:
                    act_path = f"{act_path}/{parent_field}"
            else:
                act_path = f"{act_path}/{self.action.parent}"

        if self.action.keep_filter:
            act_path = f"{act_path}?container_act=y"
        return act_path

    def eval_builder_active(self, related_name="") -> bool:
        logger.info("eval_builder_active")
        builder = self.action.builder_enabled
        if self.action.mode == "form" and self.action.type == "data":
            builder = False
        return builder

    def prepare_list_query(self, data: dict, data_model_name) -> dict:
        q = {}
        if (
            not self.action.action_type == "menu"
            or not self.container_action == "s"
        ):
            sess_query = self.session.app.get('queries').get(data_model_name)
        else:
            self.session.app.get('queries')[data_model_name] = {}
            sess_query = {}
        list_query = {}
        if self.action.list_query:
            list_query = json.loads(self.action.list_query)
        session_q = {}
        if self.container_action and sess_query:
            session_q = json.loads(sess_query)
        q = {**session_q, **list_query}

        if data.get("query") and not data.get("query") == "clean":
            data_q = data.get("query")
            if isinstance(data_q, dict) or isinstance(data, list):
                parsed_q = data_q
            elif isinstance(data_q, str):
                parsed_q = check_parse_json(data_q)
            query = {**q, **parsed_q}
        else:
            query = q
        return query.copy()

    async def eval_computed_fields(
        self, data: CoreModel, eval_todo=True
    ) -> CoreModel:
        for k, v in self.computed_fields.items():
            if hasattr(self, v):
                mtd = getattr(self, v)
                if v == "eval_data":
                    data = await mtd(data, eval_todo=eval_todo)
                if v == "eval_user_todo":
                    data = await mtd(data, eval_todo=eval_todo)
                else:
                    data = await mtd(data)
        return data

    async def eval_data(self, data: CoreModel, eval_todo=True) -> CoreModel:
        return data

    # actions
    async def compute_action(self, data: dict) -> BaseModel:
        logger.info(
            f"compute_action -> act_name:{self.action_name}, data keys:{data.keys()}"
        )
        res = ResponseData(**{"menu": [], "content": {}})
        self.fast_search_model: OzonModelApp = await self.env.get(
            'fast_search_config'
        )
        self.action = await self.action_model.by_name(self.action_name)
        if not self.action:
            logger.error(
                f"No action found forn act_name: {self.action_name} "
                f"model: {self.action_model}"
            )
        if not self.action or self.action.admin and self.session.is_public:
            return ResponseData(
                **{
                    "action": "redirect",
                    "url": "/login/",
                }
            )
        can_read = await self.acl.can_read(self.action)
        if not can_read:
            return ResponseData(
                **{
                    "action": "redirect",
                    "url": "/",
                }
            )
        self.model = self.action.model
        self.next_action = await self.action_model.by_name(
            self.action.next_action_name
        )
        logger.info(f"Call method -> {self.action.action_type}_action")
        logger.info(f"Next action -> {self.action.next_action_name}")
        try:
            res.content = await getattr(
                self, f"{self.action.action_type}_action"
            )(data=data)
            return res
        except ValidationError as e:
            logger.error(str(e))
            logger.error(f"data: {data}")
            return ResponseDataErr(**{"model": self.model, "message": str(e)})
        except RuntimeError as e:
            return ResponseDataErr(**{"model": self.model, "message": str(e)})

    async def eval_list_mode(
        self, related_name, data_model_name, data: dict
    ) -> BaseModel:
        logger.debug(
            f"eval_list_mode action_model: {self.action.model}, "
            f"data_model: {self.data_model},"
            f" component_type: {self.component_type}, "
            f"related_name: {related_name}, data:{data}"
            f" data_model_name: {data_model_name}, "
            f"container_act: {self.container_action}"
        )
        await self.make_context_button()

        action_url = f"{self.action.action_root_path}/{self.action.rec_name}"
        logger.debug(f"List context Actions  {len(self.contextual_buttons)}")
        act_path = await self.compute_action_path()

        fields = []
        list_data = []
        merge_field = ""
        schema_sort = {}
        schema = None
        can_edit = False
        # TODO check this
        if (
            self.action.model == "component"
            and self.data_model == Component
            and not related_name
        ):
            model_schema = await self.compo_model.search(
                query=await self.compo_model.default_query(
                    {"type": self.component_type}
                )
            )
            if model_schema:
                schema = model_schema[0]
                fields = [
                    "row_action",
                    "title",
                    "type",
                    "display",
                    "projectId",
                    "properties",
                ]
        else:
            # TODO check this
            if (
                self.action.model == "component"
                and related_name
                and self.component_type
            ):
                schema = await self.compo_model.by_name(related_name)
            else:
                schema = await self.compo_model.by_name(self.action.model)
                if self.action.view_name and self.action.view_name not in [
                    self.action.model
                ]:
                    schema = await self.compo_model.by_name(
                        self.action.view_name
                    )
                # schema_sort = schema.properties.get("sort")

        # logger.info(schema_sort)
        if not data.get("sort") and schema_sort:
            data['sort'] = schema.properties.get("sort")
        sortstr = data.get("sort")

        if not sortstr:
            sortstr = self.data_model.default_sort_str
        sort = self.data_model.eval_sort_str(sortstr)

        limit = data.get("limit", 0)
        skip = data.get("skip", 0)

        query = self.prepare_list_query(data, data_model_name)

        query = await self.data_model.default_query(
            query, parent=self.action.parent, model_type=self.component_type
        )

        self.session.app.get('queries')[data_model_name] = encode_dataj(query)

        if self.container_action:
            action_url = f"{action_url}?container_act=y"
            self.session.app['breadcrumb'][action_url] = self.action.title
        else:
            self.session.app['breadcrumb'] = {}
        if self.execute:
            list_data = await self.data_model.search(
                query=query,
                sort=sort,
                limit=limit,
                skip=skip,
                row_action=act_path,
            )

        recordsTotal = await self.data_model.count_by_filter(query)

        can_edit = await self.eval_editable_and_context_button(
            schema, list_data
        )

        self.session.app['action_name'] = self.action.rec_name
        self.session.app['curr_model'] = self.action.model
        self.session.app['curr_schema'] = schema.schema()
        self.session.app['act_builder'] = self.action.builder_enabled
        self.session.app['component_type'] = self.component_type

        return DataResponse(
            **{
                "editable": can_edit,
                "context_buttons": self.contextual_buttons[:],
                "mode": self.action.mode,
                "query": query,
                "is_domain_query": self.action.force_domain_query,
                "limit": limit,
                "skip": skip,
                "sort": sortstr,
                "recordsTotal": recordsTotal,
                "recordsFiltered": recordsTotal,
                "data": list_data[:],
                "schema": schema.schema(),
                "action_url": action_url,
                "action_name": self.action.rec_name,
                "related_name": related_name,
                "builder": self.action.builder_enabled,
                "component_type": self.component_type,
                "model": self.action.model,
                "title": self.action.title,
                "fast_search": self.fast_config.copy(),
            }
        )

    async def eval_form_mode(
        self, related_name, data_model_name, data={}
    ) -> BaseModel:
        builder_active = self.eval_builder_active()
        logger.info(
            f"eval_form_mode Name:{self.action.rec_name},  Model:{self.action.model}, Data Model: {self.data_model},"
            f"action_type:{self.action.type}, action_name: {self.action_name}, related: {self.curr_ref}, "
            f"default: {self.action.ref}, related_name: {related_name}, builder: {builder_active}, "
            f"view_name: {self.action.view_name}"
        )
        fields = []
        model_schema: OzonModelApp = False
        schema: OzonModelApp = False
        model_data = self.action.model
        view_model_schema: OzonModelApp = None
        if self.action.model == "component":
            if not self.action_model == self.data_model:
                model_schema = self.env.get(self.curr_ref)
            else:
                model_schema = self.env.get(related_name)
        else:
            model_schema = self.env.get(self.action.model)
            if self.action.view_name and self.action.view_name not in [
                self.action.model
            ]:
                view_model_schema = self.env.get(self.action.view_name)
                model_data = self.action.view_name
        schema = view_model_schema if view_model_schema else model_schema

        data: CoreModel = await schema.load({"rec_name": related_name})

        can_edit = await self.eval_editable_and_context_button(schema, data)
        fields = await self.eval_editable_fields(schema, data)

        action_url = await self.compute_action_path(data)

        if not self.parent:
            self.session.app['mode'] = self.action.mode
            self.session.app['curr_model'] = model_data
            self.session.app['curr_schema'] = schema.schema
            self.session.app['act_builder'] = builder_active
            self.session.app['component_type'] = self.component_type
            self.session.app['child'] = []
        else:
            self.session.app[self.action.rec_name] = {}
            self.session.app['child'].append(self.action.rec_name)
            self.session.app[self.action.rec_name]['mode'] = self.action.mode
            self.session.app[self.action.rec_name]['curr_model'] = model_data
            self.session.app[self.action.rec_name]['curr_schema'] = schema
            self.session.app[self.action.rec_name][
                'act_builder'
            ] = builder_active
            self.session.app[self.action.rec_name][
                'component_type'
            ] = self.component_type

        res = {
            "editable": can_edit,
            "editable_fields": fields,
            "context_buttons": self.contextual_buttons[:],
            "action_name": self.action.rec_name,
            "mode": self.action.mode,
            "schema": encode_dataj(schema.schema),
            "data": encode_dataj(data.get_dict_json()) if data else {},
            "builder": builder_active,
            "component_type": self.component_type,
            "model": model_data,
            "title": self.action.title,
            "action_url": action_url,
            "rec_name": related_name,
        }

        if self.action.builder_enabled and not self.iframe:
            builder_action = (
                f"{self.action.action_root_path}/{self.action_name}"
            )
            if self.curr_ref:
                builder_action = f"{builder_action}/{self.curr_ref}"
            res["builder_api_action"] = builder_action
        return DataResponse(**res)

    async def eval_fast_search(self, query):
        list_fast_search = await self.fast_search_model.search_base(
            query=await self.fast_search_model.default_query(query)
        )
        if list_fast_search:
            data_fast_search = list_fast_search[0]
            if form_fast_search:
                form_search_schema = await self.compo_model.by_name(
                    fdata_fast_search.searchForm
                )
                self.fast_config = {
                    "model": self.action.model,
                    "schema": form_search_schema.schema(),
                    "fast_serch_model": data_fast_search.searchForm,
                    "data": {},
                }

    async def window_action(self, data={}) -> BaseModel:
        logger.debug(
            f"window_action -> {self.action.model}, "
            f"action_type {self.action.type},"
            f" component_type: {self.action.component_type}, "
            f"mode: {self.action.mode}"
        )
        related_name = self.aval_related_name()
        await self.eval_fast_search({"model": self.action.rec_name})
        if self.action.type == "component":
            # get Schema
            logger.info(
                f'Make Model Component: -> {self.action.model} | action type Component: -> {self.action.type}'
            )
            self.data_model = await self.mdata.gen_model(self.action.type)
            data_model_name = self.action.type
            self.component_type = self.action.component_type
        else:
            # get Data
            if self.action.model == "component" and related_name:
                self.data_model = await self.env.get(related_name)
                self.component_type = self.action.component_type
                data_model_name = related_name
                related_name = ""
            else:
                self.data_model = await self.env.get(self.action.model)
                data_model_name = self.action.model
                self.component_type = ""

        logger.info(f'Data Model: -> {self.data_model}')

        return await getattr(self, f"eval_{self.action.mode}_mode")(
            related_name, data_model_name, data=data
        )

    async def menu_action(self, data={}) -> BaseModel:
        logger.info(
            f"menu_action -> {self.action.model} action_type {self.action.type}"
        )
        related_name = self.aval_related_name()
        await self.eval_fast_search({"model": self.action.rec_name})
        if self.action.type == "component":
            self.data_model = await self.env.get(self.action.type)
            self.component_type = self.action.component_type
            data_model_name = self.action.type
        else:
            self.data_model = await self.env.get(self.action.model)
            self.component_type = ""
            data_model_name = self.action.model

        return await getattr(self, f"eval_{self.action.mode}_mode")(
            related_name, data_model_name, data=data
        )

    async def save_copy_component(self, data={}, copy=False):
        logger.debug(f"copy: {copy}")
        if copy:
            record = await self.compo_model.copy(
                {"rec_name": data.get('rec_name')}
            )
        else:
            record = await self.env.insert_update_component(data)

        return self.compo_model.handle_return(record)

    async def before_save(
        self, record: CoreModel, copy: bool = False, update: bool = False
    ) -> CoreModel:
        return record

    async def after_save(
        self, record: CoreModel, copy: bool = False, update: bool = False
    ) -> CoreModel:
        return record

    async def save_action(self, data={}) -> BaseModel:
        logger.info(
            f"save_action -> model:{self.action.model} "
            f"action_type:{self.action.type}, curr_ref:{self.curr_ref}"
        )
        reload = True
        model_schema = False
        update = False

        if self.action.model == "component":
            record = await self.save_copy_component(data=data)

            can_edit = await self.eval_editable(self.compo_model, record)
            if not can_edit:
                logger.error(f"Accesso Negato {record_data.rec_name}")
                return ResponseDataErr(
                    message=f"Accesso Negato {record_data.rec_name}",
                    model=self.compo_model.model,
                    rec_name=record_data.rec_name,
                )

            if isinstance(record_res, ResponseDataErr):
                return record

            logger.info("make auto actions for model")
            await self.mdata.make_default_action_model(record)

            await self.check_and_create_task_action(record)
        else:
            if self.action.view_name and self.action.view_name not in [
                self.action.model
            ]:
                self.data_model: OzonModelApp = await self.env.get(
                    self.action.view_name
                )
            else:
                self.data_model: OzonModelApp = await self.env.get(
                    self.action.model
                )

            if self.curr_ref and not data.get('rec_name'):
                data['rec_name'] = self.curr_ref

            rec = await self.data_model.count({"rec_name": data['rec_name']})
            if rec > 0:
                update = True

            record = await self.data_model.new(data)

            if self.data_model.status.fail:
                return self.data_model.response_err()

            can_edit = await self.eval_editable(self.data_model, record)
            if not can_edit:
                logger.error(f"Accesso Negato {record_data.rec_name}")
                return ResponseDataErr(
                    message=f"Accesso Negato {record_data.rec_name}",
                    model=self.data_model.model,
                    rec_name=record_data.rec_name,
                )

            record = await self.before_save(record, copy=False, update=update)

            if self.data_model.status.fail:
                return self.data_model.response_err()

            if self.data_model.data_model == "no model":
                reload = False
            else:
                if update:
                    record = await self.data_model.update(record)
                else:
                    record = await self.data_model.insert(record)

                if self.data_model.status.fail:
                    return self.data_model.response_err()
        act_path = await self.compute_action_path(record)
        return ResponseData(
            **{
                "status": "ok",
                "link": f"{act_path}",
                "reload": reload,
                "schema": model_schema.get_dict() if model_schema else {},
                "data": record.get_dict(),
            }
        )

    async def copy_action(self, data={}) -> BaseModel:
        logger.info(
            f"copy_action -> model:{self.action.model} "
            f"action_type:{self.action.type}, curr_ref:{self.curr_ref}"
        )
        related_name = self.aval_related_name()
        if self.action.model == "component":
            record = self.compo_model.by_name(data.get("rec_name"))
            can_edit = await self.eval_editable(self.compo_model, record)
            if not can_edit:
                logger.error(f"Accesso Negato {record_data.rec_name}")
                return ResponseDataErr(
                    message=f"Accesso Negato {record_data.rec_name}",
                    model=self.compo_model.model,
                    rec_name=record_data.rec_name,
                )

            record = await self.save_copy_component(data=data, copy=True)

            if self.compo_model.status.fail:
                return self.data_model.response_err()
            logger.info("make auto actions for model")
            await self.mdata.make_default_action_model(record)

            await self.check_and_create_task_action(record)
            schema = self.compo_model.model.get_dict_json()

        else:
            model_schema: OzonModelApp = self.env.get(self.action.model)
            record = model_schema.by_name(data.get("rec_name"))
            can_edit = await self.eval_editable(self.data_model, record)
            if not can_edit:
                logger.error(f"Accesso Negato {record_data.rec_name}")
                return ResponseDataErr(
                    message=f"Accesso Negato {record_data.rec_name}",
                    model=self.data_model.model,
                    rec_name=record_data.rec_name,
                )

            record = await model_schema.copy({'rec_name', record.rec_name})

            if model_schema.status.fail:
                return model_schema.response_err()

            schema = model_schema.model.get_dict_json()

        act_path = await self.compute_action_path(record)

        return BaseResponse(
            **{
                "status": "ok",
                "link": f"{act_path}",
                "reload": True,
                "schema": schema,
                "data": record.get_dict_json(),
            }
        )

    async def delete_action(self) -> BaseModel:
        logger.info(
            f"delete_action -> model:{self.action.model} "
            f"action_type:{self.action.type}, "
            f"curr_ref:{self.curr_ref}"
        )
        self.data_model: OzonModelApp = await self.env.get(self.action.model)
        record = await self.data_model.by_name(self.curr_ref)
        can_delete = await self.eval_delete(self.data_model, record)
        if not can_delete:
            logger.error(f"Accesso Negato {record_data.rec_name}")
            return ResponseDataErr(
                message=f"Accesso Negato {record_data.rec_name}",
                model=self.data_model.model,
                rec_name=record_data.rec_name,
            )

        if self.action.model == "component":
            await self.clean_action_and_menu_group(self.data_model)
        await self.data_model.set_to_delete(record)
        act_path = await self.compute_action_path(record)
        return ResponseData(
            **{"status": "ok", "link": f"{act_path}", "reload": True}
        )

    # TODO
    async def apiApp_action(self, data={}):
        logger.info(
            f"-> model:{self.action.model} "
            f"action_type:{self.action.type}, "
            f"curr_ref:{self.curr_ref}"
        )
        data_model: OzonModelApp = await self.env.get(self.action.model)

        method_name = self.action.url

        if hasattr(self, method_name):
            data = await mtd(data)
        else:
            logger.error(f"No method name {method_name}")
        # save record
        record = await self.save_action(data=data)
        # if is error record is dict
        act_path = await self.compute_action_path(record)

        return ResponseData(
            **{
                "status": "ok",
                "link": f"{act_path}",
                "reload": True,
                "schema": data_model.get_dict(),
                "data": record.get_dict(),
            }
        )

    async def system_action(self, data={}):
        pass

    async def make_default_action_model(
        self, record: CoreModel, menu_group=False
    ):
        """

        :param record: record
        :param component_schema:  name of component
        :param menu_group: dict with 2 entries "rec_name" and "title"
        :return: None
        """
        model_name = record.rec_name
        actions = await self.action_model.count_by_filter(
            {"$and": [{"model": model_name}]}
        )
        if (
            record
            and record.type in ['form', 'resource']
            and self.action.builder_enabled
            and actions == 0
            and not record.data_model
        ):
            q = {
                "$and": [
                    {"model": "action"},
                    {"sys": True},
                    {"deleted": 0},
                    {"list_query": "{}"},
                ]
            }

            menu_group_model: OzonModelApp = await self.env.get("menu_group")
            apps_model = self.env.get("settings")
            list_actions_todo = await self.action_model.distinct(
                "rec_name", q, sort=sort, limit=0, skip=0
            )

            logger.debug(f"found {len(list_actions_todo)} action {record.sys}")
            apps_code = await apps_model.get(
                "rec_name", apps_model.default_domain
            )

            menu_groups = await menu_group_model.count_by_filter(
                domain={"rec_name": model_name, "deleted": 0}
            )
            if menu_groups == 0 and not record.type == 'resource':
                menu = await menu_group_model.new(
                    {
                        "rec_name": model_name,
                        "label": record.title,
                        "admin": record.sys,
                        "app_code": [self.app_code],
                    }
                )
                if record.sys:
                    menu.app_code = apps_code

                menu = await menu_group_model.insert(menu)

            for action_tmp_name in list_actions_todo:
                action = await self.action_model.copy(
                    {"rec_name": action_tmp_name}
                )
                action.sys = record.sys
                action.model = model_name
                action.data_value['model'] = record.title
                action.admin = record.sys
                if not action.admin:
                    action.user_function = "user"
                if action.component_type:
                    action.component_type = component_schema.type
                if action.action_type == "menu":
                    action.title = f"{record.title}"
                    action.data_value['title'] = f"{record.title}"
                    action.data_value['data_model'] = model_name
                    if menu_group:
                        action.menu_group = menu_group['rec_name']
                        action.data_value['menu_group'] = menu_group['title']
                    else:
                        if component_schema.type == 'resource':
                            action.menu_group = 'risorse_app'
                            action.data_value['menu_group'] = "Risorse Apps"
                        else:
                            action.menu_group = model_name
                            action.data_value['menu_group'] = record.title

                action.rec_name = action.rec_name.replace(
                    "_action", f"_{model_name}"
                )
                action.data_value['rec_name'] = action.rec_name
                action.next_action_name = action.next_action_name.replace(
                    "_action", f"_{model_name}"
                )
                await self.action_model.insert(action)

    async def check_and_create_task_action(self, record: CoreModel):
        model: OzonModelApp = await self.env.get(record.rec_name)
        properties = model.model.fields_properties()
        if model.model.create_task_action():
            for k in model.model.create_task_action():
                config = properties.get(k, {})
                actions = await self.action_model.count_by_filter(
                    {
                        "$and": [
                            {"model": record.rec_name},
                            {
                                "rec_name": f"{record.rec_name}_"
                                f"{config.get('rec_name')}"
                            },
                        ]
                    }
                )
                if actions == 0:
                    await self.make_action_task_for_model(
                        record, config.copy()
                    )

    async def make_action_task_for_model(
        self, record: CoreModel, act_config={}
    ):
        logger.debug(f" make_default_action_model {model_name}")

        model_name = record.rec_name
        q = {
            "$and": [
                {"model": model_name},
                {"deleted": 0},
                {"action_type": "save"},
                {"list_query": "{}"},
            ]
        }

        list_data = await self.action_model.distinct(
            "rec_name", q, sort=sort, limit=0, skip=0
        )
        if list_data:
            action = self.action_model.copy({"rec_name": list_data[0]})
            action.sys = record.sys
            action.model = model_name
            action.data_value['model'] = record.title
            action.admin = act_config.get("admin", False)
            if not action.admin:
                action.user_function = "user"
            if action.component_type:
                action.component_type = record.type
            action.action_type = act_config.get("action_type", "task")
            action.data_value['action_type'] = act_config.get("action_type")
            action.type = act_config.get("type", "data")
            action.title = f"Task {record.title}"
            action.data_value['title'] = f"Task {record.title}"
            action.rec_name = f"{model_name}_{act_config.get('rec_name')}"
            action.data_value['rec_name'] = action.rec_name
            await self.action_model.insert(action)

    async def clean_action_and_menu_group(self, model: OzonModelApp):
        menu_group_model: OzonModelApp = await self.env.get("menu_group")
        action_model: OzonModelApp = await self.env.get("action")
        await action_model.remove_all(domain={"$and": [{"model": model.name}]})
        await menu_group_model.remove_all(
            domain={"$and": [{"rec_name": model.name}]}
        )
