from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from ozonenv.core.BaseModels import CoreModel

from app.core.OzonEnvApp import RUNTIME_MODEL_NAME_PATTERN
from app.services.common import ResponseObjectData
from app.services.common import make_response_object

logger = logging.getLogger("uvicorn.error")


def _normalize_sort_string(value: Any) -> str:
    """Accetta solo stringhe di sort runtime non vuote."""

    if isinstance(value, str):
        return value.strip()
    return ""


def _is_enabled_flag(value: Any) -> bool:
    """Interpreta flag persistiti come bool/int/str senza ambiguita su '0'."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"", "0", "false", "no", "off", "none", "null"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def action_groups(action: Any) -> set[str]:
    """Gruppi dichiarati sull'action, normalizzati lowercase.

    Accetta sia lista/set/tupla sia stringa comma-separated (il builder
    persiste `groups` come lista, ma config legacy usa la stringa).
    Set vuoto = action senza scope esplicito.
    """

    raw = getattr(action, "groups", None) or []
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, set, tuple)):
        values = list(raw)
    else:
        return set()
    return {str(item).strip().lower() for item in values if str(item).strip()}


def _merge_query(
        base_query: dict[str, Any], extra_query: dict[str, Any]
) -> dict[str, Any]:
    if not base_query:
        return extra_query.copy() if isinstance(extra_query, dict) else {}
    if not extra_query:
        return base_query.copy()
    return {"$and": [base_query.copy(), extra_query.copy()]}


def _merge_query_flat(
        base_query: dict[str, Any], extra_query: dict[str, Any]
) -> dict[str, Any]:
    if not base_query:
        return extra_query.copy() if isinstance(extra_query, dict) else {}
    if not extra_query:
        return base_query.copy()

    merged_and: list[dict[str, Any]] = []
    for query in (base_query, extra_query):
        if isinstance(query, dict) and isinstance(query.get("$and"), list):
            merged_and.extend(
                item.copy() if isinstance(item, dict) else item
                for item in query["$and"]
            )
        else:
            merged_and.append(query.copy())
    return {"$and": merged_and}


# Operazioni CRUD (model_groups_rule) richieste da un action_type. Un
# `save` puo' essere sia insert sia update: basta una delle due, la
# distinzione precisa la fa `Service.upsert` sul payload reale.
_ACTION_TYPE_OPERATIONS: dict[str, tuple[str, ...]] = {
    "save": ("create", "update"),
    "copy": ("create",),
    "delete": ("delete",),
}


def _apply_access_flags(
        response: ResponseObjectData, flags: dict[str, bool]
) -> None:
    """Scrive readable/editable/can_create sulla response.

    `make_response_object` li lascia a `True` di default: sulle response di
    scrittura sono l'unica cosa che dice al client se il form appena
    salvato e' ancora modificabile."""
    response.readable = flags["readable"]
    response.editable = flags["editable"]
    response.can_create = flags["can_create"]


def _runtime_component_visibility_query() -> dict[str, Any]:
    return {"rec_name": {"$regex": RUNTIME_MODEL_NAME_PATTERN}}


