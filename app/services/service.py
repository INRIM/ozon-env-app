import json
import logging
import re
from datetime import datetime
from types import UnionType
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from ozonenv.OzonEnv import OzonEnv
from ozonenv.core.BaseModels import CoreModel
from ozonenv.core.OzonModel import OzonModelBase

from .action_runtime import ActionRuntime
from .action_runtime import _is_enabled_flag
from .common import *
from .formio import get_formio_select_options
from app.app_settings import get_env_settings
from app.core.models import FieldAclEffect
from app.core.models import FieldAclOperation
from app.core.OzonModelApp import DateEngineApp
from app.core.service_manager import ServiceManagerCore
from app.core.service_registry import ServiceRegistryCore
from app.core.OzonEnvApp import normalize_component_properties
from app.core.webhooks import WebhookDispatcher
from app.services.message_queue import maybe_enqueue_on_save
from app.ozon_env_acl import CompiledFieldAcl
from app.ozon_env_acl import apply_record_rule_override
from app.ozon_env_acl import compile_field_acl_policies
from app.ozon_env_acl import enforce_write_acl
from app.ozon_env_acl import obfuscate_fields_in_place
from app.ozon_env_acl import record_rule_access
from app.ozon_env_acl import record_rule_read_domain
from app.ozon_env_acl import synth_policies_from_component_properties

logger = logging.getLogger("uvicorn.error")
_COMPONENT_RUNTIME_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_BOOLEAN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "active",
        "admin",
        "builder_enabled",
        "create_menu_dashboard",
        "default",
        "demo",
        "force_domain_query",
        "handle_global_change",
        "make_virtual_model",
        "no_cancel",
        "no_public_user",
        "sys",
        "write_access",
    }
)


def _normalize_order(order: str) -> str:
    """
    Normalizza la sintassi dell'ordinamento verso il formato ozon-env:
    - `field:asc|desc` (gia valido)
    - `-field` -> `field:desc`
    - `+field` / `field` -> `field:asc`
    - `field asc|desc` o `field+asc|desc` -> `field:asc|desc`
    Supporta piu campi separati da virgola.
    """

    if not order:
        return ""
    tokens = [token.strip() for token in order.split(",") if token.strip()]
    normalized: list[str] = []
    for token in tokens:
        token_clean = token.replace("+", " ").strip()
        if " " in token_clean:
            parts = [p.strip() for p in token_clean.split(" ") if p.strip()]
            if len(parts) == 2:
                field, direction = parts[0], parts[1].lower()
                if direction in ("asc", "desc"):
                    normalized.append(f"{field}:{direction}")
                    continue
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


def _has_non_system_records(records: list[Any]) -> bool:
    for record in records if isinstance(records, list) else []:
        if not _is_enabled_flag(getattr(record, "sys", False)):
            return True
    return False


def _is_admin_only_menu_group(group: CoreModel) -> bool:
    return _is_enabled_flag(group.admin)


def _is_runtime_component_name(name: Any) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return False
    return bool(_COMPONENT_RUNTIME_NAME_RE.fullmatch(normalized))


def _merge_query(
    base_query: dict[str, Any], extra_query: dict[str, Any]
) -> dict[str, Any]:
    if not base_query:
        return extra_query.copy() if isinstance(extra_query, dict) else {}
    if not extra_query:
        return base_query.copy()
    return {"$and": [base_query.copy(), extra_query.copy()]}


def _record_to_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return record.copy()
    if hasattr(record, "get_dict"):
        return record.get_dict()
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="python")
    if hasattr(record, "dict"):
        return record.dict()
    return {}


