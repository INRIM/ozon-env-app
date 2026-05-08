import logging
from typing import Any

from app.services.common import ResponseObjectData

logger = logging.getLogger("uvicorn.error")


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


def _safe_order_value(value: Any) -> str:
    """Normalizza un valore order: accetta solo stringhe non vuote."""

    if isinstance(value, str):
        return value.strip()
    return ""


def _merge_query(
        base_query: dict[str, Any], extra_query: dict[str, Any]
) -> dict[str, Any]:
    if not base_query:
        return extra_query.copy() if isinstance(extra_query, dict) else {}
    if not extra_query:
        return base_query.copy()
    return {"$and": [base_query.copy(), extra_query.copy()]}


class ActionRuntime:
    """
    Runtime action service extracted from legacy ServiceAction semantics:
    - action by rec_name
    - model/view_name split (data on model, schema on view_name)
    - submit/abandon sequence hints for client
    """

    def __init__(self, service):
        self.service = service
        self.env = service.env

    async def get_action_record(self, action_name: str):
        action_model = self.env.get("action")
        return await action_model.by_name(action_name)

    async def _resolve_abandon_action(
            self,
            action_name: str,
            action: Any,
    ) -> str:
        action_mode = _field(action, "mode", "")
        action_model_name = _field(action, "model", "")
        if action_mode != "form":
            return ""

        if action_model_name:
            try:
                action_model = self.env.get("action")
                query = await self.service._default_query(
                    action_model,
                    {
                        "$and": [
                            {"model": action_model_name},
                            {"mode": "list"},
                            {"action_type": {"$in": ["menu", "window"]}},
                        ]
                    },
                )
                candidates = await action_model.find(
                    domain=query,
                    sort="list_order:asc,rec_name:asc",
                    limit=0,
                )
                for cand in (_obj_to_dict(item) for item in candidates):
                    if cand.get("next_action_name", "") == action_name:
                        return cand.get("rec_name", "")
                for cand in (_obj_to_dict(item) for item in candidates):
                    rec_name = cand.get("rec_name", "")
                    if rec_name:
                        return rec_name
            except Exception:
                logger.debug(
                    "unable to resolve abandon action for %s",
                    action_name,
                )

        if action_name.startswith("form_form_"):
            suffix = action_name[len("form_form_"):].strip()
            if suffix:
                return f"list_{suffix}"
        return ""

    async def _resolve_action_sequence(
            self,
            action_name: str,
            action: Any,
    ) -> dict[str, str]:
        submit_action = _field(action, "next_action_name", "") or ""
        submit_next = ""
        if submit_action:
            next_action = await self.get_action_record(submit_action)
            if next_action:
                submit_next = _field(next_action, "next_action_name", "") or ""

        abandon_action = await self._resolve_abandon_action(action_name, action)
        return {
            "current_action": action_name,
            "submit_action": submit_action,
            "submit_next_action": submit_next,
            "abandon_action": abandon_action,
        }

    async def _get_schema_components(self, schema_name: str) -> list[Any] | None:
        if not schema_name:
            return None
        try:
            component_model = self.env.get("component")
            schema_record = await component_model.by_name(schema_name)
            schema_data = _obj_to_dict(schema_record)
            components = schema_data.get("components")
            if isinstance(components, list):
                return components
        except Exception:
            logger.debug("schema component not found name=%s", schema_name)
        return None

    async def _get_fast_search_config(
            self, action_name: str, action_model: str
    ) -> dict[str, Any] | None:
        try:
            fs_model = self.service.env.get("fast_search_config")
            record = await fs_model.load({"model": action_name, "deleted": 0})
            if not record:
                return None
            data = _obj_to_dict(record)
            search_form_name = str(data.get("searchForm", "") or "").strip()
            if not search_form_name:
                return None
            compo_model = self.service.env.get("component")
            schema = await compo_model.by_name(search_form_name)
            schema_dict = _obj_to_dict(schema) if schema else {}
            return {
                "model": action_model,
                "schema": schema_dict.get("components", schema_dict),
                "fast_serch_model": search_form_name,
            }
        except Exception:
            logger.warning(
                "fast_search_config lookup failed action=%s", action_name
            )
            return None

    async def _get_component_record(self, component_name: str) -> dict[str, Any]:
        if not component_name:
            return {}
        try:
            component_model = self.env.get("component")
            record = await component_model.by_name(component_name)
            return _obj_to_dict(record)
        except Exception:
            logger.debug("component not found name=%s", component_name)
            return {}

    async def _resolve_list_defaults(
            self,
            action: Any,
            schema_model: str,
    ) -> tuple[dict[str, Any], str]:
        """
        Risolve query/order base per action list con precedenza:
        1) action
        2) component schema
        """

        action_query_raw = _field(action, "list_query", None)
        if action_query_raw in (None, ""):
            action_query_raw = _field(action, "query", None)
        action_query = self.service._parse_query_dict(action_query_raw)
        action_order = _safe_order_value(_field(action, "list_order", ""))
        if not action_order:
            action_order = _safe_order_value(_field(action, "order", ""))

        component_data = await self._get_component_record(schema_model)
        component_query = self.service._parse_query_dict(
            component_data.get("list_query", component_data.get("query", {}))
        )
        component_order = _safe_order_value(
            component_data.get("list_order", component_data.get("order", ""))
        )

        resolved_query = action_query if action_query else component_query
        resolved_order = action_order if action_order else component_order
        return resolved_query, resolved_order

    async def handle_get(
            self,
            action_name: str,
            rec_name: str = "",
            query: dict[str, Any] = None,
            order: str = "",
            skip: int = 0,
            limit: int = 100,
    ) -> ResponseObjectData:
        action = await self.get_action_record(action_name)
        if not action:
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": f"Action '{action_name}' not found",
                },
                readable=False,
                editable=False,
            )

        action_mode = _field(action, "mode", "")
        action_model = _field(action, "model", "")
        action_view_name = _field(action, "view_name", "")
        action_component_type = _field(action, "component_type", "")
        action_type = _field(action, "type", "")
        target_model = action_model
        schema_model = action_view_name or action_model
        payload_query = query.copy() if isinstance(query, dict) else {}
        resolved_list_query: dict[str, Any] = {}
        resolved_list_order: str = ""
        if action_mode == "list":
            resolved_list_query, resolved_list_order = (
                await self._resolve_list_defaults(action, schema_model)
            )
        runtime_order = order.strip() if isinstance(order, str) else ""
        effective_order = runtime_order or resolved_list_order

        if action_type == "component" and action_component_type:
            component_query = _merge_query(
                {
                    "$and": [
                        {"deleted": 0},
                        {"active": True},
                        {"type": action_component_type},
                    ]
                },
                _merge_query(resolved_list_query, payload_query),
            )
            if action_mode == "list":
                listed = await self.service.list_records(
                    model_name="component",
                    query=component_query,
                    order=effective_order,
                    skip=skip,
                    limit=limit,
                )
                res = listed.content
            else:
                if rec_name:
                    loaded = await self.service.load_record("component", rec_name)
                else:
                    loaded = await self.service.compo_by_name(
                        "component", action_model
                    )
                res = loaded.content
        else:
            if action_mode == "list":
                merged_query = _merge_query(resolved_list_query, payload_query)
                listed = await self.service.list_records(
                    model_name=target_model,
                    query=merged_query,
                    order=effective_order,
                    skip=skip,
                    limit=limit,
                )
                res = listed.content
            else:
                if rec_name:
                    loaded = await self.service.load_record(target_model, rec_name)
                    res = loaded.content
                else:
                    res = ResponseObjectData(
                        mode="form",
                        model=target_model,
                        data={},
                        query=payload_query,
                    )

        # view_name override only affects schema, not data model.
        schema_components = await self._get_schema_components(schema_model)
        if schema_components is not None:
            res.schema = schema_components
        if target_model:
            res.model = target_model

        action_sequence = await self._resolve_action_sequence(action_name, action)
        response_fields = {
            **(res.fields if isinstance(res.fields, dict) else {}),
            "action_name": action_name,
            "action_model": action_model,
            "action_type": _field(action, "action_type", ""),
            "component_type": action_component_type,
            "action_sequence": action_sequence,
            "submit_action_name": action_sequence.get("submit_action", ""),
            "abandon_action_name": action_sequence.get("abandon_action", ""),
        }
        if action_mode == "form":
            # Explicit alias requested by client: next action used by submit.
            response_fields["next_action_name"] = action_sequence.get(
                "submit_action", ""
            )
        if action_mode == "list":
            fs_config = await self._get_fast_search_config(action_name, action_model)
            if fs_config:
                response_fields["fast_search"] = fs_config
        res.fields = response_fields
        return res

    async def handle_post(
            self,
            action_name: str,
            data: dict[str, Any],
            rec_name: str = "",
    ) -> ResponseObjectData:
        action = await self.get_action_record(action_name)
        if not action:
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": f"Action '{action_name}' not found",
                },
                readable=False,
                editable=False,
            )

        target_model = _field(action, "model", "")
        if not target_model:
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": f"Action '{action_name}' has no target model",
                },
                readable=False,
                editable=False,
            )

        payload = data.copy() if isinstance(data, dict) else {}
        target_rec_name = rec_name or payload.get("rec_name", "")
        action_type = _field(action, "action_type", "")

        if action_type == "delete":
            if not target_rec_name:
                return ResponseObjectData(
                    mode="action",
                    data={
                        "status": "error",
                        "message": "Missing rec_name for delete action",
                    },
                    readable=False,
                    editable=False,
                )
            payload["deleted"] = 1
            saved = await self.service.upsert(
                target_model, payload, rec_name=target_rec_name
            )
            return saved.content

        if action_type == "copy":
            source_name = target_rec_name
            if not source_name:
                return ResponseObjectData(
                    mode="action",
                    data={
                        "status": "error",
                        "message": "Missing rec_name for copy action",
                    },
                    readable=False,
                    editable=False,
                )
            source = await self.service.load_record(target_model, source_name)
            source_data = source.content.data if source and source.content else {}
            if not isinstance(source_data, dict):
                source_data = _obj_to_dict(source_data)
            clone_rec_name = payload.get("rec_name", f"{source_name}_copy")
            source_data.pop("id", None)
            source_data["rec_name"] = clone_rec_name
            saved = await self.service.upsert(
                target_model, source_data, rec_name=clone_rec_name
            )
            return saved.content

        saved = await self.service.upsert(
            target_model, payload, rec_name=target_rec_name
        )
        return saved.content

    async def handle_delete(
            self,
            action_name: str,
            rec_name: str,
            data: dict[str, Any] = None,
    ) -> ResponseObjectData:
        payload = data.copy() if isinstance(data, dict) else {}
        payload["deleted"] = 1
        return await self.handle_post(
            action_name=action_name,
            data=payload,
            rec_name=rec_name,
        )
