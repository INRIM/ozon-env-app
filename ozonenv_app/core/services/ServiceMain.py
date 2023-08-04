# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import copy
import sys
import os

from ozonenv.core.BaseModels import Session
from ozonenv.core.cache.cache import get_cache
from ozonenv_app.core.BaseModel import *
from ozonenv_app.core.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.OzonModelApp import OzonModelApp
from ozonenv_app.core.ServiceModelSecurity import ServiceModelSecurity
from ozonenv_app.core.ServiceMenuManager import ServiceMenuManager
from ozonenv_app.core.ServiceAction import ServiceAction
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


class ServiceMain:
    def __init__(self, app: OzonEnvApp):
        self.app: OzonEnvApp = app
        self.env = app.env
        self.app_settings: Settings = self.env.orm.app_settings
        self.session: Session = self.app.session
        self.action_service = None
        self.app_code = self.env.app_code
        self.acl: ServiceModelSecurity = ServiceModelSecurity(self.app)
        self.menu_manager = ServiceMenuManager(self.app)

    async def get_param(self, name: str) -> Any:
        data = await self.env.get("global_params").by_name(name)
        if data:
            return data.value
        else:
            return ""

    async def service_handle_action(
        self,
        action_name: str,
        data: dict = {},
        rec_name: str = "",
        parent="",
        iframe="",
        execute=False,
        container_act="",
    ) -> BaseModel:
        logger.debug(
            f"handle_action -> name:{action_name}, rec_name:{rec_name}, "
            f"execute:{execute}, data:{data.keys()}, "
            f"container_act: {container_act}, parent: {parent}"
        )
        if not data:
            data = {"limit": 0, "skip": 0, "sort": "", "query": {}}
        self.action_service = ServiceAction(
            self.app,
            action_name=action_name,
            rec_name=rec_name,
            parent=parent,
            iframe=iframe,
            execute=execute,
            container_act=container_act,
        )

        act_data = await self.action_service.compute_action(data=data)

        if isinstance(act_data, ResponseDataErr):
            return act_data

        act_data.settings = self.app.get_layout_response()

        return act_data

    async def service_get_layout(self, name="") -> LayoutResponse:
        logger.debug("service_get_default_layout")
        if not name:
            name = self.session.app.get("layout")
        else:
            self.session.app['layout'] = name

        layout: CoreModel = await self.env.get("component").by_name(name)

        return LayoutResponse(
            **{
                "mode": "system",
                "settings": self.app.get_layout_response(),
                "menu": await self.menu_manager.make_main_menu(),
                "schema": layout.schema(),
            }
        )

    async def service_get_dashboard(self, parent=""):
        # logger.debug(f"service_get_dashboard {parent}")
        # cards = await self.menu_manager.make_dashboard_menu(parent=parent)

        return {"model": "action", "content": {"mode": "cards", "data": {}}}

    async def service_get_schema(self, model_name) -> dict:
        logger.debug(f"service_get_schema by name {model_name}")
        # TODO add check rules for model
        schema = await self.env.get(model_name).schema.copy()
        return schema or {}

    async def service_get_schema_model(self, model_name: str):
        logger.debug(f"service_get_schema_model by name {model_name}")

        schema, fields = self.env.get(model_name).get_schema_fields()

        res = {
            "mode": "system",
            "schema": schema,
            "fields": fields,
            "metadata": default_list_metadata_fields,
        }
        return res

    async def service_reorder_record(self, data):
        logger.debug(f"service_reorder_record by name {data}")
        model_data = await self.env.get(data['model_name']).model
        list_to_save = []
        for record_data in data['columns']:
            record = await self.env.get(model_data).by_name(record_data['key'])
            record.list_order = int(record_data['value'])
            if not data['model_name'] == "component":
                record.data_value['list_order'] = record_data['value']
            list_to_save.append(record)
        await self.env.get(model_data).update_all(
            list_to_save, remove_meta=False
        )
        return {"status": "ok"}

    async def service_get_schemas_by_type(
        self, schema_type="form", query={}, fields=[], additional_key=[]
    ) -> ResponseData:
        logger.info(
            f"service_get_schemas_by_type  schema_type:{schema_type}, query:{query}, "
            f"fields:{fields},additional_key:{additional_key}"
        )
        # TODO add check rules
        query = {"$and": [{"deleted": 0}, {"type": {"$eq": schema_type}}]}
        data = await self.env.get('component').search_all_distinct(
            "rec_name", query=query, additional_key=additional_key
        )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "data": data or [],
                }
            }
        )

    async def service_get_schemas_by_parent_and_type(
        self, parent_model, schema_type="form", fields=[], additional_key=[]
    ) -> ResponseData:
        logger.info(f"service_get_schema by name {parent_model}")
        # TODO add check rules parent_model
        query = {"$and": [{"parent": {"$eq": parent_model}}, {"deleted": 0}]}
        data = await self.env.get('component').get_list_base(
            fields=fields,
            query=query,
            model_type=schema_type,
            additional_key=additional_key,
        )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "data": data or [],
                }
            }
        )

    async def service_get_data_for_model(
        self, model_name, query={}, fields=[], additional_key=[]
    ) -> ResponseData:
        logger.debug(f"get_data_model {model_name}")
        # TODO add check read rules model_name
        data_model = await self.env.get(model_name)
        query = await data_model.default_query(query)
        data = await data_model.get_list_base(
            data_model, fields=fields, query=query, sort=sort
        )
        return ResponseData(
            **{"content": {"mode": "list", "data": data or []}}
        )

    async def service_get_data_view(
        self, model_name, query={}, fields=[], additional_key=[]
    ) -> ResponseData:
        logger.debug(f"service_get_data_view {model_name}")
        # TODO add check read rules model_name
        data = await self.env.get(model_name).search(query=query)
        return ResponseData(
            **{"content": {"mode": "list", "data": data or []}}
        )

    async def service_get_record(
        self, model_name: str, rec_name: str
    ) -> ResponseData:
        logger.debug(
            f"service_get_record by name model_name:"
            f"{model_name}, rec_name:{rec_name}"
        )

        # TODO add check read rules for model
        model: OzonModelApp = await self.env.get(model_name)
        data: CoreModel = await model.by_name(rec_name)
        if not data:
            data = await model.new({})
        can_edit = await self.acl.can_update(model, data)
        return ResponseData(
            **{
                "content": {
                    "editable": can_edit,
                    "mode": "form",
                    "model": model_name,
                    "schema": model.schema or {},
                    "data": data or {},
                }
            }
        )

    async def service_component_distinct_model(self) -> ResponseData:
        logger.info(f"service_component_distinct_model")
        # TODO add check read rules for model
        query = {"$and": [{"deleted": 0}, {"data_model": {"$eq": ""}}]}
        data = await self.mdata.all_distinct(
            Component, "rec_name", query=query
        )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "data": data or [],
                }
            }
        )

    async def service_distinct_rec_name_by_model(
        self, model_name="component", domain={}, props={}
    ) -> ResponseData:
        logger.debug(
            f"service_component_distinct_model model_name:{model_name}, "
            f"domain:{domain}, props:{props}"
        )
        # TODO add check read rules for model
        data = []
        can_read = True
        if model_name:
            model_data = await self.env.get(model_name)
            distinct_field = props.get("id", "rec_name")
            data: CoreModel = await model_data.search_all_distinct(
                distinct=distinct_field,
                query=domain,
                compute_label=props.get("compute_label", ""),
            )
            can_read = await self.acl.can_read(data)
            if model_name == "component":
                data.append(
                    {
                        "_id": "component",
                        "rec_name": "component",
                        "title": "Component",
                        "type": "",
                    },
                )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "data": data and can_read or [],
                }
            }
        )

    async def service_freq_for_field_model(
        self,
        model_name="",
        field="",
        field_query={},
        min_occurence=2,
        add_fields="",
        sort=-1,
    ) -> ResponseData:
        logger.debug(
            f"gen freq model_name:{model_name}, field:{field}, "
            f"field_query:{field_query}, min_occurence: {min_occurence}"
        )

        data = []
        if model_name and field:
            model_data = await self.env.get(model_name)
            data = await model_data.freq_for_all_by_field_value(
                field=field,
                field_query=field_query,
                min_occurence=min_occurence,
                add_fields=add_fields,
                sort=sort,
            )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "data": data or [],
                }
            }
        )

    async def get_remote_data_select(
        self, url, path_value, header_key, header_value_key, params={}
    ) -> ResponseData:
        logger.debug(
            f" url:{url}, path_value:{path_value},"
            f" header_key:{header_key}, header_value_key:{header_value_key}, "
            f" params:{params}"
        )
        if path_value:
            url = f"{url}/{path_value}"
        if self.env.use_cache:
            cache = await get_cache()
            editing = self.session.app.get("builder")
            memcache = await cache.get(
                self.app_code, f"get_remote_data_select:{url}"
            )
            if memcache and not editing:
                values = memcache.get("content", {}).get("data", [])
                if len(values) > 0:
                    logger.info(f"use cache")
                    return memcache
        rec_cfg = await self.get_param(header_value_key)
        headers = {}
        if isinstance(rec_cfg, dict):
            remote_data = await self.get_remote_data(
                headers, header_key, rec_cfg.get("key"), url, params=params
            )
        else:
            remote_data = await self.get_remote_data(
                headers, header_key, rec_cfg, url, params=params
            )
        data = remote_data if isinstance(remote_data, list) else []
        res = ResponseData(**{"content": {"mode": "list", "data": data}})
        if data and len(data) > 0 and self.env.use_cache:
            await cache.set(
                self.app_code, f"get_remote_data_select:{url}", res, expire=800
            )
        return res

    async def get_remote_data(
        self, headers={}, header_key=[], header_value="", url="", params={}
    ) -> ResponseData:
        logger.debug(
            f"server get_remote_data --> {url}, header_key:{header_key},"
            f" header_value:{header_value} "
        )
        if header_key and header_value:
            headers.update(
                {"Content-Type": "application/json", header_key: header_value}
            )
        else:
            headers.update(
                {
                    "Content-Type": "application/json",
                }
            )

        async with httpx.AsyncClient(timeout=None) as client:
            res = await client.get(url=url, headers=headers, params=params)

        if res.status_code == 200:
            logger.info(f"server get_remote_data --> {url} SUCCESS ")
            datar = res.json()
            data = copy.deepcopy(datar)
            if isinstance(datar, dict) and datar.get("result"):
                if isinstance(datar.get("result"), dict) and datar.get(
                    "result"
                ).get("select_list"):
                    data = datar.get("result", {}).get("select_list", [])
                if isinstance(datar.get("result"), list):
                    data = datar.get("result", [])
        else:
            logger.info(
                f"server get_remote_data --> {url} Error {res.status_code} "
            )
            data = {}
        return ResponseData(**{"content": {"mode": "list", "data": data}})

    async def export_data(
        self, model_name, datas, parent_name=""
    ) -> ResponseData:
        logger.info(
            f" model:{model_name}, query:{datas}, parent_name:{parent_name}"
        )

        data_mode = datas.get('data_mode', 'json')
        model = await self.env.get(model_name)
        query = await data_model.default_query(datas['query'])
        logger.info(query)
        # sort = self.mdata.eval_sort_str(schema.properties.get("sort", ''))

        if not data_mode == 'json':
            data = await model.search_export(
                fields=[],
                merge_field="data_value",
                query=query,
                parent=parent_name,
                remove_keys=["_id", "id"],
                sort=sort,
            )
        else:
            if schema.sys:
                to_rm = default_fields[:]
            else:
                to_rm = []
            to_rm.append("_id")
            data = await model.search_export(
                data_model,
                fields=[],
                query=query,
                parent=parent_name,
                remove_keys=to_rm,
            )
        return ResponseData(
            **{
                "content": {
                    "mode": "list",
                    "model": model_name,
                    "schema": model.schema or {},
                    "data": data or [],
                }
            }
        )

    async def update_record_user_data(self, record: CoreModel, uid):
        logger.info(f"update {uid}")
        self.auth_service = ServiceAuth.new(
            settings=self.app_settings,
            public_endpoint=[],
            parent=self,
            request=self.request,
            pwd_context=self.pwd_context,
            req_id="",
        )
        user = await self.auth_service.session_service.user_role(uid)
        logger.info(f"record update {user.get('uid')}")
        record.owner_uid = user.get('uid')
        record.owner_name = user.get('full_name', "")
        record.owner_mail = user.get('mail', "")
        record.owner_sector = user.get("divisione_uo", "")
        record.owner_sector_id = int(user.get('divisione_uo_id', 0))
        record.owner_personal_type = user.get("tipo_personale", "")
        record.owner_job_title = user.get("qualifica", "")
        record.owner_function = user.get("user_function")
        return record

    async def celan_model(self, model_name) -> BaseModel:
        if not self.session.is_admin:
            return ResponseDataErr(
                **{
                    "status": "error",
                    "message": f"Error Admin Only",
                    "model": model_name,
                }
            )
        data_model = await self.env.get(model_name)
        res = await data_model.remove_all({})
        return ResponseData(
            **{
                "status": "ok",
                "rec_name": "",
                "model": model_name,
                "message": f"deleted {res} records",
            }
        )

    async def import_raw_data(self, model_name, record_data) -> BaseModel:
        if not self.session.is_admin:
            return ResponseDataErr(
                **{
                    "status": "error",
                    "message": f"Admin Only",
                    "model": model_name,
                }
            )
        data_model: OzonModelApp = await self.env.get(model_name)
        try:
            record = await data_model.new(data=record_data)
            data_model.ignore_set_user_data = False
            if record.owner_uid:
                record = await self.update_record_user_data(
                    record, record.owner_uid
                )
                data_model.ignore_set_user_data = True
            record = await data_model.insert(record)

            return ResponseData(
                **{
                    "status": "ok",
                    "rec_name": record.rec_name,
                    "model": model_name,
                }
            )
        except ValidationError as e:
            logger.error(f" Validation {e}")
            return ResponseDataErr(
                **{
                    "status": "error",
                    "message": f"Errore validazione {e}",
                    "model": model_name,
                }
            )

    async def get_mail_template(
        self, model_name, template_name=""
    ) -> BaseModel:
        logger.info(f" model:{model_name}, template_name:{template_name}")

        template_model: OzonModelApp = await self.env.get("mail_template")
        query = {"$and": [{"model": model_name}, {"default": True}]}

        if template_name:
            query = {"rec_name": template_name}

        query = await template_model.default_query(query)
        list_template = await template_model.search(query=query)

        tmp_dict = {}
        if list_template:
            tmp_dict = list_template[0]

        return ResponseData(
            **{
                "content": {
                    "mode": "form",
                    "model": model_name,
                    "data": tmp_dict or {},
                }
            }
        )

    async def get_mail_server_out(self, server_name="") -> BaseModel:
        logger.info(f" server_name:{server_name}")
        server_model: OzonModelApp = await self.env.get("mail_server_out")
        query = await server_model.default_query({"rec_name": server_name})
        list_server = await server_model.search(query=query)
        server_dict = {}
        if list_server:
            server_dict = list_server[0]

        return ResponseData(
            **{
                "content": {
                    "mode": "form",
                    "model": "mail_server_out",
                    "data": server_dict or {},
                }
            }
        )

    async def attachment_to_trash(
        self, model_name, rec_name, data
    ) -> ResponseData:
        logger.info(f"model:{model_name}, rec_name:{rec_name} data {data}")
        try:
            key = data.get('key')
            file_field = data.get("field")

            data_model = await self.env.get(model_name)
            trash_model = await self.env.get("attachment_trash")
            record = await data_model.by_name(rec_name)

            list_files = []
            rec_to_save = []
            for file_todo in record.get(file_field):
                if not file_todo.key == key:
                    list_files.append(file_todo)
                else:
                    rec_to_save.append(file_todo)
            record.file_field = list_files[:]

            trash = trash_model.new(
                {
                    "rec_name": f"trash.{str(uuid.uuid4())}",
                    "model": model_name,
                    "modell_rec_name": rec_name,
                    "attachments": rec_to_save[:],
                }
            )
            trash = trash_model.insert(trash)
            # if error record is dict
            if not trash or trash.is_error():
                return ResponseData(
                    **{
                        "status": "error",
                        "message": f"Errore {trash.message}",
                        "model": model_name,
                        "rec_name": rec_name,
                    }
                )
            await data_model.update(record)
            return ResponseData(
                **{"link": "#", "reload": True, "status": "ok"}
            )
        except Exception as e:
            logger.error(e, exc_info=True)
            return ResponseData(
                **{
                    "status": "error",
                    "message": f"Errore {e}",
                    "model": model_name,
                    "rec_name": rec_name,
                }
            )

    async def clean_all_to_delete_action(self) -> ResponseData:
        logger.info(f"clean expired to_delete_action ")
        c_names = await self.env.orm.get_collections_names()
        for name in c_names:
            data_model = await self.env.get(name)
            logger.info(f" clean {name} ")
            if data_model:
                if name == "session":
                    q1 = {
                        "$or": [
                            {
                                "expire_datetime": {
                                    "$lt": datetime.now().isoformat()
                                }
                            },
                            {"active": False},
                        ]
                    }
                    res = await data_model.remove_all(q1)
                    q2 = {"$or": [{"active": False}, {"is_public": True}]}
                    res += await data_model.remove_all(q2)
                    logger.info(f" clean to delete {name}  {res}")
                else:
                    curr_timestamp = datetime.now().timestamp()
                    q = {
                        "$and": [
                            {"deleted": {"$gt": 0}},
                            {"deleted": {"$lt": curr_timestamp}},
                        ]
                    }
                    res = await data_model.remove_all(q)
                    logger.info(f" clean to delete {name}  {res}")
        return ResponseData(**{"status": "done"})

    async def count(self, model_name, query_data) -> ResponseData:
        model: OzonModelApp = self.env.get(model_name)
        query = await model.default_query(query_data)
        cdata = DataResponse(**{})
        cdata.recordsTotal = await model.count_by_filter(query)
        return ResponseData(**{"status": "ok", "content": cdata})

    async def get_calendar_task(self, task_name) -> BaseModel:
        await self.make_settings()
        try:
            calendar = await self.mdata.by_name("calendar", task_name)
            task = await self.mdata.by_name("action", calendar.task)
            return ResponseData(
                **{"status": "success", "calendar": calendar, "task": task}
            )
        except Exception as e:
            logger.error(f"Task: {task_name} - {e}", exc_info=True)
            return ResponseDataErr(
                **{"status": "error", "data": {}, "name": task_name}
            )

    async def update_calendar_task(
        self, task_name, execution_status
    ) -> BaseModel:
        action = await self.env.get('action').by_name("action", task_name)
        can_read = await self.acl.can_read(action)
        if not can_read:
            return ResponseDataErr(
                **{"status": "error", "name": task_name, "data": {}}
            )
        try:
            calendar = await self.env.get('calendar').by_name(task_name)
            task = await self.env.get('action').by_name(calendar.task)
            self.action_service = ServiceAction.new(
                session=self.session,
                service_main=self,
                action_name=task.rec_name,
                rec_name=calendar.rec_name,
                parent="",
                iframe="",
                execute=True,
                pwd_context=self.pwd_context,
            )
            await self.action_service.make_settings()
            return await self.action_service.calendar_task(
                task_name, calendar, task, execution_status
            )
        except Exception as e:
            logger.error(f"Task: {task_name} - {e}", exc_info=True)
            return ResponseDataErr(
                **{"status": "error", "data": {}, "name": task_name}
            )