class ActionRuntime:
    """
    Runtime action service extracted from legacy ServiceAction semantics:
    - action by rec_name
    - model/view_name split (data on model, schema on view_name)
    - submit sequence hints for client
    """

    def __init__(self, service):
        self.service = service

    async def _is_action_allowed(self, action: CoreModel) -> bool:
        """Gate completo: visibilita' (gruppi) + CRUD (`write_access` e
        `action_type`).

        Da usare dove l'action va eseguita o mostrata come pulsante. Il GET
        di un form usa invece i due gate separati: senza scrittura il form
        si apre in readonly, non 403 (vedi `handle_get`).
        """
        if not await self._is_action_visible(action):
            return False
        return await self._has_action_write_access(action)

    async def _is_action_visible(self, action: CoreModel) -> bool:
        """Gate di sola visibilita': gruppi dell'action / read model-level."""
        session = getattr(self.service, "session", None)
        if not session:
            return True
        is_admin = bool(getattr(session, "is_admin", False))
        if is_admin:
            return True

        user = getattr(session, "user", None) or {}
        user_groups = {
            str(g or "").strip().lower()
            for g in (user.get("groups", []) if isinstance(user, dict) else [])
            if str(g or "").strip()
        }

        # Check action-specific groups if defined
        groups = action_groups(action)

        if groups:
            # If groups are explicitly set, the user must belong to at least one of them
            allowed = bool(user_groups & groups)
        elif getattr(action, "admin", False) or getattr(action, "sys", False):
            # Nessun override esplicito: per le action sys/admin, la visibilita'
            # e' decisa dal gate CRUD model-level (model_groups_rule) sul model
            # target dell'action, non piu' da un euristica hardcoded — un
            # model in IDENTITY_MODEL_NAMES resta admin-only "gratis" perche'
            # normalize_component_properties non gli inietta mai default
            # models_groups (nessuna riga -> model_group_access fail-closed
            # nega tutti i non-admin), senza bisogno di un check dedicato qui.
            model_name = getattr(action, "model", "") or ""
            if not model_name:
                allowed = "technical_operator" in user_groups
            else:
                access = await self.service._get_model_group_access(model_name)
                allowed = bool(access.get("read", False))
        else:
            allowed = True

        return allowed

    @staticmethod
    def _action_required_operations(action: CoreModel) -> tuple[str, ...]:
        """Operazioni CRUD che l'action richiede sul model target.

        Due sorgenti, in unione:
        - `write_access: true` -> scrittura generica (create|update): il
          flag dichiara che l'action porta a una scrittura. `write` non
          esiste come chiave in model_groups_rule, la scrittura e'
          `create or update`; la distinzione precisa la fa a valle
          `Service.upsert` (insert -> create, update -> update).
        - `action_type` -> l'operazione che l'action esegue davvero
          (save/copy/delete). Serve perche' nei dati queste action hanno
          `write_access: false`: senza, i pulsanti Salva/Duplica/Elimina
          restavano visibili a chi non puo' eseguirli.

        Tupla vuota = nessun permesso di scrittura richiesto (menu,
        window di sola lettura): il gate non si applica.
        """
        operations: set[str] = set()
        if _is_enabled_flag(getattr(action, "write_access", False)):
            operations.update(("create", "update"))
        action_type = str(getattr(action, "action_type", "") or "").strip().lower()
        operations.update(_ACTION_TYPE_OPERATIONS.get(action_type, ()))
        return tuple(sorted(operations))

    async def _has_action_write_access(self, action: CoreModel) -> bool:
        """Gate CRUD model-level (`model_groups_rule`) sulle operazioni
        richieste dall'action (vedi `_action_required_operations`).

        L'appartenenza al gruppo dell'action NON basta: un utente del solo
        gruppo `user` — che nei default ha create/update/delete `false` —
        vede le action di sola lettura del suo gruppo ma non quelle che
        scrivono.

        Basta UNA delle operazioni richieste, anche quando le sorgenti
        sono due (create|update per una save: chi puo' solo creare vede
        comunque il pulsante, l'update lo blocca `Service.upsert`). Quindi
        un'action che dichiara sia `write_access` sia un `action_type` che
        scrive e' piu' permissiva della somma dei due gate presi singoli:
        nei dati base non esiste (il flag e' solo su action `window`), ma
        se servisse un AND va scritto esplicitamente.

        Clamp a senso unico: nessuna operazione richiesta significa
        "questo gate non dice niente", non concede niente di suo.

        Fail-closed se l'action richiede scrittura ma non ha model.
        """
        operations = self._action_required_operations(action)
        if not operations:
            return True
        if getattr(self.service, "session", None) is None:
            # Nessuna sessione: stessa scelta di `_is_action_visible`
            # (chiamate interne senza attore, non richieste utente).
            return True
        model_name = str(getattr(action, "model", "") or "").strip()
        if not model_name:
            logger.info(
                "acl.action_write_access action=%s denied: operazioni %s senza model",
                getattr(action, "rec_name", ""),
                operations,
            )
            return False
        access = await self.service._get_model_group_access(model_name)
        granted = any(access.get(operation, False) for operation in operations)
        if not granted:
            logger.info(
                "acl.action_write_access action=%s model=%s operations=%s "
                "denied access=%s",
                getattr(action, "rec_name", ""),
                model_name,
                operations,
                access,
            )
        return granted

    async def get_action_record(self, action_name: str) -> CoreModel | None:
        action_model = self.service.env.get("action")
        return await action_model.by_name(action_name)

    async def _resolve_action_sequence(
            self,
            action_name: str,
            action: CoreModel,
    ) -> dict[str, str]:
        submit_action = action.next_action_name or ""
        submit_next = ""
        if submit_action:
            next_action = await self.get_action_record(submit_action)
            if next_action:
                submit_next = next_action.next_action_name or ""

        return {
            "current_action": action_name,
            "submit_action": submit_action,
            "submit_next_action": submit_next,
        }

    async def _get_context_actions(
            self, action_model: str, action_mode: str, component_type: str = ""
    ) -> list[dict[str, Any]]:
        """Restituisce i pulsanti di contesto visibili all'utente corrente.

        Logica:
        - Carica tutte le action con model == action_model, deleted=0, active=True.
        - Filtra per context_button_mode (str o list) che include action_mode.
        - context_button_mode vuoto o assente non abilita mai il pulsante.
        - Se component_type valorizzato: esclude action con component_type diverso
          (action senza component_type passano sempre — sono valide per tutti i tipi).
        - Applica controlli permesso:
            admin        → solo se is_admin
            write_access → solo se utente autenticato (not is_public)
            no_public_user → solo se not is_public
        """
        if not action_model or not action_mode:
            return []
        try:
            action_model_obj = self.service.env.get("action")
            query = {
                "$and": [
                    {"model": action_model},
                    {"deleted": 0},
                    {"active": True},
                ]
            }
            candidates = await action_model_obj.find(
                domain=query,
                sort="list_order:asc,rec_name:asc",
                limit=0,
            )
            session = self.service.session
            is_public = bool(getattr(session, "is_public", False))

            buttons: list[dict[str, Any]] = []
            for action in candidates:
                rec_name = action.rec_name or ""
                action_type = str(action.action_type or "").strip().lower()
                cbm = action.context_button_mode
                modes: list[str] = []
                if isinstance(cbm, list):
                    modes = [
                        str(mode).strip().lower()
                        for mode in cbm
                        if str(mode).strip()
                    ]
                elif isinstance(cbm, str):
                    modes = [
                        mode.strip().lower()
                        for mode in cbm.split(",")
                        if mode.strip()
                    ]
                if action_mode not in modes:
                    continue

                # --- component_type filter ---
                # Action con component_type valorizzato → solo se matcha il tipo corrente.
                # Action senza component_type → valida per tutti i tipi.
                action_ct = str(action.component_type or "").strip()
                if action_ct and component_type and action_ct != component_type:
                    continue

                # --- permission checks ---
                if not await self._is_action_allowed(action):
                    continue
                if action.write_access and is_public:
                    continue
                if action.no_public_user and is_public:
                    continue
                action_root = action.action_root_path or "/action"

                # Actions that operate on a specific record use /path/rec/rec.
                _rec_action_types = {"save", "copy", "delete"}
                if action_type in _rec_action_types:
                    url_action = f"{action_root}/{rec_name}/{rec_name}"
                else:
                    url_action = f"{action_root}/{rec_name}"

                buttons.append({
                    "rec_name": rec_name,
                    "action_type": action_type,
                    "label": action.title or rec_name,
                    "button_icon": action.button_icon or "",
                    "modal": bool(action.modal),
                    "url_action": url_action,
                    "context_button_mode": cbm,
                })
            return buttons
        except Exception:
            # Lista vuota = "nessun pulsante" per il client: un errore qui
            # non deve restare senza causa nei log.
            logger.exception(
                "context_actions lookup failed model=%s mode=%s",
                action_model,
                action_mode,
            )
            return []

    async def _get_fast_search_config(
            self, action_name: str, action_model: str
    ) -> dict[str, Any] | None:
        try:
            fs_model = self.service.env.get("fast_search_config")
            record = await fs_model.load({"model": action_name, "deleted": 0})
            if not record:
                return None
            search_form_name = str(record.searchForm or "").strip()
            if not search_form_name:
                return None
            schema = await self.service._get_component_record(search_form_name)
            return {
                "model": action_model,
                "schema": schema.components if schema else [],
                "fast_serch_model": search_form_name,
            }
        except Exception:
            logger.warning(
                "fast_search_config lookup failed action=%s", action_name
            )
            return None

    async def _get_fast_actions_config(
            self, action_name: str, action_model: str
    ) -> dict[str, Any] | None:
        try:
            fa_model = self.service.env.get("fast_actions_config")
            record = await fa_model.load({"model": action_name, "deleted": 0})
            if not record:
                return None
            actions_form_name = str(record.actionsForm or "").strip()
            if not actions_form_name:
                return None
            schema = await self.service._get_component_record(actions_form_name)
            return {
                "model": action_model,
                "schema": schema.components if schema else [],
                "fast_actions_model": actions_form_name,
            }
        except Exception:
            logger.warning(
                "fast_actions_config lookup failed action=%s", action_name
            )
            return None

    def _resolve_list_defaults(
            self,
            action: CoreModel,
    ) -> tuple[dict[str, Any], str]:
        """
        Risolve query/order base per action list.
        list_query/listOrderString sono campi dello schema `action`,
        quindi la sorgente e' sempre il record action (non il component
        schema del data-model, che non li dichiara).
        """

        action_query_raw = action.list_query
        if action_query_raw in (None, ""):
            action_query_raw = getattr(action, "query", None)
        resolved_query = self.service._resolve_query_json_logic_vars(
            self.service._parse_query_dict(action_query_raw)
        )
        resolved_order = _normalize_sort_string(action.listOrderString)
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
        if not await self._is_action_visible(action):
            raise HTTPException(
                status_code=403,
                detail=f"Action '{action_name}' is restricted",
            )
        # Gate scrittura separato dalla visibilita': su un form l'assenza di
        # permesso di scrittura NON nega l'apertura, la degrada a readonly
        # (il pulsante che ci porta e' comunque nascosto da
        # `_get_context_actions`, che usa il gate completo). Fuori dal form
        # non c'e' niente da degradare -> nega.
        write_allowed = await self._has_action_write_access(action)
        if not write_allowed and action.mode != "form":
            raise HTTPException(
                status_code=403,
                detail=f"Action '{action_name}' is restricted",
            )

        action_mode = action.mode
        action_model = action.model
        action_view_name = action.view_name
        action_component_type = action.component_type
        record_type = action.type
        target_model = action_model
        schema_model = action_view_name or action_model
        schema_record = await self.service._get_component_record(schema_model)
        payload_query = query.copy() if isinstance(query, dict) else {}

        # 1. Read component properties
        resolved_query = {}
        resolved_sort = ""

        schema_properties = getattr(schema_record, "properties", None) or {}
        if isinstance(schema_properties, str):
            try:
                import json
                schema_properties = json.loads(schema_properties)
            except Exception:
                schema_properties = {}

        if isinstance(schema_properties, dict):
            # query from queryformeditable
            q_val = schema_properties.get("queryformeditable")
            if q_val:
                if isinstance(q_val, str):
                    try:
                        import json
                        resolved_query = json.loads(q_val)
                    except Exception:
                        resolved_query = {}
                elif isinstance(q_val, dict):
                    resolved_query = q_val.copy()
                resolved_query = self.service._resolve_query_json_logic_vars(
                    resolved_query
                )
            # sort from sort
            resolved_sort = str(schema_properties.get("sort") or "").strip()

        # 2. Read action
        action_query_raw = getattr(action, "list_query", None)
        if action_query_raw in (None, ""):
            action_query_raw = getattr(action, "query", None)
        action_query = self.service._resolve_query_json_logic_vars(
            self.service._parse_query_dict(action_query_raw)
        )
        if action_query:  # if configured on action, replace
            resolved_query = action_query

        action_sort_raw = getattr(action, "listOrderString", None)
        if action_sort_raw:  # if configured on action, replace
            action_sort = _normalize_sort_string(action_sort_raw)
            if action_sort:
                resolved_sort = action_sort

        runtime_order = order.strip() if isinstance(order, str) else ""
        effective_order = runtime_order or resolved_sort

        if record_type == "component" and action_component_type:
            component_base_query = {
                "$and": [
                    {"deleted": 0},
                    {"active": True},
                    {"type": action_component_type},
                ]
            }
            component_base_query = _merge_query_flat(
                component_base_query,
                _runtime_component_visibility_query(),
            )
            component_query = _merge_query_flat(
                component_base_query,
                _merge_query(resolved_query, payload_query),
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
                merged_query = _merge_query(resolved_query, payload_query)
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
                    # Form nuovo record: nessun record da caricare, quindi
                    # nessun passaggio da load_record (che per i record
                    # esistenti calcola gia' editable = model+record update).
                    # Il permesso qui e' `create` a livello di MODEL (stessa
                    # sorgente di Service.response_access_flags, senza record
                    # da valutare): senza, il form si apre in sola lettura
                    # invece che editabile per default. `readable` non viene
                    # toccato: la visibilita' e' gia' decisa dal gate
                    # sull'action.
                    can_create = (
                        await self.service.response_access_flags(target_model)
                    )["can_create"]
                    res = ResponseObjectData(
                        mode="form",
                        model=target_model,
                        data={},
                        query=payload_query,
                        editable=can_create,
                        can_create=can_create,
                    )

        # view_name override only affects schema, not data model.
        schema_components = (
            schema_record.components if schema_record else None
        )
        if isinstance(schema_components, list):
            res.response_schema = schema_components
        schema_properties = getattr(schema_record, "properties", None) or {}
        if isinstance(schema_properties, str):
            try:
                import json
                schema_properties = json.loads(schema_properties)
            except Exception:
                schema_properties = {}
        if isinstance(schema_properties, dict):
            res.properties = schema_properties
        if target_model:
            res.model = target_model

        action_sequence = await self._resolve_action_sequence(action_name, action)
        response_fields = {
            **(res.fields if isinstance(res.fields, dict) else {}),
            "action_name": action_name,
            "action_model": action_model,
            "action_type": action.action_type,
            "component_type": action_component_type,
            "action_sequence": action_sequence,
            "submit_action_name": action_sequence.get("submit_action", ""),
        }
        if action_mode == "form":
            # Explicit alias requested by client: next action used by submit.
            response_fields["next_action_name"] = action_sequence.get(
                "submit_action", ""
            )
            response_fields["cancel_button"] = not _is_enabled_flag(
                getattr(schema_record, "no_cancel", False)
            )
            res.title = str(schema_record.title or "") if schema_record else ""
        if action_mode == "list":
            fs_config = await self._get_fast_search_config(action_name, action_model)
            if fs_config:
                response_fields["fast_search"] = fs_config
            fa_config = await self._get_fast_actions_config(action_name, action_model)
            if fa_config:
                response_fields["fast_actions"] = fa_config
            res.title = str(action.title or "")
        res.fields = response_fields
        res.context_actions = await self._get_context_actions(
            action_model, action_mode, component_type=action_component_type
        )
        res.query = resolved_query
        res.sort = resolved_sort
        if not write_allowed:
            # Clamp a senso unico: toglie l'editabilita', non la concede mai
            # (readable/editable calcolati a monte da load_record/list_records
            # restano se gia' piu' restrittivi).
            res.editable = False
            res.can_create = False
        return res

    async def handle_post(
            self,
            action_name: str,
            data: dict[str, Any],
            rec_name: str = "",
    ) -> ResponseObjectData:
        logger.info(f"handle_post {action_name} rec_name {rec_name} data {data.get('rec_name', False)}")
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
        if not await self._is_action_allowed(action):
            raise HTTPException(
                status_code=403,
                detail=f"Action '{action_name}' is restricted",
            )

        target_model = action.model
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
        action_type = action.action_type
        sync_component_runtime = (
            target_model == "component"
            and _is_enabled_flag(action.builder_enabled)
        )
        generate_component_defaults = sync_component_runtime

        next_action_name = (action.next_action_name or "").strip()
        next_action_url = f"/action/{next_action_name}" if next_action_name else ""

        if action_type == "delete":
            result = await self.handle_delete(
                action_name, target_rec_name, data=payload
            )
            result.next_action_url = next_action_url
            return result

        if action_type == "copy":
            target_rec_name = data.get('rec_name', False)
            action = await self.get_action_record(action_name)
            target_model = action.model
            logger.info(f"Copy {target_rec_name} target_model {target_model}")
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
            # La copia crea un nuovo record senza passare da Service.upsert
            # (che il gate create ce l'ha): va controllato qui. Serve anche
            # il READ sul record sorgente — `model.copy` lo legge per
            # rec_name senza nessun gate, e `handle_post` e' raggiungibile
            # con un rec_name arbitrario.
            await self.service._assert_model_operation(
                target_model, "create", action_name
            )
            await self.service._assert_model_operation(
                target_model, "read", action_name
            )
            source_record = await self.service.env.get(target_model).by_name(
                source_name
            )
            if source_record:
                await self.service._assert_record_operation(
                    target_model, source_record, "read", action_name
                )
            # duplica nativa ozon-env: clone con nuovo id + rec_name "_copy",
            # NON salvato (l'originale non viene toccato). Il record torna nel
            # form: si salva solo se l'utente fa submit -> niente record fantasma.
            model_obj = self.service.env.get(target_model)
            record = await model_obj.copy({"rec_name": source_name})

            if record is None:
                logger.error(f"duplication Error {model_obj.status.msg}")
                return ResponseObjectData(
                    mode="action",
                    data={
                        "status": "error",
                        "message": getattr(
                            getattr(model_obj, "status", None), "msg", ""
                        ) or f"Copy of '{source_name}' failed",
                    },
                    readable=False,
                    editable=False,
                )
            record =  await model_obj.upsert(record)
            next_action_url = f"{next_action_url}/{record.rec_name}"
            logger.info(f"duplicate  {record.rec_name} to url {next_action_url}")
            result = make_response_object(
                model_obj, mode="form", data=record
            ).content
            result.next_action_url = next_action_url
            result.context_actions = await self._get_context_actions(
                target_model,
                "form",
                component_type=getattr(action, "component_type", "") or "",
            )
            _apply_access_flags(
                result,
                await self.service.response_access_flags(target_model, record),
            )
            return result


        saved = await self.service.upsert(
            target_model,
            payload,
            rec_name=target_rec_name,
            sync_component_runtime=sync_component_runtime,
            generate_component_defaults=generate_component_defaults,
        )
        result = saved.content
        result.next_action_url = next_action_url
        # Anche le response di scrittura portano i pulsanti: il client li
        # ridisegna dopo il salva e una lista vuota qui significherebbe
        # "nessun pulsante calcolato", non "nessun pulsante permesso".
        result.context_actions = await self._get_context_actions(
            target_model,
            str(getattr(action, "mode", "") or "form"),
            component_type=getattr(action, "component_type", "") or "",
        )
        _apply_access_flags(
            result,
            await self.service.response_access_flags(target_model, result.data),
        )
        return result

    async def handle_delete(
            self,
            action_name: str,
            rec_name: str,
            data: dict[str, Any] = None,
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
        if not await self._is_action_allowed(action):
            raise HTTPException(
                status_code=403,
                detail=f"Action '{action_name}' is restricted",
            )

        target_model = action.model
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

        if not rec_name:
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": "Missing rec_name for delete action",
                },
                readable=False,
                editable=False,
            )

        await self.service._assert_model_operation(target_model, "delete", action_name)

        model_obj = self.service.env.get(target_model)
        record = await model_obj.by_name(rec_name)
        if not record:
            return ResponseObjectData(
                mode="action",
                data={
                    "status": "error",
                    "message": f"Record '{rec_name}' not found",
                },
                readable=False,
                editable=False,
            )

        await self.service._assert_record_operation(
            target_model, record, "delete", action_name
        )

        # soft delete nativo ozon-env (calcola il timestamp + update).
        await model_obj.set_to_delete(record)

        flags = await self.service.response_access_flags(target_model)
        return ResponseObjectData(
            mode="action",
            model=target_model,
            data={"status": "ok"},
            readable=False,
            editable=False,
            # can_create resterebbe al default True: se il client ci disegna
            # sopra un pulsante "Nuovo" sarebbe un fail-open.
            can_create=flags["can_create"],
        )