def _safe_json_dict(raw: Any) -> dict[str, Any]:
    """`model_fields_rule.filters` e' un campo testo (json editor), non un
    dict tipizzato via ORM — coerente con queryformeditable/altri campi
    JSON-in-textarea dell'app. Parse difensivo qui, non a monte."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("model_fields_rule.filters non e' JSON valido: %r", raw)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_boolean_payload_values(
    data: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return data
    normalized = data.copy()
    for key in _BOOLEAN_PAYLOAD_KEYS:
        if normalized.get(key, None) == "":
            normalized[key] = False
    return normalized


def _annotation_accepts_string(annotation: Any) -> bool:
    if annotation is str:
        return True
    origin = get_origin(annotation)
    if origin in {Union, UnionType}:
        return any(
            _annotation_accepts_string(arg) for arg in get_args(annotation)
        )
    return False


def _string_payload_keys(record_model: Any) -> set[str]:
    model = getattr(record_model, "model", None)
    model_fields = getattr(model, "model_fields", None)
    if isinstance(model_fields, dict):
        return {
            str(key)
            for key, field in model_fields.items()
            if _annotation_accepts_string(getattr(field, "annotation", None))
        }
    legacy_fields = getattr(model, "__fields__", None)
    if isinstance(legacy_fields, dict):
        return {
            str(key)
            for key, field in legacy_fields.items()
            if _annotation_accepts_string(
                getattr(field, "outer_type_", None)
                or getattr(field, "type_", None)
            )
        }
    return set()


def _normalize_string_payload_values(
    data: dict[str, Any] | None,
    record_model: Any,
) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return data
    string_keys = _string_payload_keys(record_model)
    if not string_keys:
        return data
    normalized = data.copy()
    for key in string_keys:
        value = normalized.get(key, None)
        if value is not None and not isinstance(value, str):
            if isinstance(value, (bool, int, float)):
                normalized[key] = str(value)
    return normalized


def _normalize_payload_values(
    data: dict[str, Any] | None,
    record_model: Any,
) -> dict[str, Any] | None:
    data = _normalize_boolean_payload_values(data)
    return _normalize_string_payload_values(data, record_model)


def _payload_requests_menu_dashboard(data: dict[str, Any] | None) -> bool:
    if not isinstance(data, dict):
        return False
    return _is_enabled_flag(data.get("create_menu_dashboard", False))


class Service:
    def __init__(self, env: OzonEnv):
        self.env = env
        self.session = env.user_session
        self.settings = env.orm.app_settings
        self.action_runtime = ActionRuntime(self)
        self.service_manager = ServiceManagerCore(env)
        self.service_registry = ServiceRegistryCore(env)
        self.webhooks = WebhookDispatcher.from_settings(get_env_settings())
        self._compiled_field_acl: CompiledFieldAcl | None = None
        self._record_rulse_cache: dict[str, list[dict[str, Any]]] = {}
        self._sys_model_cache: dict[str, bool] = {}
        self.date_engine = DateEngineApp()
        logger.info(
            "service initialized app_code=%s",
            self.session.app_code,
        )

    def _is_menu_group_allowed(self, group: CoreModel) -> bool:
        session = getattr(self, "session", None)
        if not session:
            return True
        is_admin = getattr(session, "is_admin", False)
        if is_admin:
            return True
            
        user = getattr(session, "user", None) or {}
        user_groups = set(user.get("groups", []) if isinstance(user, dict) else [])
        
        # Check menu-specific groups if defined
        group_groups_raw = getattr(group, "groups", None) or []
        if isinstance(group_groups_raw, str):
            group_groups = {g.strip() for g in group_groups_raw.split(",") if g.strip()}
        elif isinstance(group_groups_raw, (list, set, tuple)):
            group_groups = {str(g).strip() for g in group_groups_raw if str(g).strip()}
        else:
            group_groups = set()
            
        if group_groups:
            # If groups are explicitly set, the user must belong to at least one of them
            return bool(user_groups & group_groups)
            
        # By default check admin menu groups
        if getattr(group, "admin", False):
            # Check if it's the Identity layer menu group
            if getattr(group, "rec_name", "") == "identity":
                # Identity layer: accessible only to admin
                return False
            # Other system/admin menus: accessible to admin and technical_operator
            return "technical_operator" in user_groups
            
        return True

    def _get_model(self, model_name: str):
        normalized = str(model_name or "").strip()
        candidates = [normalized]
        lower = normalized[:1].lower() + normalized[1:] if normalized else ""
        if lower and lower not in candidates:
            candidates.append(lower)
        for candidate in candidates:
            model = self.env.get(candidate)
            if model is not None:
                file_dump_mode = get_env_settings().model_file_dump_mode.strip()
                set_file_dump_mode = getattr(model, "set_file_dump_mode", None)
                if file_dump_mode and callable(set_file_dump_mode):
                    set_file_dump_mode(file_dump_mode)
                return model
        raise HTTPException(
            status_code=404,
            detail=f"Model '{model_name}' not found",
        )

    async def get_models(self, query: dict = None):
        logger.info("service.get_models query=%s", query if query else {})
        compo_model = self.env.get("component")
        dynamic: list[str] = await compo_model.distinct(
            "rec_name", query if query else {}
        )
        if not isinstance(dynamic, list):
            dynamic = []
        static: list[str] = list(self.env.models.keys())
        # static first, dynamic appended (deduplication via dict.fromkeys)
        merged = list(dict.fromkeys(static + dynamic))
        logger.info(
            "service.get_models static=%d dynamic=%d total=%d",
            len(static),
            len(dynamic),
            len(merged),
        )
        return merged

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
        field_key,
        curr_model,
        schema_type: str = "formio",
    ):
        logger.info(
            "service.get_select_options field=%s model=%s schema_type=%s",
            field_key,
            curr_model,
            schema_type,
        )
        if schema_type == "formio":
            return await get_formio_select_options(
                self,
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

    async def register_service_repo(
        self,
        *,
        url: str,
        version: str = "main",
        manifest_path: str = "manifest.json",
        active: bool = True,
    ) -> dict[str, Any]:
        return await self.service_registry.register_repo(
            url=url,
            version=version,
            manifest_path=manifest_path,
            active=active,
        )

    async def register_service_manifest(
        self,
        manifest_data: dict[str, Any],
        *,
        manifest_path: str = "",
        source_path: str = "",
    ) -> dict[str, Any]:
        return await self.service_registry.register_manifest(
            manifest_data,
            manifest_path=manifest_path,
            source_path=source_path,
        )

    async def list_registered_services(
        self,
        query: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await self.service_registry.list_services(query=query)

    async def up_registered_service(
        self,
        code: str,
        *,
        build: bool = True,
    ) -> dict[str, Any]:
        return await self.service_registry.up(code, build=build)

    async def down_registered_service(self, code: str) -> dict[str, Any]:
        return await self.service_registry.down(code)

    def _camunda_gateway(self):
        from app.app_settings import get_env_settings
        from app.services.camunda import CamundaGateway

        return CamundaGateway(get_env_settings())

    async def start_camunda_gateway_process(
        self,
        process_key: str,
        payload: dict[str, Any] | None = None,
        *,
        update_data: bool = False,
        process_model: str = "",
    ) -> ResponseObject:
        async def save_form_record(
            model_name: str,
            record_payload: dict[str, Any],
        ) -> dict[str, Any]:
            response = await self.upsert(
                model_name,
                record_payload,
                rec_name=str(record_payload.get("rec_name", "") or ""),
            )
            saved = getattr(getattr(response, "content", None), "data", None)
            return _record_to_dict(saved)

        start_payload = payload or {}
        model_name = ""
        if update_data:
            _, process = await self.service_manager.load_process(process_key)
            configured_model = str(process.model or "").strip()
            requested_model = str(process_model or "").strip()
            if (
                configured_model
                and requested_model
                and configured_model != requested_model
            ):
                raise ValueError(
                    f"Camunda process '{process.rec_name}' is configured for model "
                    f"'{configured_model}', not '{requested_model}'"
                )
            model_name = requested_model or configured_model
            if not model_name:
                raise ValueError(
                    f"Camunda process '{process.rec_name}' has no model configured"
                )
            start_payload = await save_form_record(model_name, start_payload)

        result = await self.service_manager.start_camunda_process(
            process_key,
            start_payload,
            gateway=self._camunda_gateway(),
        )

        process_id = str(result.get("process_id", "") or "")
        if update_data and process_id:
            process_payload = start_payload.copy()
            process_payload["process_id"] = process_id
            saved_with_process = await save_form_record(
                model_name,
                process_payload,
            )
            result["form"] = saved_with_process

        # Attende il primo avanzamento: se il task successivo e' uno user task,
        # risponde "self" -> il form rletto (il client resta sul form, ora col
        # processo avviato). Errore dell'external task -> risposta d'errore.
        if process_id:
            rec_name = str(
                (result.get("form") or {}).get("rec_name")
                or start_payload.get("rec_name")
                or ""
            ).strip()
            client_resp = await self._camunda_after_start_response(
                process_id,
                model=model_name,
                rec_name=rec_name,
                update_data=update_data,
                has_payload=bool(payload),
            )
            if client_resp is not None:
                return client_resp

        # Nessuna risposta dal task (o wait disabilitato): form col processo
        # avviato. process_id/process_status danno al client le coordinate.
        model_obj = self._get_model(model_name) if model_name else None
        status = str((result.get("stato") or {}).get("status") or "started")
        return make_response_object(
            model_obj,
            mode="form",
            data=result.get("form") or None,
            process_id=str(result.get("process_id", "") or ""),
            process_status=status,
        )

    async def _camunda_after_start_response(
        self,
        process_id: str,
        *,
        model: str = "",
        rec_name: str = "",
        update_data: bool = False,
        has_payload: bool = False,
    ) -> ResponseObject | None:
        """Dopo lo start: attende il settle e costruisce la risposta client.
        - errore dell'external task -> risposta d'errore (dalla struct);
        - task successivo = user task -> "self": il form rletto (mode=form), cosi
          il client resta sul form col processo avviato (non redirige altrove).
        None se non c'e' nulla da gestire."""
        settings = get_env_settings()
        wait_seconds = float(
            getattr(settings, "camunda_complete_wait_seconds", 0) or 0
        )
        if wait_seconds <= 0:
            return None
        gateway = self._camunda_gateway()
        try:
            settle = await gateway.wait_until_settled(
                process_id,
                timeout_seconds=wait_seconds,
                interval_seconds=float(
                    getattr(settings, "camunda_poll_interval_seconds", 0.5)
                    or 0.5
                ),
            ) or {}
        except Exception:  # noqa: BLE001 - l'attesa non deve rompere lo start
            logger.exception(
                "camunda wait_until_settled (start) failed process=%s",
                process_id,
            )
            return None

        process_vars = settle.get("variables") or {}

        # Se il settle non porta la struct dell'ultimo task (variabili purgate
        # dal runtime dopo l'end del processo), prova a rileggerle dalla history.
        if not process_vars.get("last_task"):
            history_loader = getattr(
                gateway, "get_process_history_variables", None
            )
            if callable(history_loader):
                try:
                    history_vars = await history_loader(process_id)
                except Exception:  # noqa: BLE001 - fallback best-effort
                    logger.exception(
                        "camunda history variables failed process=%s",
                        process_id,
                    )
                    history_vars = {}
                if history_vars:
                    process_vars = history_vars
                    settle = {**settle, "variables": history_vars}

        # 1) errore dell'external task -> risposta d'errore.
        err_resp = self._camunda_error_response(process_vars, model=model)
        if err_resp is not None:
            return err_resp

        # 2) URL di redirect secondo le regole di start (vedi
        #    _resolve_start_redirect). "" -> nessun redirect -> form.
        next_url = await self._resolve_start_redirect(
            settle,
            model=model,
            rec_name=rec_name,
            update_data=update_data,
            has_payload=has_payload,
        )
        if next_url:
            model_obj = self._get_model(model) if model else None
            return make_response_object(
                model_obj,
                mode="redirect",
                next_action_url=next_url,
                process_id=process_id,
                process_status=str(settle.get("reason") or ""),
            )
        return None

    async def _resolve_camunda_action_url(
        self, model: str, rec_name: str = "", kind: str = "open"
    ) -> str:
        """URL di una action form per `model`, secondo convenzione:
        - kind="open" -> `form_form_{model}` (apre il record) ->
          `/action/form_form_{model}/{rec_name}`
        - kind="new"  -> `new_{model}` (nuovo) -> `/action/new_{model}`
        Verifica che la action esista (no URL morti); altrimenti "" (il caller
        ripiega su "self")."""
        model = str(model or "").strip()
        if not model:
            return ""
        action_name = (
            f"form_form_{model}" if kind == "open" else f"new_{model}"
        )
        action = await self._get_action_record(action_name)
        if not action:
            logger.warning(
                "camunda redirect: action %s inesistente per model=%s",
                action_name,
                model,
            )
            return ""
        rec_name = str(rec_name or "").strip()
        if kind == "open" and rec_name:
            return f"/action/{action_name}/{rec_name}"
        return f"/action/{action_name}"

    async def _resolve_start_redirect(
        self,
        settle: dict[str, Any],
        *,
        model: str = "",
        rec_name: str = "",
        update_data: bool = False,
        has_payload: bool = False,
    ) -> str:
        """Risolve l'URL di redirect dopo lo start del processo. Ritorna ""
        (nessun redirect, il caller mostra il form) oppure un token/URL.

        Regole (model/rec_name/update_data autoritativi dalla struct del task,
        fallback ai parametri di start):
        1. update_data + record salvato (rec_name) -> apre il form salvato:
           `/action/form_form_{model}/{rec_name}` (submit-like).
        2. payload ma NON update_data -> "self" (reload pagina corrente).
        3. nessun payload + task successivo user_task -> nuovo form del model del
           task: `/action/new_{model}`.
        next_page esplicito dal worker (non "self"/vuoto) -> verbatim.
        """
        process_vars = settle.get("variables") or {}
        last_task = str(process_vars.get("last_task") or "").strip()
        ctx = process_vars.get(last_task) if last_task else None
        ctx = ctx if isinstance(ctx, dict) else {}

        # next_page esplicito dal worker -> verbatim (link gia' risolto).
        next_page = str(ctx.get("next_page") or "").strip()
        if next_page and next_page != "self":
            return next_page

        s_model = str(ctx.get("model") or model or "").strip()
        s_rec = str(ctx.get("rec_name") or rec_name or "").strip()
        s_update = bool(ctx.get("update_data")) or bool(update_data)

        # Regola 1: form salvato -> aprilo.
        if s_update and s_rec and s_model:
            url = await self._resolve_camunda_action_url(
                s_model, s_rec, "open"
            )
            return url or "self"

        # Regola 2: payload senza update_data -> reload.
        if has_payload and not s_update:
            return "self"

        # Regola 3: niente payload + prossimo user task -> nuovo form del model.
        if not has_payload and settle.get("reason") == "user_task":
            task_model = s_model or str(
                process_vars.get("model") or ""
            ).strip()
            url = await self._resolve_camunda_action_url(
                task_model, "", "new"
            )
            return url or "self"

        return ""

    async def get_camunda_gateway_status(
        self,
        process_id: str,
    ) -> ResponseObject:
        result = await self.service_manager.camunda_status(
            process_id,
            gateway=self._camunda_gateway(),
        )
        stato = result.get("stato") or {}
        return make_response_object(
            None,
            mode="status",
            data=result.get("variables") or None,
            process_id=str(stato.get("process_id") or process_id or ""),
            process_status=str(stato.get("status") or ""),
        )

    async def complete_camunda_gateway_task(
        self,
        process_id: str,
        payload: dict[str, Any] | None = None,
        *,
        decision: str = "",
    ) -> ResponseObject:
        payload = payload or {}
        gateway = self._camunda_gateway()
        form = payload.get("form") if isinstance(payload.get("form"), dict) else {}
        variables = (
            payload.get("variables")
            if isinstance(payload.get("variables"), dict)
            else {}
        )
        model = str(
            form.get("model")
            or variables.get("model")
            or payload.get("model")
            or ""
        ).strip()
        rec_name = str(
            form.get("rec_name")
            or variables.get("rec_name")
            or payload.get("rec_name")
            or ""
        ).strip()
        data = form or variables

        # DIAG: capire perche' la response esce vuota (model non risolto / niente
        # struct redirect). Rimuovere dopo la diagnosi.
        logger.info(
            "DIAG complete: process_id=%s decision=%s payload_keys=%s "
            "form_keys=%s variables_keys=%s -> model=%r rec_name=%r",
            process_id,
            decision,
            list(payload.keys()),
            list(form.keys()),
            list(variables.keys()),
            model,
            rec_name,
        )

        # 1) salva le modifiche dell'utente sul form PRIMA del complete (cosi il
        #    record riflette lo stato dell'utente; l'eventuale worker external lo
        #    aggiornera' poi). Va fatto prima dell'attesa per evitare la race in
        #    cui sovrascriverebbe quanto scritto dal worker.
        if model and rec_name:
            await self.upsert(model, data, rec_name)

        # 2) completa il task camunda (manda le variabili a Camunda).
        complete_result = await self.service_manager.complete_camunda_task(
            process_id,
            payload,
            gateway=gateway,
            decision=decision,
        )

        # 3) attende che il flow avanzi oltre gli eventuali service/external task
        #    (fino al prossimo user task o alla fine del processo) prima di
        #    rispondere: il click "completa" torna solo dopo che l'external task
        #    ha risposto. Best-effort col timeout configurato.
        settings = get_env_settings()
        wait_seconds = float(
            getattr(settings, "camunda_complete_wait_seconds", 0) or 0
        )
        settle: dict[str, Any] = {}
        if wait_seconds > 0 and process_id:
            try:
                settle = await gateway.wait_until_settled(
                    process_id,
                    exclude_task_key=str(
                        complete_result.get("completed_task_key") or ""
                    ),
                    timeout_seconds=wait_seconds,
                    interval_seconds=float(
                        getattr(
                            settings, "camunda_poll_interval_seconds", 0.5
                        )
                        or 0.5
                    ),
                ) or {}
            except Exception:  # noqa: BLE001 - l'attesa non deve rompere il complete
                logger.exception(
                    "camunda wait_until_settled failed process=%s", process_id
                )

        # DIAG: cosa ha catturato il settle (reason + variabili di processo).
        _settle_vars = settle.get("variables") or {}
        logger.info(
            "DIAG settle: reason=%s settled=%s var_keys=%s last_task=%r "
            "last_task_struct=%r",
            settle.get("reason"),
            settle.get("settled"),
            list(_settle_vars.keys()),
            _settle_vars.get("last_task"),
            _settle_vars.get(str(_settle_vars.get("last_task") or "")),
        )

        # 4) risposta dal risultato dell'external task: la variabile col nome
        #    dell'ultimo task (variables["last_task"]) contiene la struttura con
        #    next_action/next_page/error. Costruisce la risposta client
        #    (redirect/errore); fallback al form rletto se non disponibile.
        client_resp = self._camunda_client_response(
            settle.get("variables") or {}, model=model, rec_name=rec_name
        )
        if client_resp is not None:
            return client_resp
        if model and rec_name:
            return await self.load_record(model, rec_name)
        if model:
            return make_response_object(
                self._get_model(model),
                mode="form",
                data={},
                process_id=process_id,
                process_status="completed",
            )
        return make_response_object(
            None,
            mode="status",
            data=variables or None,
            process_id=process_id,
            process_status="completed",
        )

    def _camunda_error_response(
        self, process_vars: dict[str, Any], *, model: str = ""
    ) -> ResponseObject | None:
        """Se la struct dell'ultimo task segnala errore -> response d'errore
        (mode=form, fail=True, message). Altrimenti None."""
        last_task = str(process_vars.get("last_task") or "").strip()
        ctx = process_vars.get(last_task) if last_task else None
        if not isinstance(ctx, dict) or not bool(ctx.get("error")):
            return None
        ctx_model = str(ctx.get("model") or model or "").strip()
        model_obj = self._get_model(ctx_model) if ctx_model else None
        return make_response_object(
            model_obj,
            mode="form",
            fail=True,
            message=str(ctx.get("msg") or f"Errore nel task {last_task}"),
            process_status="error",
        )

    def _camunda_client_response(
        self,
        process_vars: dict[str, Any],
        *,
        model: str = "",
        rec_name: str = "",
    ) -> ResponseObject | None:
        """Risposta client dalla struct dell'external task (chiavi legacy
        ProcessServiceCamunda), usata dal path di complete. None se non c'e' una
        struct usabile (il caller fa fallback al form).

        Sempre envelope ResponseObject: error -> form con fail/message; redirect
        -> mode=redirect con next_page VERBATIM (incluso "self": il client lo
        interpreta come reload; nessuna traduzione lato server)."""
        err_resp = self._camunda_error_response(process_vars, model=model)
        if err_resp is not None:
            return err_resp

        last_task = str(process_vars.get("last_task") or "").strip()
        ctx = process_vars.get(last_task) if last_task else None
        if not isinstance(ctx, dict):
            return None
        if ctx.get("next_action") == "redirect":
            ctx_model = str(ctx.get("model") or model or "").strip()
            model_obj = self._get_model(ctx_model) if ctx_model else None
            next_page = str(ctx.get("next_page") or "self") or "self"
            return make_response_object(
                model_obj, mode="redirect", next_action_url=next_page
            )

        return None

    async def complete_many_camunda_gateway_tasks(
        self,
        payload: dict[str, Any] | None = None,
        *,
        decision: str = "",
    ) -> ResponseObject:
        """Batch: per ogni rec_name nella lista esegue il task singolo
        (complete/approved/refused). Il process_id di ciascuno e' letto dal
        record (campo `process_id`, salvato allo start). Non si ferma al primo
        errore. Risposta: ResponseObject mode=list, content.data = lista di
        {rec_name, status, message}, total_count = totale; fail=True se almeno
        un record e' fallito."""
        payload = payload or {}
        model = str(payload.get("model") or "").strip()
        rec_names = payload.get("rec_names") or payload.get("rec_name") or []
        if isinstance(rec_names, str):
            rec_names = [rec_names]
        # variabili/form comuni a tutti (senza le chiavi di batch).
        common = {
            k: v
            for k, v in payload.items()
            if k not in ("rec_names", "rec_name")
        }
        results: list[dict[str, Any]] = []
        for rec_name in rec_names:
            rec_name = str(rec_name or "").strip()
            if not rec_name or not model:
                results.append(
                    {
                        "rec_name": rec_name,
                        "status": "error",
                        "message": "missing model/rec_name",
                    }
                )
                continue
            try:
                record = await self._get_model(model).by_name(rec_name)
                process_id = record.process_id
                if not process_id:
                    raise ValueError(
                        f"record '{rec_name}' senza process_id"
                    )
                await self.complete_camunda_gateway_task(
                    process_id, record.get_dict(), decision=decision
                )
                results.append({"rec_name": rec_name, "status": "ok"})
            except Exception as exc:  # noqa: BLE001 - un record non blocca il batch
                logger.exception(
                    "complete_many: errore rec_name=%s", rec_name
                )
                results.append(
                    {
                        "rec_name": rec_name,
                        "status": "error",
                        "message": str(exc),
                    }
                )
        ok = sum(1 for r in results if r["status"] == "ok")
        failed = len(results) - ok
        status = "ok" if failed == 0 else "partial"
        return make_response_object(
            None,
            mode="list",
            data=results,
            total_count=len(results),
            process_status=status,
            fail=failed > 0,
            message=(
                "" if failed == 0 else f"{failed}/{len(results)} falliti"
            ),
        )

    async def compo_by_name(self, model: str, name: str) -> ResponseObject:
        logger.info("service.compo_by_name model=%s name=%s", model, name)
        compo_model = self.env.get(model)
        if name == "component":
            record = await compo_model.new({"rec_name": "", "app_code": ""})
        else:
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
        model_name = envelope.content.model
        model = self.env.get(model_name)
        baseline_obfuscate_fields = envelope.content.obfucated_fields or []
        record_rulse = await self._get_record_rulse(model_name)
        logger.info(
            "acl.stream_record model=%s baseline_obfuscate_fields=%s record_rulse_count=%s",
            model_name,
            baseline_obfuscate_fields,
            len(record_rulse),
        )
        if not record_rulse:
            return model.stream_find(
                domain=envelope.content.query,
                sort=normalized_order,
                skip=skip,
                limit=limit,
                pipeline_items=pipeline_items,
                obfuscate_fields=baseline_obfuscate_fields,
                fields=envelope.content.fields,
                batch_size=envelope.content.batch_size,
            )
        # record_rulse presente: niente oscuramento server-side (altrimenti
        # il valore reale non e' piu' recuperabile per l'eventuale
        # override), si applica tutto in Python riga per riga.
        cursor = model.stream_find(
            domain=envelope.content.query,
            sort=normalized_order,
            skip=skip,
            limit=limit,
            pipeline_items=pipeline_items,
            obfuscate_fields=[],
            fields=envelope.content.fields,
            batch_size=envelope.content.batch_size,
        )
        return self._apply_record_rulse_to_stream(
            cursor, record_rulse, baseline_obfuscate_fields
        )

    async def _apply_record_rulse_to_stream(
        self,
        cursor: Any,
        record_rulse: list[dict[str, Any]],
        baseline_obfuscate_fields: list[str],
    ):
        async for item in cursor:
            original_dict = _record_to_dict(item)
            obfuscated_dict = _record_to_dict(item)
            obfuscate_fields_in_place(obfuscated_dict, baseline_obfuscate_fields)
            final_fields = apply_record_rule_override(
                original=original_dict,
                obfuscated=obfuscated_dict,
                baseline_obfuscated_fields=baseline_obfuscate_fields,
                record_rulse=record_rulse,
                resolve_var=self._resolve_query_json_logic_vars,
            )
            logger.info(
                "acl.stream_record rec_name=%s baseline=%s final=%s "
                "sample_field_value=%s",
                original_dict.get("rec_name"),
                baseline_obfuscate_fields,
                final_fields,
                {
                    f: obfuscated_dict.get(f)
                    for f in baseline_obfuscate_fields
                },
            )
            yield obfuscated_dict

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
        record_model = self._get_model(model_name)
        domain = record_model.get_domain(query)
        record_rulse = await self._get_record_rulse(model_name)
        is_admin = bool(getattr(self.session, "is_admin", False))
        is_sys_model = await self._is_sys_model(model_name)
        if record_rulse and not is_admin and not is_sys_model:
            read_domain = record_rule_read_domain(
                record_rulse, resolve_var=self._resolve_query_json_logic_vars
            )
            domain = _merge_query(domain, read_domain)
            logger.info(
                "acl.list_records model=%s record_rulse read-scope domain narrowed=%s",
                model_name,
                domain,
            )
        total_count = await record_model.count(domain=domain)
        acl = await self._get_compiled_field_acl()
        denied_read_fields, obfuscate_read_fields = acl.read_masks(
            model_key=model_name,
            app_key=str(getattr(self.session, "app_code", "")),
        )
        read_mask_fields = sorted(
            set(denied_read_fields + obfuscate_read_fields)
        )
        logger.info(
            "acl.list_records model=%s denied=%s obfuscate=%s read_mask_fields=%s",
            model_name,
            denied_read_fields,
            obfuscate_read_fields,
            read_mask_fields,
        )
        logger.info(
            "service.list_records total_count=%s domain=%s",
            total_count,
            domain,
        )
        logger.info(
            "acl.list_records model=%s record_rulse_count=%s stream=%s",
            model_name,
            len(record_rulse),
            resp_stream,
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
            response.content.obfucated_fields = sorted(
                set(obfuscate_fields or []) | set(read_mask_fields)
            )
            return response
        # Se il model ha record_rulse, l'oscuramento server-side ACL (via
        # aggregate obfuscate_fields, ozonenv/core/OzonOrm.py:2139) va
        # saltato per i campi read_mask_fields: una regola record puo'
        # dover "svelare" un campo oscurato dalla baseline (es. e' un mio
        # record), ma il valore reale non e' piu' recuperabile se e' gia'
        # stato annullato in query — l'oscuramento ACL resta comunque
        # garantito subito dopo da acl.apply_read + apply_record_rule_
        # override, lato Python. Un `obfuscate_fields` esplicito passato
        # dal chiamante (non ACL, non soggetto a override) resta invece
        # sempre server-side.
        find_obfuscate_fields = sorted(
            set(obfuscate_fields or [])
            | (set() if record_rulse else set(read_mask_fields))
        )
        logger.info(
            "acl.list_records model=%s find_obfuscate_fields=%s (caller=%s read_mask=%s skipped_for_record_rulse=%s)",
            model_name,
            find_obfuscate_fields,
            obfuscate_fields or [],
            read_mask_fields,
            bool(record_rulse),
        )
        data = await record_model.find(
            domain=domain,
            sort=normalized_order,
            skip=skip,
            limit=limit,
            pipeline_items=pipeline_items,
            obfuscate_fields=find_obfuscate_fields,
            fields=fields,
        )
        original_items = [
            _record_to_dict(item) for item in data
        ] if isinstance(data, list) else []
        data, applied_obfuscate_fields = acl.apply_read(
            model_key=model_name,
            app_key=str(getattr(self.session, "app_code", "")),
            data=data,
        )
        logger.info(
            "acl.list_records model=%s apply_read obfuscated=%s rows=%s",
            model_name,
            applied_obfuscate_fields,
            len(data) if isinstance(data, list) else 0,
        )
        if record_rulse and isinstance(data, list):
            data = [_record_to_dict(item) for item in data]
            per_item_fields: set[str] = set()
            for original_dict, obfuscated_dict in zip(original_items, data):
                item_fields = apply_record_rule_override(
                    original=original_dict,
                    obfuscated=obfuscated_dict,
                    baseline_obfuscated_fields=applied_obfuscate_fields,
                    record_rulse=record_rulse,
                    resolve_var=self._resolve_query_json_logic_vars,
                )
                logger.debug(
                    "acl.record_rulse model=%s rec_name=%s baseline=%s final=%s",
                    model_name,
                    original_dict.get("rec_name"),
                    applied_obfuscate_fields,
                    item_fields,
                )
                per_item_fields |= set(item_fields)
            applied_obfuscate_fields = sorted(per_item_fields)
            logger.info(
                "acl.list_records model=%s record_rulse applied, union_obfuscated=%s",
                model_name,
                applied_obfuscate_fields,
            )
        list_hook = await self.webhooks.emit(
            "data.after_list",
            context=self._webhook_context(model_name=model_name),
            payload={"records": data if isinstance(data, list) else []},
        )
        if isinstance(list_hook.data, list):
            data = list_hook.data
        elif isinstance(list_hook.payload, dict) and isinstance(
            list_hook.payload.get("records"), list
        ):
            data = list_hook.payload["records"]
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
        sync_component_runtime: bool = False,
        generate_component_defaults: bool = False,
    ) -> Union[None, ResponseObject]:
        logger.info(
            "service.upsert model=%s rec_name=%s",
            model_name,
            rec_name,
        )
        record_model = self._get_model(model_name)
        if model_name == "component":
            data = _normalize_payload_values(data, record_model)
            normalize_component_properties(data)
        operation = await self._resolve_write_operation(
            record_model, rec_name, data
        )
        write_hook = await self.webhooks.emit(
            "data.before_write",
            context=self._webhook_context(
                model_name=model_name,
                rec_name=rec_name,
                operation=operation,
            ),
            payload=data if isinstance(data, dict) else {},
        )
        if isinstance(write_hook.payload, dict):
            data = write_hook.payload
        acl = await self._get_compiled_field_acl()
        await enforce_write_acl(
            acl,
            self.env,
            session=self.session,
            model_key=model_name,
            operation=operation,
            payload=data if isinstance(data, dict) else {},
        )
        create_menu_dashboard = (
            model_name == "component"
            and _payload_requests_menu_dashboard(data)
        )
        if isinstance(data, dict):
            data.pop("create_menu_dashboard", None)
        record = await record_model.upsert(
            data=data,
            rec_name=rec_name,
            data_value=data_value,
            trnf_config=trnf_config,
            fields_parser=fields_parser,
        )
        if (
            model_name == "component"
            and record is not None
            and sync_component_runtime
        ):
            await self._sync_component_runtime(
                _record_to_dict(record),
                generate_defaults=(
                    create_menu_dashboard
                    or (
                        generate_component_defaults
                        and operation == FieldAclOperation.INSERT.value
                    )
                ),
            )
        elif model_name == "component" and record is not None:
            await self._sync_component_rules(_record_to_dict(record))
            if create_menu_dashboard:
                await self._create_menu_dashboard_for_component(
                    _record_to_dict(record)
                )
        if record is not None:
            # Auto-enqueue mail se il component del model lo richiede
            # (component.properties.send_mail_create/update). Best-effort.
            await maybe_enqueue_on_save(
                self,
                model_name,
                getattr(record, "rec_name", "") or rec_name,
                operation,
            )
            await self.webhooks.emit(
                "data.after_write",
                context=self._webhook_context(
                    model_name=model_name,
                    rec_name=getattr(record, "rec_name", "") or rec_name,
                    operation=operation,
                ),
                payload=_record_to_dict(record),
            )
        return make_response_object(record_model, mode="form", data=record)

    async def _sync_component_rules(self, schema: dict[str, Any]) -> None:
        if not isinstance(schema, dict) or not schema:
            return
        model_name = str(schema.get("rec_name", "") or "").strip()
        try:
            from app.ozon_env_acl.model_rules_sync import sync_model_rules

            await sync_model_rules(self.env, schema)
            logger.info(
                "component hook: model rule sync ok rec_name=%s",
                model_name,
            )
        except Exception:
            logger.exception(
                "component hook: model rule sync failed rec_name=%s",
                model_name,
            )

    async def _sync_component_runtime(
        self,
        schema: dict[str, Any],
        generate_defaults: bool = False,
    ) -> None:
        if not isinstance(schema, dict) or not schema:
            return
        model_name = str(schema.get("rec_name", "") or "").strip()
        if not _is_runtime_component_name(model_name):
            logger.info(
                "component hook: skip runtime sync invalid rec_name=%s",
                model_name,
            )
            return
        try:
            await self.env.insert_update_component(schema)
            logger.info(
                "component hook: insert_update_component ok rec_name=%s",
                model_name,
            )
        except Exception:
            logger.exception(
                "component hook: insert_update_component failed rec_name=%s",
                model_name,
            )
        if generate_defaults:
            await self._create_menu_dashboard_for_component(schema)

    async def _create_menu_dashboard_for_component(
        self,
        schema: dict[str, Any],
    ) -> None:
        if not isinstance(schema, dict) or not schema:
            return
        model_name = str(schema.get("rec_name", "") or "").strip()
        if not _is_runtime_component_name(model_name):
            logger.info(
                "component hook: skip menu dashboard invalid rec_name=%s",
                model_name,
            )
            return
        if str(schema.get("data_model", "") or "").strip().lower() == "no_model":
            logger.info(
                "component hook: skip menu dashboard for no_model component=%s",
                model_name,
            )
            return
        try:
            await self._make_default_actions_for_component(schema)
        except Exception:
            logger.exception(
                "component hook: make_default_actions failed rec_name=%s",
                model_name,
            )

    async def fast_search_list(
        self,
        action_name: str,
        query_fields: list[dict[str, Any]],
        skip: int = 0,
        limit: int = 100,
        order: str = "",
    ) -> ResponseObject:
        action = await self.action_runtime.get_action_record(action_name)
        if not action:
            raise HTTPException(
                status_code=404,
                detail=f"Action '{action_name}' not found",
            )
        action_model = action.model
        action_mode = action.mode
        if action_mode != "list":
            raise HTTPException(
                status_code=422,
                detail="fast_search only valid on list actions",
            )
        resolved_query, resolved_order = (
            self.action_runtime._resolve_list_defaults(action)
        )
        effective_order = order.strip() or resolved_order
        if query_fields:
            merged = _merge_query(resolved_query, {"$and": query_fields})
        else:
            merged = resolved_query
        return await self.list_records(
            model_name=action_model,
            query=merged,
            order=effective_order,
            skip=skip,
            limit=limit,
            resp_stream=True,
            batch_size=10,
        )

    async def load(
        self, model_name: str, domain: dict, in_execution=False
    ) -> Union[None, ResponseObject]:
        logger.info("service.load model=%s domain=%s", model_name, domain)
        record_model = self._get_model(model_name)
        record = await record_model.load(domain)
        return make_response_object(record_model, mode="form", data=record)

    async def load_record(
        self, model: str, rec_name: str
    ) -> Union[None, ResponseObject]:
        logger.info(
            "service.load_record model=%s rec_name=%s", model, rec_name
        )
        record_model = self._get_model(model)
        raw_record = await record_model.by_name(rec_name)
        original_dict = _record_to_dict(raw_record)
        record_rulse = await self._get_record_rulse(model)
        is_admin = bool(getattr(self.session, "is_admin", False))
        is_sys_model = await self._is_sys_model(model)
        record_access = record_rule_access(
            record_rulse=record_rulse,
            record=original_dict,
            resolve_var=self._resolve_query_json_logic_vars,
            is_admin=is_admin or is_sys_model,
        )
        logger.info(
            "acl.load_record model=%s rec_name=%s is_admin=%s is_sys_model=%s record_rulse_count=%s access=%s",
            model,
            rec_name,
            is_admin,
            is_sys_model,
            len(record_rulse),
            record_access,
        )
        if not record_access["read"]:
            raise HTTPException(
                status_code=404,
                detail=f"Record '{rec_name}' not found",
            )
        acl = await self._get_compiled_field_acl()
        record, obfuscate_fields = acl.apply_read(
            model_key=model,
            app_key=str(getattr(self.session, "app_code", "")),
            data=raw_record,
        )
        logger.info(
            "acl.load_record model=%s rec_name=%s baseline_obfuscated=%s",
            model,
            rec_name,
            obfuscate_fields,
        )
        if record_rulse:
            obfuscated_dict = _record_to_dict(record)
            obfuscate_fields = apply_record_rule_override(
                original=original_dict,
                obfuscated=obfuscated_dict,
                baseline_obfuscated_fields=obfuscate_fields,
                record_rulse=record_rulse,
                resolve_var=self._resolve_query_json_logic_vars,
            )
            record = obfuscated_dict
            logger.info(
                "acl.load_record model=%s rec_name=%s after_record_rulse=%s",
                model,
                rec_name,
                obfuscate_fields,
            )
        read_hook = await self.webhooks.emit(
            "data.after_read",
            context=self._webhook_context(model_name=model, rec_name=rec_name),
            payload=_record_to_dict(record),
        )
        if isinstance(read_hook.payload, dict):
            record = read_hook.payload
        response = make_response_object(
            record_model,
            mode="form",
            data=record,
            readable=record_access["read"],
            editable=record_access["update"],
        )
        response.content.obfucated_fields = obfuscate_fields
        return response

    def _webhook_context(
        self,
        *,
        model_name: str = "",
        rec_name: str = "",
        operation: str = "",
    ) -> dict[str, Any]:
        return {
            "app_code": str(getattr(self.session, "app_code", "") or ""),
            "uid": str(getattr(self.session, "uid", "") or ""),
            "model": model_name,
            "rec_name": rec_name,
            "operation": operation,
        }

    async def _emit_calendar_task_event(
        self,
        rec_name: str,
        result: dict[str, Any],
    ) -> None:
        """Emette l'esito della run di un calendar task come webhook outbound.

        Notifica post-facto: NON deve mai rompere la run, quindi è fail-safe
        (swallow di ogni errore, indipendente dal fail_mode configurato).
        Evento `calendar.task.completed` se ok, altrimenti
        `calendar.task.failed`.
        """
        status_value = str(result.get("status", "") or "")
        event = (
            "calendar.task.completed"
            if status_value == "ok"
            else "calendar.task.failed"
        )
        try:
            await self.webhooks.emit(
                event,
                context=self._webhook_context(
                    model_name="calendar", rec_name=rec_name
                ),
                payload={
                    "rec_name": rec_name,
                    "task": result.get("task", ""),
                    "task_record_name": result.get("task_record_name", ""),
                    "status": status_value,
                    "run_id": result.get("run_id", ""),
                    "started_at": result.get("started_at", ""),
                    "finished_at": result.get("finished_at", ""),
                    "message": result.get("message", ""),
                },
            )
        except Exception:
            logger.exception(
                "calendar webhook emit failed rec_name=%s event=%s",
                rec_name,
                event,
            )

    async def run_calendar_task(
        self,
        rec_name: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_payload = payload.copy() if isinstance(payload, dict) else {}
        calendar_model = self._get_model("calendar")
        record = await calendar_model.by_name(rec_name)
        if not record:
            raise HTTPException(
                status_code=404,
                detail=f"Calendar task '{rec_name}' not found",
            )

        task_data = _record_to_dict(record)
        if record.tipo != "task":
            return {
                "status": "error",
                "rec_name": rec_name,
                "message": "Calendar record is not a task",
            }
        if record.deleted  != 0:
            return {
                "status": "error",
                "rec_name": rec_name,
                "message": "Calendar task is deleted",
            }

        trigger = str(run_payload.get("trigger", "manual") or "manual")
        if (
            trigger != "manual"
            and not _is_enabled_flag(task_data.get("periodico", False))
            and str(task_data.get("stato", "")).lower() == "done"
        ):
            return {
                "status": "skipped",
                "rec_name": rec_name,
                "message": "One-shot calendar task already completed",
            }

        action_name = record.task or ""
        if not action_name:
            await self._write_calendar_fields(
                calendar_model,
                rec_name,
                task_data,
                {"stato": "erroreConfigurazione"},
            )
            return {
                "status": "error",
                "rec_name": rec_name,
                "message": "Calendar task action is empty",
            }

        if action_name == "clean_expired_deleted":
            logger.info("Clean all record")
            started_at = datetime.now(ZoneInfo("Europe/Rome"))
            try:
                deleted_count = await self.clean_expired_deleted_records()
                logger.info(f"Clean {deleted_count} records")
                finished_at = datetime.now(ZoneInfo("Europe/Rome"))
                next_state = (
                    "progress" if record.periodico else "done"
                )
                update_data = {
                    "stato": next_state,
                    "last": finished_at,
                    "message": f"Successfully deleted {deleted_count} expired records",
                }
                if next_state == "done":
                    update_data["active"] = False
                await self._write_calendar_fields(
                    calendar_model, rec_name, task_data, update_data
                )
                result = {
                    "status": "ok",
                    "rec_name": rec_name,
                    "task": action_name,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "message": f"Successfully deleted {deleted_count} expired records",
                }
                await self._emit_calendar_task_event(rec_name, result)
                return result
            except Exception as exc:
                finished_at = datetime.now(ZoneInfo("Europe/Rome"))
                await self._write_calendar_fields(
                    calendar_model,
                    rec_name,
                    task_data,
                    {
                        "stato": "errore",
                        "last": finished_at,
                        "message": str(exc),
                    },
                )
                result = {
                    "status": "error",
                    "rec_name": rec_name,
                    "task": action_name,
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "message": str(exc),
                }
                await self._emit_calendar_task_event(rec_name, result)
                return result

        target_rec_name = record.task_record_name
        action_payload = {
            "rec_name": target_rec_name,
            "calendar_task": rec_name,
            "run_id": run_payload.get("run_id", ""),
            "scheduled_time": run_payload.get("scheduled_time", ""),
            "trigger": trigger,
        }

        started_at = datetime.now(ZoneInfo("Europe/Rome"))
        try:
            result = await self.service_handle_action_post(
                action_name=action_name,
                data=action_payload,
                rec_name=target_rec_name,
            )
        except Exception as exc:
            finished_at = datetime.now(ZoneInfo("Europe/Rome"))
            await self._write_calendar_fields(
                calendar_model,
                rec_name,
                task_data,
                {
                    "stato": "errore",
                    "last": finished_at,
                    "message": str(exc),
                },
            )
            result = {
                "status": "error",
                "rec_name": rec_name,
                "task": action_name,
                "task_record_name": target_rec_name,
                "run_id": run_payload.get("run_id", ""),
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "message": str(exc),
            }
            await self._emit_calendar_task_event(rec_name, result)
            return result

        finished_at = datetime.now(ZoneInfo("Europe/Rome"))
        result_data = result.data if result is not None else {}
        failed = (
            isinstance(result_data, dict)
            and str(result_data.get("status", "")).lower() == "error"
        )
        next_state = (
            "errore"
            if failed
            else (
                "progress"
                if _is_enabled_flag(task_data.get("periodico", False))
                else "done"
            )
        )
        update_data: dict[str, Any] = {
            "stato": next_state,
            "last": finished_at,
        }
        if next_state == "done":
            update_data["active"] = False
        await self._write_calendar_fields(
            calendar_model, rec_name, task_data, update_data
        )

        result_out = {
            "status": "error" if failed else "ok",
            "rec_name": rec_name,
            "task": action_name,
            "task_record_name": target_rec_name,
            "run_id": run_payload.get("run_id", ""),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "message": (
                str(result_data.get("message", ""))
                if isinstance(result_data, dict)
                else ""
            ),
        }
        await self._emit_calendar_task_event(rec_name, result_out)
        return result_out

    async def clean_expired_deleted_records(self) -> int:
        import time
        # logger.info("clean_expired_deleted_records")
        current_time = int(time.time())
        collections = self.env.models
        logger.info(f"clean_expired_deleted_records {len(collections)}")
        total_deleted = 0
        for model_name in collections:
            try:
                model_obj = self.env.get(model_name)
                if model_obj and not model_obj.virtual:
                    domain = {"deleted": {"$gt": 0, "$lte": current_time}}
                    logger.info(f"Try Remove model {model_name} @ {domain}")
                    count = await model_obj.remove_all(domain)
                    total_deleted += count or 0
            except Exception as exc:
                logger.exception(
                    "failed to clean expired deleted for model %s: %s",
                    model_name,
                    exc,
                )
        return total_deleted

    async def _write_calendar_fields(
        self,
        calendar_model: Any,
        rec_name: str,
        base: dict[str, Any],
        changes: dict[str, Any],
    ) -> None:
        # L'ORM ozon-env diffa il record completo: un upsert parziale
        # cancellerebbe i campi non passati (get_dict NON esclude i default).
        # Scriviamo il record intero (base appena letto + changes) per un $set
        # minimale. Vedi memoria ozon-env-upsert-partial-wipe.
        full = dict(base)
        full.update(changes)
        await calendar_model.upsert(data=full, rec_name=rec_name)

    async def _make_default_actions_for_component(
        self,
        schema: dict[str, Any],
    ) -> None:
        model_name = str(schema.get("rec_name", "") or "").strip()
        if not model_name:
            return

        comp_type = str(schema.get("type", "") or "").strip()
        comp_title = str(schema.get("title", "") or model_name).strip()
        comp_sys = bool(schema.get("sys", False))

        action_model = self.env.get("action")
        menu_group_model = self.env.get("menu_group")

        template_actions = await action_model.find(
            domain={
                "$and": [
                    {"model": "action"},
                    {"sys": True},
                    {"deleted": 0},
                    {"list_query": "{}"},
                ]
            },
            sort="list_order:asc,rec_name:desc",
            limit=0,
        )
        logger.info(
            "component hook: found %s template actions for model=%s",
            (
                len(template_actions)
                if isinstance(template_actions, list)
                else "?"
            ),
            model_name,
        )

        if comp_type != "resource":
            existing_count = await menu_group_model.count(
                domain={"rec_name": model_name, "deleted": 0}
            )
            if not existing_count:
                group_data: dict[str, Any] = {
                    "rec_name": model_name,
                    "label": comp_title,
                    "admin": comp_sys,
                    "active": True,
                    "deleted": 0,
                }
                if not comp_sys:
                    app_code = str(
                        getattr(self.session, "app_code", "") or ""
                    ).strip()
                    if app_code:
                        group_data["apps"] = [app_code]
                await menu_group_model.upsert(
                    data=group_data, rec_name=model_name
                )
                logger.info(
                    "component hook: menu_group created model=%s", model_name
                )

        for template in (
            template_actions if isinstance(template_actions, list) else []
        ):
            data = template.get_dict()
            data.pop("_id", None)
            data.pop("id", None)

            action_rec_name = str(data.get("rec_name", "") or "").replace(
                "_action", f"_{model_name}"
            )
            next_action = str(data.get("next_action_name", "") or "").replace(
                "_action", f"_{model_name}"
            )

            data.update(
                {
                    "rec_name": action_rec_name,
                    "next_action_name": next_action,
                    "model": model_name,
                    "sys": comp_sys,
                    "admin": comp_sys,
                    "active": True,
                    "deleted": 0,
                    "action_root_path": "/action",
                }
            )
            if not comp_sys:
                data["user_function"] = "user"
                data["groups"] = ["operator"]
            if data.get("component_type"):
                data["component_type"] = comp_type
            if data.get("action_type") == "menu":
                data["title"] = comp_title
                data_value = dict(data.get("data_value") or {})
                data_value.update(
                    {
                        "title": comp_title,
                        "model": comp_title,
                        "data_model": model_name,
                        "rec_name": action_rec_name,
                    }
                )
                if comp_type == "resource":
                    data["menu_group"] = "risorse_app"
                    data_value["menu_group"] = "Risorse Apps"
                else:
                    data["menu_group"] = model_name
                    data_value["menu_group"] = comp_title
                data["data_value"] = data_value

            await action_model.upsert(data=data, rec_name=action_rec_name)
            logger.info(
                "component hook: action upserted model=%s action=%s",
                model_name,
                action_rec_name,
            )

    async def _resolve_write_operation(
        self,
        record_model: OzonModelBase,
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

    async def _get_record_rulse(
        self, model_key: str
    ) -> list[dict[str, Any]]:
        """Righe `model_fields_rule` (rule_type="record") per `model_key`,
        scoped per app_code corrente (cache per-request): regole
        data-dependent, valutate per record da `apply_record_rule_override`
        — non compilabili in `CompiledFieldAcl` (che e' actor-only, niente
        contesto riga).

        Fonte di verita': la collection `model_fields_rule` (popolata al
        salva del component da `app.ozon_env_acl.model_rules_sync`), NON
        `component.properties` — quest'ultima puo' non persistere la
        config (es. `user` e' un identity model escluso dai default, ma un
        admin puo' comunque aver configurato la regola via builder in
        passato: il sync l'ha scritta in `model_fields_rule` a prescindere)."""
        if model_key in self._record_rulse_cache:
            logger.info(
                "acl.record_rulse model=%s cache_hit count=%s",
                model_key,
                len(self._record_rulse_cache[model_key]),
            )
            return self._record_rulse_cache[model_key]
        record_rulse: list[dict[str, Any]] = []
        app_code = str(getattr(self.session, "app_code", "") or "")
        try:
            rule_model = self.env.get("model_fields_rule")
            if rule_model is not None:
                domain = {
                    "$and": [
                        {"app_code": app_code},
                        {"model": model_key},
                        {"rule_type": "record"},
                        {"active": True},
                        {"deleted": 0},
                    ]
                }
                rows = await rule_model.find(domain=domain, limit=0)
                for row in rows or []:
                    data = _record_to_dict(row)
                    record_rulse.append(
                        {
                            "filters": _safe_json_dict(data.get("filters")),
                            "restricted_fields": data.get("restricted_fields")
                            or [],
                            "read": bool(data.get("read", False)),
                            "create": bool(data.get("create", False)),
                            "update": bool(data.get("update", False)),
                            "delete": bool(data.get("delete", False)),
                        }
                    )
        except Exception:
            logger.warning(
                "record_rulse lookup failed model=%s", model_key
            )
            record_rulse = []
        logger.info(
            "acl.record_rulse model=%s app_code=%s rows_found=%s rules=%s",
            model_key,
            app_code,
            len(record_rulse),
            record_rulse,
        )
        self._record_rulse_cache[model_key] = record_rulse
        return record_rulse

    async def _is_sys_model(self, model_key: str) -> bool:
        """True se il component che definisce `model_key` ha `sys=True`
        (config applicativa condivisa: action, menu_group, settings, user,
        ecc. — vedi IDENTITY_MODEL_NAMES/_DEFAULT_MODELS_GROUPS_SYS in
        app.core.OzonEnvApp). Questi NON sono "documenti" di un singolo
        utente: l'ownership per-record non ha senso li', sono gia' regolati
        per gruppo da models_groups. L'enforcement hide/readonly di
        record_rulse (record_rule_access) si applica solo ai model non-sys
        (dati applicativi/plugin: modulo_dati_persona, ext_service, ecc.) —
        altrimenti la regola owner_uid iniettata di default da
        normalize_component_properties su OGNI component nasconderebbe
        config condivisa a chiunque non ne sia il creatore.

        Fail-open a True (sys, quindi enforcement SALTATO) se il lookup
        fallisce o il model non ha un component registrato: qui il rischio
        di rottura (bloccare per errore config condivisa) e' peggiore del
        rischio di non restringere un record realmente non-sys."""
        if model_key in self._sys_model_cache:
            return self._sys_model_cache[model_key]
        is_sys = True
        try:
            component_model = self.env.get("component")
            if component_model is not None:
                component = await component_model.by_name(model_key)
                if component is not None:
                    is_sys = bool(component.sys)
        except Exception:
            logger.warning("_is_sys_model lookup failed model=%s", model_key)
        self._sys_model_cache[model_key] = is_sys
        return is_sys

    async def _get_compiled_field_acl(self) -> CompiledFieldAcl:
        cached = getattr(self.session, "compiled_field_acl", None)
        if isinstance(cached, CompiledFieldAcl):
            logger.info(
                "acl.compile cache_hit (session) policies=%s",
                len(cached.policies),
            )
            return cached
        if self._compiled_field_acl is not None:
            logger.info(
                "acl.compile cache_hit (service) policies=%s",
                len(self._compiled_field_acl.policies),
            )
            return self._compiled_field_acl

        policies = await self._load_field_acl_policies()
        session_user = getattr(self.session, "user", None)
        session_groups = (
            session_user.get("groups") if isinstance(session_user, dict) else None
        )
        logger.info(
            "acl.compile app_code=%s uid=%s groups=%s is_admin=%s total_raw_policies=%s",
            str(getattr(self.session, "app_code", "")),
            str(getattr(self.session, "uid", "")),
            session_groups,
            bool(getattr(self.session, "is_admin", False)),
            len(policies),
        )
        compiled = compile_field_acl_policies(policies, session=self.session)
        logger.info(
            "acl.compile compiled_policies=%s",
            len(compiled.policies),
        )
        self._compiled_field_acl = compiled
        try:
            object.__setattr__(self.session, "compiled_field_acl", compiled)
        except Exception:
            pass
        return compiled

    async def _load_field_acl_policies(self) -> list[Any]:
        policies: list[Any] = []
        for model_name in (
            "field_acl_policy",
            "fieldaclpolicy",
            "FieldAclPolicy",
        ):
            try:
                model = self.env.get(model_name)
            except Exception:
                continue
            if model is None:
                continue
            try:
                domain = model.get_domain({"active": True, "deleted": 0})
            except Exception:
                domain = {"active": True, "deleted": 0}
            try:
                policies = await model.find(
                    domain=domain,
                    sort="priority:asc,list_order:asc,rec_name:asc",
                    limit=0,
                )
            except TypeError:
                policies = await model.find(domain=domain, limit=0)
            except Exception:
                logger.exception("field ACL policy loading failed")
                policies = []
            break
        static_count = len(policies or [])
        component_property_policies = (
            await self._load_component_property_acl_policies()
        )
        model_fields_rule_policies = await self._load_model_fields_rule_policies()
        logger.info(
            "acl.load_policies static=%s component_properties=%s model_fields_rule=%s",
            static_count,
            len(component_property_policies),
            len(model_fields_rule_policies),
        )
        return (
            list(policies or [])
            + component_property_policies
            + model_fields_rule_policies
        )

    async def _load_component_property_acl_policies(self) -> list[dict[str, Any]]:
        """FieldAclPolicy sintetiche da component.properties (vedi app.ozon_env_acl)."""
        try:
            component_model = self.env.get("component")
        except Exception:
            return []
        if component_model is None:
            return []
        try:
            domain = component_model.get_domain({"active": True, "deleted": 0})
        except Exception:
            domain = {"active": True, "deleted": 0}
        try:
            components = await component_model.find(domain=domain, limit=0)
        except Exception:
            logger.exception("component ACL properties loading failed")
            return []
        return synth_policies_from_component_properties(components)

    async def _load_model_fields_rule_policies(self) -> list[dict[str, Any]]:
        """FieldAclPolicy OBFUSCATE sintetiche da `model_fields_rule`
        (rule_type="fields"), scoped per app_code corrente — fonte di
        verita' per l'oscuramento per-gruppo (vedi `_get_record_rulse` per
        l'equivalente per-record, rule_type="record").

        Una riga = (model, group) con la lista COMPLETA dei campi
        ristretti per quel model e `read` = quel gruppo puo' leggerli in
        chiaro. Piu' righe (gruppi diversi) per lo stesso (model, campo)
        vanno unite in UNA policy con `exclude_groups` = unione di tutti i
        gruppi ammessi: `read_masks` oscura se ALMENO UNA policy che
        matcha lo dice, quindi una policy per gruppo separata negherebbe
        un attore che sta nel gruppo A ma non nel gruppo B, anche se A da
        solo basterebbe.

        Niente bypass admin (a differenza di `models_groups`/legacy
        `models_restricted_fields`, che escludono admin via
        `is_admin: False`): un campo GDPR-style non deve diventare visibile
        solo perche' l'attore e' admin — solo gruppo o record_rulse
        sbloccano il campo, admin incluso (comportamento confermato
        dall'utente dopo che l'admin vedeva il campo in chiaro su tutta la
        lista).
        """
        try:
            rule_model = self.env.get("model_fields_rule")
        except Exception:
            return []
        if rule_model is None:
            return []
        app_code = str(getattr(self.session, "app_code", "") or "")
        domain = {
            "$and": [
                {"app_code": app_code},
                {"rule_type": "fields"},
                {"active": True},
                {"deleted": 0},
            ]
        }
        try:
            rows = await rule_model.find(domain=domain, limit=0)
        except Exception:
            logger.exception("model_fields_rule policy loading failed")
            return []
        logger.info(
            "acl.model_fields_rule_policies app_code=%s fields_rows_found=%s",
            app_code,
            len(rows or []),
        )
        groups_by_field: dict[tuple[str, str], set[str]] = {}
        for row in rows or []:
            data = _record_to_dict(row)
            if not data.get("read"):
                continue
            model_key = str(data.get("model") or "").strip()
            group = str(data.get("group") or "").strip()
            if not model_key or not group:
                continue
            for field_path in data.get("restricted_fields") or []:
                field_path = str(field_path).strip()
                if not field_path:
                    continue
                groups_by_field.setdefault((model_key, field_path), set()).add(
                    group
                )
        policies = [
            {
                "model_key": model_key,
                "field_path": field_path,
                "operation": FieldAclOperation.READ.value,
                "effect": FieldAclEffect.OBFUSCATE.value,
                "actor_selector": {"exclude_groups": sorted(groups)},
                "priority": 10,
            }
            for (model_key, field_path), groups in groups_by_field.items()
        ]
        logger.info(
            "acl.model_fields_rule_policies app_code=%s policies=%s",
            app_code,
            policies,
        )
        return policies

    async def _get_action_record(self, action_name: str) -> CoreModel | None:
        return await self.action_runtime.get_action_record(action_name)

    async def _get_component_record(
        self, component_name: str
    ) -> CoreModel | None:
        if not component_name:
            return None
        try:
            component_model = self.env.get("component")
            return await component_model.by_name(component_name)
        except Exception:
            logger.debug("component not found name=%s", component_name)
            return None

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

    def _resolve_json_logic_var(self, spec: Any) -> Any:
        """
        Risolve un singolo nodo json-logic `{"var": ...}` letto da
        `action.list_query`. Namespace supportati:
        - `user.<attr>` / `session.<attr>` / `app.<attr>`: attributo (anche
          annidato) letto dalla sessione corrente (`self.session`).
        - `now`, `now-3h`, `now+3d-3h`, ...: datetime UTC aware relativo a
          adesso, vedi `_resolve_now_expr`.
        Forma json-logic completa `{"var": ["path", default]}` supportata:
        se il path non risolve, ritorna `default` (default: None).
        """
        if isinstance(spec, list):
            path = spec[0] if spec else ""
            default = spec[1] if len(spec) > 1 else None
        else:
            path = spec
            default = None
        path = str(path or "").strip()
        if not path:
            return default

        if path == "now" or path.startswith("now+") or path.startswith("now-"):
            resolved = self.date_engine.resolve_relative_expr(path)
            return resolved if resolved is not None else default

        segments = path.split(".")
        if segments[0] not in ("user", "session", "app"):
            return default

        value: Any = self.session
        for segment in segments[1:]:
            if value is None:
                break
            if isinstance(value, dict):
                value = value.get(segment)
            else:
                value = getattr(value, segment, None)
        return value if value is not None else default

    def _resolve_query_json_logic_vars(self, data: Any) -> Any:
        """
        Cammina ricorsivamente un query dict (tipicamente `action.list_query`
        gia' parsato) e sostituisce ogni nodo `{"var": ...}` col valore
        risolto da `_resolve_json_logic_var`. Non tocca nessun altro
        operatore Mongo/json-logic: passa attraverso invariati.
        """
        if isinstance(data, dict):
            if set(data.keys()) == {"var"}:
                return self._resolve_json_logic_var(data["var"])
            return {
                key: self._resolve_query_json_logic_vars(value)
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [self._resolve_query_json_logic_vars(item) for item in data]
        return data

    def _parse_query_dict(self, query_value: Any) -> dict[str, Any]:
        if isinstance(query_value, dict):
            return query_value.copy()
        parsed = check_parse_json(
            query_value if query_value is not None else "{}"
        )
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

    def _model_name(self, model: OzonModelBase) -> str:
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
        model: OzonModelBase,
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
        model: OzonModelBase,
        query: dict[str, Any],
        sort: str = "list_order:asc,rec_name:asc",
        limit: int = 0,
    ) -> list[Any]:
        return await model.find(domain=query, sort=sort, limit=limit)

    async def _count_base(
        self, model: OzonModelBase, query: dict[str, Any]
    ) -> int:
        domain = query
        if hasattr(model, "get_domain") and callable(model.get_domain):
            domain = model.get_domain(query)
        return await model.count(domain=domain)

    async def _make_menu_item(
        self, card: dict[str, Any], rec_b: CoreModel
    ) -> dict[str, Any] | bool:
        action_model_name = rec_b.model
        cc_model = self.env.get(action_model_name)

        if rec_b.mode:
            link = (
                f"{rec_b.action_root_path or '/action'}"
                f"/{rec_b.rec_name or ''}"
            )
        else:
            link = rec_b.action_root_path or "/action"

        number = 0
        if rec_b.mode == "list":
            list_query = self._resolve_query_json_logic_vars(
                self._parse_query_dict(rec_b.list_query)
            )
            q = await self._default_query(cc_model, list_query)
            number = await self._count_base(cc_model, q)

        return {
            "model": rec_b.model or "",
            "icon": rec_b.button_icon or "",
            "action_type": rec_b.action_type or "",
            "content": link,
            "label": rec_b.title or rec_b.rec_name or "",
            "mode": rec_b.mode or "",
            "number": number,
        }

    def _make_button_main_menu(self, rec: CoreModel) -> dict[str, Any]:
        btn_action_parser = {
            "save": "post",
            "copy": "post",
            "delete": "post",
            "window": False,
        }
        rec_name = rec.rec_name or ""
        action_type = rec.action_type or ""
        action_root_path = rec.action_root_path or "/action"

        if action_type in btn_action_parser:
            url_action = f"{action_root_path}/{rec_name}/{rec_name}"
        else:
            url_action = f"{action_root_path}/{rec_name}"

        return {
            "model": rec.model or "",
            "key": rec_name,
            "type": "button",
            "label": rec.title or rec_name,
            "leftIcon": rec.button_icon or "",
            "btn_action_type": btn_action_parser.get(action_type),
            "action_type": action_type,
            "url_action": url_action,
            "builder": bool(rec.builder_enabled),
        }

    async def _get_dashboard_menu_flags(
        self,
        group_names: list[str],
    ) -> tuple[bool, bool]:
        normalized_groups = [
            str(group_name).strip()
            for group_name in group_names
            if str(group_name).strip()
        ]
        if not normalized_groups:
            return False, False

        action_model = self.env.get("action")
        if len(normalized_groups) == 1:
            menu_group_filter: dict[str, Any] = {
                "menu_group": normalized_groups[0]
            }
        else:
            menu_group_filter = {"menu_group": {"$in": normalized_groups}}

        q_menu_user = await self._make_query_user(
            [
                {"action_type": "menu"},
                {"component_type": {"$in": ["form", "resource", "layout"]}},
                menu_group_filter,
            ]
        )
        q_menu = await self._default_query(action_model, {"$and": q_menu_user})
        menu_actions = await self._find_base(action_model, query=q_menu)
        if not menu_actions:
            return False, False
        return True, _has_non_system_records(menu_actions)

    async def _get_basic_menu_list(
        self, parent: str = ""
    ) -> list[dict[str, Any]]:
        menu_group_model = self.env.get("menu_group")
        action_model = self.env.get("action")

        # I menu_group admin-only appartengono al menu admin in alto
        # (generato dal layout via service_get_menu), quindi NON devono mai
        # diventare card della dashboard, nemmeno per gli utenti admin.
        group_base = {"$and": [{"admin": False}, {"parent": parent}]}
        menu_groups_query = await self._default_query(
            menu_group_model,
            group_base,
        )
        menu_groups = await self._find_base(
            menu_group_model,
            query=menu_groups_query,
        )

        menu_list: list[dict[str, Any]] = []
        model_done: set[str] = set()
        for group in menu_groups:
            if _is_admin_only_menu_group(group):
                continue
            group_name = group.rec_name or ""
            q_user = await self._make_query_user([{"menu_group": group_name}])
            action_query = await self._default_query(
                action_model, {"$and": q_user}
            )
            found_item = await self._find_base(
                action_model, query=action_query
            )

            if found_item:
                has_menu_actions, has_non_system_menu = (
                    await self._get_dashboard_menu_flags([group_name])
                )
                if has_menu_actions and not has_non_system_menu:
                    continue
                first = found_item[0]
                model_key = f"{group_name}{first.model or ''}"
                if model_key not in model_done:
                    model_done.add(model_key)
                    menu_list.append(
                        {
                            "model": first.model or "",
                            "menu_group": group_name,
                            "label": group.label or group_name,
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
                # Sotto-menu admin-only non devono comparire (ne' contare)
                # nella card folder della dashboard, per nessun utente.
                sub_menus = [
                    sub
                    for sub in sub_menus
                    if not _is_admin_only_menu_group(sub)
                ]
                if sub_menus:
                    sub_groups = [
                        sub.rec_name or "" for sub in sub_menus if sub.rec_name
                    ]
                    _, has_non_system_menu = (
                        await self._get_dashboard_menu_flags(sub_groups)
                    )
                    if has_non_system_menu:
                        menu_list.append(
                            {
                                "model": False,
                                "menu_group": group_name,
                                "label": group.label or group_name,
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
        logger.info(
            "service_get_layout: start name=%s app_code=%s uid=%s is_admin=%s",
            name,
            getattr(self.session, "app_code", None),
            getattr(self.session, "uid", None),
            getattr(self.session, "is_admin", None),
        )
        component_model = self.env.get("component")
        layout_name = name or ""
        schema = {}
        layout_query: dict[str, Any] = {}

        if layout_name:
            record = await self._get_component_record(layout_name)
            schema = record.get_dict() if record else {}
            layout_query = {"rec_name": layout_name}
            logger.info(
                "service_get_layout: explicit layout name=%s found=%s",
                layout_name,
                bool(record),
            )

        if not schema:
            layout_query = await self._default_query(
                component_model,
                {"type": "layout"},
            )
            logger.info(
                "service_get_layout: fallback query=%s",
                layout_query,
            )
            layouts = await self._find_base(
                component_model,
                query=layout_query,
                limit=1,
            )
            if layouts:
                schema = layouts[0].get_dict()
                layout_name = layouts[0].rec_name or layout_name
            logger.info(
                "service_get_layout: fallback layouts found=%d layout_name=%s",
                len(layouts) if isinstance(layouts, list) else 0,
                layout_name,
            )

        menu_data = await self.service_get_menu()
        logger.info(
            "service_get_layout: done layout_name=%s schema_keys=%d menu_items=%d",
            layout_name,
            len(schema) if isinstance(schema, dict) else 0,
            len(menu_data.data) if isinstance(menu_data.data, list) else 0,
        )
        return ResponseObjectData(
            mode="layout",
            query=layout_query,
            data={
                "layout": layout_name,
                "schema": schema,
                "menu": menu_data.data,
                "settings": {
                    "module_name": (
                        getattr(self.settings, "module_name", "")
                        or getattr(self.settings, "module_label", "")
                        or getattr(self.settings, "app_name", "")
                    ),
                    "version": (
                        getattr(self.settings, "version", "")
                        or getattr(self.settings, "app_version", "")
                    ),
                    "logo_img_url": getattr(self.settings, "logo_img_url", ""),
                },
            },
        )

    async def service_get_menu(self, parent: str = "") -> ResponseObjectData:
        is_admin = self.session.is_admin
        user_groups = set(self.session.user.get("groups", []) if (self.session and getattr(self.session, "user", None)) else [])
        logger.info(
            "service_get_menu: start parent=%s app_code=%s uid=%s is_admin=%s groups=%s",
            parent,
            getattr(self.session, "app_code", None),
            getattr(self.session, "uid", None),
            is_admin,
            user_groups,
        )
        if not is_admin and "technical_operator" not in user_groups:
            if not user_groups:
                logger.info("service_get_menu: non-admin with no groups, returning empty menu")
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
        menu_groups = [g for g in menu_groups if self._is_menu_group_allowed(g)]
        logger.info(
            "service_get_menu: menu_group_query=%s groups_found=%d",
            menu_group_query,
            len(menu_groups) if isinstance(menu_groups, list) else 0,
        )

        grouped: dict[str, list[dict[str, Any]]] = {}
        for menu_group in menu_groups:
            group_name = menu_group.rec_name or ""
            if not group_name:
                continue
            menu_query_user = await self._make_query_user(
                [{"action_type": "menu"}, {"menu_group": group_name}]
            )
            menu_query = await self._default_query(
                action_model, {"$and": menu_query_user}
            )
            menu_actions = await self._find_base(
                action_model, query=menu_query
            )
            menu_actions = [a for a in menu_actions if self.action_runtime._is_action_allowed(a)]
            logger.info(
                "service_get_menu: group=%s actions_found=%d",
                group_name,
                len(menu_actions) if isinstance(menu_actions, list) else 0,
            )
            if not menu_actions:
                continue

            label = menu_group.label or "No Menu"
            grouped.setdefault(label, [])
            for rec_item in menu_actions:
                grouped[label].append(self._make_button_main_menu(rec_item))

        data = [grouped] if grouped else [{}]
        logger.info(
            "service_get_menu: done labels=%d total_buttons=%d",
            len(grouped),
            sum(len(v) for v in grouped.values()),
        )
        return ResponseObjectData(
            mode="menu", data=data, query=menu_group_query
        )

    async def service_get_dashboard(
        self, parent: str = ""
    ) -> ResponseObjectData:
        action_model = self.env.get("action")
        menu_list = await self._get_basic_menu_list(parent=parent)
        list_cards: list[dict[str, Any]] = []

        for card in menu_list:
            if card.get("model"):
                q_menu_user = await self._make_query_user(
                    [
                        {"action_type": "menu"},
                        {
                            "component_type": {
                                "$in": ["form", "resource", "layout"]
                            }
                        },
                        {"$and": [{"menu_group": card.get("menu_group", "")}]},
                    ]
                )
                q_user = await self._make_query_user(
                    [
                        {"action_type": {"$in": ["window", "process_task"]}},
                        {
                            "component_type": {
                                "$in": ["form", "resource", "layout"]
                            }
                        },
                        {"$and": [{"menu_group": card.get("menu_group", "")}]},
                    ]
                )

                q_menu = await self._default_query(
                    action_model, {"$and": q_menu_user}
                )
                q = await self._default_query(action_model, {"$and": q_user})

                menu_actions = await self._find_base(
                    action_model, query=q_menu
                )
                menu_actions = [a for a in menu_actions if self.action_runtime._is_action_allowed(a)]
                if menu_actions and not _has_non_system_records(menu_actions):
                    continue
                act_list = await self._find_base(action_model, query=q)
                act_list = [a for a in act_list if self.action_runtime._is_action_allowed(a)]
                card_buttons: list[dict[str, Any]] = []

                for rec_b in menu_actions:
                    item = await self._make_menu_item(card, rec_b)
                    if item:
                        card_buttons.append(item)

                for rec_b in act_list:
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
        next_action = (action.next_action_name or "").strip()
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
