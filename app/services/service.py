import logging
from typing import Any
from typing import Union

from ozonenv.OzonEnv import OzonEnv

from .action_runtime import ActionRuntime
from .common import *
from .formio import get_formio_select_options
from app.core.models import FieldAclOperation
from app.ozon_env_acl import CompiledFieldAcl
from app.ozon_env_acl import compile_field_acl_policies
from app.ozon_env_acl import enforce_write_acl

logger = logging.getLogger("uvicorn.error")


def _normalize_order(order: str) -> str:
    """
    Normalizza la sintassi dell'ordinamento verso il formato ozon-env:
    - `field:asc|desc` (gia valido)
    - `-field` -> `field:desc`
    - `+field` / `field` -> `field:asc`
    Supporta piu campi separati da virgola.
    """

    if not order:
        return ""
    tokens = [token.strip() for token in order.split(",") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        if ":" in token:
            normalized.append(token)
            continue
        if token.startswith("-") and len(token) > 1:
            normalized.append(f"{token[1:]}:desc")
            continue
        if token.startswith("+") and len(token) > 1:
            normalized.append(f"{token[1:]}:asc")
            continue
        normalized.append(f"{token}:asc")
    return ",".join(normalized)


def _obj_to_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj.copy()
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "get_dict"):
        return obj.get_dict()
    return {}


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _merge_query(
        base_query: dict[str, Any], extra_query: dict[str, Any]
) -> dict[str, Any]:
    if not base_query:
        return extra_query.copy() if isinstance(extra_query, dict) else {}
    if not extra_query:
        return base_query.copy()
    return {"$and": [base_query.copy(), extra_query.copy()]}


class Service:
    def __init__(self, env: OzonEnv):
        self.env = env
        self.session = env.user_session
        self.settings = env.orm.app_settings
        self.action_runtime = ActionRuntime(self)
        self._compiled_field_acl: CompiledFieldAcl | None = None
        logger.info(
            "service initialized app_code=%s",
            self.session.app_code,
        )

    async def get_models(self, query: dict = None):
        logger.info("service.get_models query=%s", query if query else {})
        compo_model = self.env.get("component")
        return await compo_model.distinct("rec_name", query if query else {})

    async def get_distinct(
            self,
            model: str,
            query: dict = None,
            compute_label: str = "",
    ):
        logger.info(
            "service.get_distinct model=%s query=%s compute_label=%s",
            model,
            query if query else {},
            compute_label,
        )
        _model = self.env.get(model)
        return await _model.search_all_distinct(
            "rec_name",
            query if query else {},
            compute_label=compute_label,
            raw_result=True,
        )

    async def get_select_options(
            self,
            cli_session,
            field_key,
            curr_model,
            schema_type: str = "formio",
    ):
        # Ad oggi supportiamo select basate su schema FormIO.
        logger.info(
            "service.get_select_options field=%s model=%s schema_type=%s",
            field_key,
            curr_model,
            schema_type,
        )
        if schema_type == "formio":
            return await get_formio_select_options(
                cli_session,
                curr_model,
                field_key,
            )
        else:
            logger.warning(
                "service.get_select_options unsupported schema_type=%s",
                schema_type,
            )
            return []

    async def by_name(self, model: str, name: str):
        logger.info("service.by_name model=%s name=%s", model, name)
        compo_model = self.env.get(model)
        return await compo_model.by_name(name)

    async def compo_by_name(self, model: str, name: str) -> ResponseObject:
        logger.info("service.compo_by_name model=%s name=%s", model, name)
        compo_model = self.env.get(model)
        record = await compo_model.by_name(name)
        return make_response_object(compo_model, mode="form", data=record)

    async def stream_record(
            self,
            envelope: ResponseObject,
            order: str,
            skip: int,
            limit: int,
            pipeline_items: list[dict] = None,
    ):
        normalized_order = _normalize_order(order)
        logger.info(
            "service.stream_record model=%s order=%s normalized_order=%s skip=%s limit=%s",
            envelope.content.model,
            order,
            normalized_order,
            skip,
            limit,
        )
        model = self.env.get(envelope.content.model)
        return model.stream_find(
            domain=envelope.content.query,
            sort=normalized_order,
            skip=skip,
            limit=limit,
            pipeline_items=pipeline_items,
            obfuscate_fields=envelope.content.obfucated_fields,
            fields=envelope.content.fields,
            batch_size=envelope.content.batch_size,
        )

    async def list_records(
            self,
            model_name: str,
            query: dict,
            order: str,
            skip: int,
            limit: int,
            pipeline_items: list[dict] = None,
            obfuscate_fields: list[str] = None,
            fields: list[str] = None,
            resp_stream: bool = False,
            batch_size: int = 500,
    ) -> Union[ResponseObject]:
        normalized_order = _normalize_order(order)
        logger.info(
            "service.list_records model=%s order=%s normalized_order=%s skip=%s limit=%s stream=%s",
            model_name,
            order,
            normalized_order,
            skip,
            limit,
            resp_stream,
        )
        record_model = self.env.get(model_name)
        domain = record_model.get_domain(query)
        total_count = await record_model.count(domain=domain)
        acl = await self._get_compiled_field_acl()
        denied_read_fields, obfuscate_read_fields = acl.read_masks(
            model_key=model_name,
            app_key=str(getattr(self.session, "app_code", "")),
        )
        read_mask_fields = sorted(set(denied_read_fields + obfuscate_read_fields))
        logger.info(
            "service.list_records total_count=%s domain=%s",
            total_count,
            domain,
        )
        if resp_stream:
            response = make_response_object(
                model=record_model,
                mode="list_stream",
                data=[],
                query=domain,
                fields=fields,
                batch_size=batch_size,
                readable=True,
                editable=True,
                can_create=True,
                total_count=total_count,
            )
            response.content.obfucated_fields = read_mask_fields
            return response
        data = await record_model.find(
            domain=domain,
            sort=normalized_order,
            skip=skip,
            limit=limit,
            pipeline_items=pipeline_items,
            obfuscate_fields=sorted(
                set(obfuscate_fields or []) | set(read_mask_fields)
            ),
            fields=fields,
        )
        data, applied_obfuscate_fields = acl.apply_read(
            model_key=model_name,
            app_key=str(getattr(self.session, "app_code", "")),
            data=data,
        )
        response = make_response_object(
            model=record_model,
            mode="list",
            data=data,
            query=query,
            total_count=total_count,
        )
        response.content.obfucated_fields = sorted(
            set(obfuscate_fields or []) | set(applied_obfuscate_fields)
        )
        return response

    async def upsert(
            self,
            model_name: str,
            data: Union[dict] = None,
            rec_name="",
            data_value: dict = None,
            trnf_config: dict = None,
            fields_parser: dict = None,
    ) -> Union[None, ResponseObject]:
        logger.info(
            "service.upsert model=%s rec_name=%s",
            model_name,
            rec_name,
        )
        record_model = self.env.get(model_name)
        operation = await self._resolve_write_operation(record_model, rec_name, data)
        acl = await self._get_compiled_field_acl()
        await enforce_write_acl(
            acl,
            self.env,
            session=self.session,
            model_key=model_name,
            operation=operation,
            payload=data if isinstance(data, dict) else {},
        )
        record = await record_model.upsert(
            data=data,
            rec_name=rec_name,
            data_value=data_value,
            trnf_config=trnf_config,
            fields_parser=fields_parser,
        )
        return make_response_object(record_model, mode="form", data=record)

    async def load(
            self, domain: dict, in_execution=False
    ) -> Union[None, ResponseObject]:
        logger.info("service.load domain=%s", domain)
        record_model = self.env.get(model_name)
        record = await record_model.load(domain)
        return make_response_object(record_model, mode="form", data=record)

    async def load_record(
            self, model: str, rec_name: str
    ) -> Union[None, ResponseObject]:
        logger.info("service.load_record model=%s rec_name=%s", model, rec_name)
        record_model = self.env.get(model)
        record = await record_model.by_name(rec_name)
        acl = await self._get_compiled_field_acl()
        record, obfuscate_fields = acl.apply_read(
            model_key=model,
            app_key=str(getattr(self.session, "app_code", "")),
            data=record,
        )
        response = make_response_object(record_model, mode="form", data=record)
        response.content.obfucated_fields = obfuscate_fields
        return response

    async def _resolve_write_operation(
            self,
            record_model: Any,
            rec_name: str,
            data: dict[str, Any] | None,
    ) -> str:
        target_rec_name = rec_name or (
            data.get("rec_name", "") if isinstance(data, dict) else ""
        )
        if not target_rec_name:
            return FieldAclOperation.INSERT.value
        try:
            existing = await record_model.by_name(target_rec_name)
        except Exception:
            existing = None
        if existing:
            return FieldAclOperation.UPDATE.value
        return FieldAclOperation.INSERT.value

    async def _get_compiled_field_acl(self) -> CompiledFieldAcl:
        cached = getattr(self.session, "compiled_field_acl", None)
        if isinstance(cached, CompiledFieldAcl):
            return cached
        if self._compiled_field_acl is not None:
            return self._compiled_field_acl

        policies = await self._load_field_acl_policies()
        compiled = compile_field_acl_policies(policies, session=self.session)
        self._compiled_field_acl = compiled
        try:
            object.__setattr__(self.session, "compiled_field_acl", compiled)
        except Exception:
            pass
        return compiled

    async def _load_field_acl_policies(self) -> list[Any]:
        for model_name in ("field_acl_policy", "fieldaclpolicy", "FieldAclPolicy"):
            try:
                model = self.env.get(model_name)
            except Exception:
                continue
            try:
                domain = model.get_domain({"active": True, "deleted": 0})
            except Exception:
                domain = {"active": True, "deleted": 0}
            try:
                return await model.find(
                    domain=domain,
                    sort="priority:asc,list_order:asc,rec_name:asc",
                    limit=0,
                )
            except TypeError:
                return await model.find(domain=domain, limit=0)
            except Exception:
                logger.exception("field ACL policy loading failed")
                return []
        return []

    async def _get_action_record(self, action_name: str):
        return await self.action_runtime.get_action_record(action_name)

    def _update_query_values(self, data: Any) -> Any:
        """
        Replica il comportamento storico di QueryEngine.update:
        sostituisce placeholder `_user_<field>` con dati sessione.
        """

        if isinstance(data, dict):
            updated: dict[str, Any] = {}
            for key, value in data.items():
                updated[key] = self._update_query_values(value)
            return updated
        if isinstance(data, list):
            return [self._update_query_values(item) for item in data]
        if isinstance(data, str) and "_user_" in data:
            attr_name = data.replace("_user_", "")
            return getattr(self.session, attr_name, data)
        return data

    def _parse_query_dict(self, query_value: Any) -> dict[str, Any]:
        if isinstance(query_value, dict):
            return query_value.copy()
        parsed = check_parse_json(query_value if query_value is not None else "{}")
        if isinstance(parsed, dict):
            return parsed
        return {}

    def _scan_find_key(self, data: Any, key: str) -> list[bool]:
        found: list[bool] = []
        if isinstance(data, dict):
            for curr_key, value in data.items():
                found.append(curr_key == key)
                if isinstance(value, dict):
                    found.extend(self._scan_find_key(value, key))
                elif isinstance(value, list):
                    for item in value:
                        found.extend(self._scan_find_key(item, key))
        return found

    def _query_has_key(self, data: Any, key: str) -> bool:
        return any(self._scan_find_key(data, key))

    def _model_name(self, model: Any) -> str:
        if hasattr(model, "str_name") and callable(model.str_name):
            try:
                name = model.str_name()
                if isinstance(name, str):
                    return name
            except Exception:
                pass
        for attr in ("data_model", "model_name", "_name", "name"):
            val = getattr(model, attr, "")
            if isinstance(val, str) and val:
                return val
        return ""

    async def _default_query(
            self,
            model: Any,
            query: dict[str, Any] | None,
            parent: str = "",
            model_type: str = "",
    ) -> dict[str, Any]:
        q = query.copy() if isinstance(query, dict) else {}
        model_name = self._model_name(model).lower()
        app_code = getattr(self.session, "app_code", "")

        if model_name == "menu_group" and app_code:
            q.update(
                {
                    "$or": [
                        {"apps": {"$in": [app_code]}},
                        {"apps": []},
                        {"apps": None},
                    ]
                }
            )

        if not self._query_has_key(q, "deleted"):
            q.update({"deleted": 0})

        if not self._query_has_key(q, "active"):
            q.update({"active": True})

        if not self._query_has_key(q, "parent") and parent:
            q.update({"parent": {"$eq": parent}})

        if not self._query_has_key(q, "type") and model_type:
            q.update({"type": {"$eq": model_type}})

        q = self._update_query_values(q)
        q = self._update_query_values(q.copy())
        return q

    async def _make_query_user(
            self, base_query: list[dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        return list(base_query or [])

    async def _find_base(
            self,
            model: Any,
            query: dict[str, Any],
            sort: str = "list_order:asc,rec_name:asc",
            limit: int = 0,
    ) -> list[Any]:
        return await model.find(domain=query, sort=sort, limit=limit)

    async def _count_base(self, model: Any, query: dict[str, Any]) -> int:
        domain = query
        if hasattr(model, "get_domain") and callable(model.get_domain):
            domain = model.get_domain(query)
        return await model.count(domain=domain)

    async def _safe_model(self, model_name: str) -> Any | None:
        try:
            return self.env.get(model_name)
        except Exception:
            logger.warning("model not found model=%s", model_name)
            return None

    async def _make_menu_item(
            self, card: dict[str, Any], rec_b: dict[str, Any]
    ) -> dict[str, Any] | bool:
        card_btn = rec_b.copy()
        action_model_name = card_btn.get("model", "")
        cc_model = await self._safe_model(action_model_name)
        if not cc_model:
            return False

        if card_btn.get("mode"):
            link = (
                f"{card_btn.get('action_root_path', '/action')}"
                f"/{card_btn.get('rec_name', '')}"
            )
        else:
            link = card_btn.get("action_root_path", "/action")

        number = 0
        if card_btn.get("mode") == "list":
            list_query = self._parse_query_dict(card_btn.get("list_query", "{}"))
            q = await self._default_query(cc_model, list_query)
            number = await self._count_base(cc_model, q)

        return {
            "model": card_btn.get("model", ""),
            "icon": card_btn.get("button_icon", ""),
            "action_type": card_btn.get("action_type", ""),
            "content": link,
            "label": card_btn.get("title", card_btn.get("rec_name", "")),
            "mode": card_btn.get("mode", ""),
            "number": number,
        }

    def _make_button_main_menu(self, rec: dict[str, Any]) -> dict[str, Any]:
        btn_action_parser = {
            "save": "post",
            "copy": "post",
            "delete": "post",
            "window": False,
        }
        rec_name = rec.get("rec_name", "")
        action_type = rec.get("action_type", "")
        action_root_path = rec.get("action_root_path", "/action")

        if action_type in btn_action_parser:
            url_action = f"{action_root_path}/{rec_name}/{rec_name}"
        else:
            url_action = f"{action_root_path}/{rec_name}"

        return {
            "model": rec.get("model", ""),
            "key": rec_name,
            "type": "button",
            "label": rec.get("title", rec_name),
            "leftIcon": rec.get("button_icon", ""),
            "btn_action_type": btn_action_parser.get(action_type),
            "action_type": action_type,
            "url_action": url_action,
            "builder": rec.get("builder_enabled", False),
        }

    async def _get_basic_menu_list(self, parent: str = "") -> list[dict[str, Any]]:
        menu_group_model = self.env.get("menu_group")
        action_model = self.env.get("action")

        menu_groups_query = await self._default_query(
            menu_group_model,
            {"$and": [{"admin": False}, {"parent": parent}]},
        )
        menu_groups = await self._find_base(
            menu_group_model,
            query=menu_groups_query,
        )

        menu_list: list[dict[str, Any]] = []
        model_done: set[str] = set()
        for group in (_obj_to_dict(item) for item in menu_groups):
            group_name = group.get("rec_name", "")
            q_user = await self._make_query_user([{"menu_group": group_name}])
            action_query = await self._default_query(
                action_model, {"$and": q_user}
            )
            found_item = await self._find_base(action_model, query=action_query)

            if found_item:
                first = _obj_to_dict(found_item[0])
                model_key = f"{group_name}{first.get('model', '')}"
                if model_key not in model_done:
                    model_done.add(model_key)
                    menu_list.append(
                        {
                            "model": first.get("model", ""),
                            "menu_group": group_name,
                            "label": group.get("label", group_name),
                        }
                    )
            else:
                sub_menu_query = await self._default_query(
                    menu_group_model,
                    {"$and": [{"deleted": 0}, {"parent": group_name}]},
                )
                sub_menus = await self._find_base(
                    menu_group_model,
                    query=sub_menu_query,
                )
                if sub_menus:
                    sub_groups = [
                        _obj_to_dict(sub).get("rec_name", "")
                        for sub in sub_menus
                        if _obj_to_dict(sub).get("rec_name", "")
                    ]
                    sub_query_user = await self._make_query_user(
                        [
                            {"deleted": 0},
                            {"menu_group": {"$in": sub_groups}},
                        ]
                    )
                    sub_action_query = await self._default_query(
                        action_model,
                        {"$and": sub_query_user},
                    )
                    sub_items = await self._find_base(
                        action_model,
                        query=sub_action_query,
                    )
                    if sub_items:
                        menu_list.append(
                            {
                                "model": False,
                                "menu_group": group_name,
                                "label": group.get("label", group_name),
                                "dashboard": True,
                                "content": f"/action/dashboard/{group_name}",
                                "action_type": "window",
                                "mode": "list",
                                "number": len(sub_menus),
                                "icon": "it-folder",
                            }
                        )
        return menu_list

    async def service_get_layout(self, name: str = "") -> ResponseObjectData:
        component_model = self.env.get("component")
        layout_name = name or ""
        schema = {}
        layout_query: dict[str, Any] = {}

        if layout_name:
            schema = _obj_to_dict(await component_model.by_name(layout_name))
            layout_query = {"rec_name": layout_name}

        if not schema:
            layout_query = await self._default_query(
                component_model,
                {"type": "layout"},
            )
            layouts = await self._find_base(
                component_model,
                query=layout_query,
                limit=1,
            )
            if layouts:
                schema = _obj_to_dict(layouts[0])
                layout_name = schema.get("rec_name", layout_name)

        menu_data = await self.service_get_menu()
        return ResponseObjectData(
            mode="layout",
            query=layout_query,
            data={
                "layout": layout_name,
                "schema": schema,
                "menu": menu_data.data,
                "settings": {
                    "module_name": getattr(self.settings, "module_name", ""),
                    "version": getattr(self.settings, "version", ""),
                    "logo_img_url": getattr(self.settings, "logo_img_url", ""),
                },
            },
        )

    async def service_get_menu(self, parent: str = "") -> ResponseObjectData:
        is_admin = bool(getattr(self.session, "is_admin", False))
        if not is_admin:
            return ResponseObjectData(
                mode="menu",
                data=[{}],
                query={"admin": True, "parent": parent},
            )
        action_model = self.env.get("action")
        menu_group_model = self.env.get("menu_group")

        menu_group_base: dict[str, Any]
        if parent:
            menu_group_base = {"$and": [{"admin": True}, {"parent": parent}]}
        else:
            menu_group_base = {"admin": True}
        menu_group_query = await self._default_query(
            menu_group_model,
            menu_group_base,
        )
        menu_groups = await self._find_base(
            menu_group_model,
            query=menu_group_query,
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for menu_group in (_obj_to_dict(item) for item in menu_groups):
            group_name = menu_group.get("rec_name", "")
            if not group_name:
                continue
            menu_query_user = await self._make_query_user(
                [{"action_type": "menu"}, {"menu_group": group_name}]
            )
            menu_query = await self._default_query(
                action_model, {"$and": menu_query_user}
            )
            menu_actions = await self._find_base(action_model, query=menu_query)
            if not menu_actions:
                continue

            label = menu_group.get("label") or "No Menu"
            grouped.setdefault(label, [])
            for rec_item in (_obj_to_dict(action) for action in menu_actions):
                grouped[label].append(self._make_button_main_menu(rec_item))

        data = [grouped] if grouped else [{}]
        return ResponseObjectData(mode="menu", data=data, query=menu_group_query)

    async def service_get_dashboard(self, parent: str = "") -> ResponseObjectData:
        action_model = self.env.get("action")
        menu_list = await self._get_basic_menu_list(parent=parent)
        list_cards: list[dict[str, Any]] = []

        for card in menu_list:
            if card.get("model"):
                c_model = await self._safe_model(card.get("model", ""))
                if not c_model:
                    continue

                q_menu_user = await self._make_query_user(
                    [
                        {"action_type": "menu"},
                        {"component_type": {"$in": ["form", "resource", "layout"]}},
                        {"$and": [{"menu_group": card.get("menu_group", "")}]},
                    ]
                )
                q_user = await self._make_query_user(
                    [
                        {"action_type": {"$in": ["window", "process_task"]}},
                        {"component_type": {"$in": ["form", "resource", "layout"]}},
                        {"$and": [{"menu_group": card.get("menu_group", "")}]},
                    ]
                )

                q_menu = await self._default_query(
                    action_model, {"$and": q_menu_user}
                )
                q = await self._default_query(action_model, {"$and": q_user})

                menu_actions = await self._find_base(action_model, query=q_menu)
                act_list = await self._find_base(action_model, query=q)
                card_buttons: list[dict[str, Any]] = []

                for rec_b in (_obj_to_dict(item) for item in menu_actions):
                    item = await self._make_menu_item(card, rec_b)
                    if item:
                        card_buttons.append(item)

                for rec_b in (_obj_to_dict(item) for item in act_list):
                    item = await self._make_menu_item(card, rec_b)
                    if item:
                        card_buttons.append(item)

                list_cards.append(
                    {
                        "model": card.get("model", ""),
                        "group_id": card.get("menu_group", ""),
                        "title": card.get("label", ""),
                        "buttons": card_buttons,
                    }
                )
            else:
                list_cards.append(
                    {
                        "model": card.get("menu_group", ""),
                        "group_id": card.get("menu_group", ""),
                        "title": card.get("label", ""),
                        "buttons": [card.copy()],
                    }
                )

        return ResponseObjectData(
            mode="card",
            model="action",
            query={"parent": parent},
            data=list_cards,
        )

    async def service_handle_action_get(
            self,
            action_name: str,
            rec_name: str = "",
            query: dict[str, Any] = None,
            order: str = "",
            skip: int = 0,
            limit: int = 100,
    ) -> ResponseObjectData:
        return await self.action_runtime.handle_get(
            action_name=action_name,
            rec_name=rec_name,
            query=query,
            order=order,
            skip=skip,
            limit=limit,
        )

    async def service_get_next_action_redirect(
            self,
            curr_action: str,
            rec_name: str = "",
    ) -> str:
        logger.info(
            "service.next_action_redirect curr_action=%s rec_name=%s",
            curr_action,
            rec_name,
        )
        action = await self._get_action_record(curr_action)
        if not action:
            logger.warning(
                "service.next_action_redirect action not found curr_action=%s",
                curr_action,
            )
            return ""
        next_action = (_field(action, "next_action_name", "") or "").strip()
        if not next_action:
            logger.info(
                "service.next_action_redirect no next_action curr_action=%s",
                curr_action,
            )
            return ""

        # Safety check: redirect only if the next action exists.
        next_action_record = await self._get_action_record(next_action)
        if not next_action_record:
            logger.warning(
                "service.next_action_redirect next action not found curr_action=%s next_action=%s",
                curr_action,
                next_action,
            )
            return ""

        target = f"/action/{next_action}"
        curr_rec_name = rec_name.strip()
        if curr_rec_name:
            return f"{target}/{curr_rec_name}"
        return target

    async def service_handle_action_post(
            self,
            action_name: str,
            data: dict[str, Any],
            rec_name: str = "",
    ) -> ResponseObjectData:
        return await self.action_runtime.handle_post(
            action_name=action_name,
            data=data,
            rec_name=rec_name,
        )

    async def service_handle_action_delete(
            self,
            action_name: str,
            rec_name: str,
            data: dict[str, Any] = None,
    ) -> ResponseObjectData:
        return await self.action_runtime.handle_delete(
            action_name=action_name,
            rec_name=rec_name,
            data=data,
        )
