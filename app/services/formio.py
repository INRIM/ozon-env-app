import logging
from typing import Any

from app.services.components.selectComponentService import (
    build_remote_select_header,
)
from app.services.remote_service import remote_data_select_response
from app.services.utils import *

logger = logging.getLogger("uvicorn.error")


def _normalize_component_properties(
    field_key: str, raw_props: Any
) -> dict[str, Any]:
    if isinstance(raw_props, dict):
        return raw_props
    if isinstance(raw_props, str):
        parsed = check_parse_json(raw_props)
        if isinstance(parsed, dict):
            logger.info(
                "formio select parsed properties from string key=%s",
                field_key,
            )
            return parsed
    if raw_props not in ("", None, [], {}, ()):
        logger.warning(
            "formio select invalid properties key=%s type=%s",
            field_key,
            type(raw_props).__name__,
        )
    return {}


def _normalize_field_definition(
    field_key: str, raw_field: Any
) -> dict[str, Any]:
    if isinstance(raw_field, dict):
        return raw_field
    if hasattr(raw_field, "model_dump"):
        dumped = raw_field.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(raw_field, "dict"):
        dumped = raw_field.dict()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(raw_field, str):
        parsed = check_parse_json(raw_field)
        if isinstance(parsed, dict):
            logger.info(
                "formio select parsed field definition from string key=%s",
                field_key,
            )
            return parsed
    if raw_field in ("", None, [], {}, ()):
        logger.warning("formio select field not found key=%s", field_key)
    else:
        logger.warning(
            "formio select invalid field key=%s type=%s",
            field_key,
            type(raw_field).__name__,
        )
    return {}


def _normalize_select_fields(
    curr_model: str, raw_select_fields: Any
) -> dict[str, Any]:
    if isinstance(raw_select_fields, dict):
        return raw_select_fields
    if isinstance(raw_select_fields, str):
        parsed = check_parse_json(raw_select_fields)
        if isinstance(parsed, dict):
            logger.info(
                "formio select parsed select_fields from string model=%s",
                curr_model,
            )
            return parsed
    if raw_select_fields in ("", None, [], {}, ()):
        logger.warning("formio select empty select_fields model=%s", curr_model)
    else:
        logger.warning(
            "formio select invalid select_fields model=%s type=%s",
            curr_model,
            type(raw_select_fields).__name__,
        )
    return {}


def _obj_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        dumped = item.model_dump()
        if isinstance(dumped, dict):
            return dumped
        return {}
    if hasattr(item, "get_dict"):
        dumped = item.get_dict()
        if isinstance(dumped, dict):
            return dumped
        return {}
    if hasattr(item, "dict"):
        dumped = item.dict()
        if isinstance(dumped, dict):
            return dumped
        return {}
    return {}


def _normalize_label_and_value(
    item: dict[str, Any],
    label_key: str = "",
    id_key: str = "",
    fallback_field_key: str = "",
) -> tuple[Any, Any]:
    label = None
    value = None

    if label_key:
        keys = [k.strip() for k in label_key.split(",") if k.strip()]
        if len(keys) > 1:
            parts = [str(item.get(k, "")).strip() for k in keys]
            joined = " ".join([p for p in parts if p])
            label = joined if joined else None
        else:
            label = item.get(label_key)

    if id_key:
        value = item.get(id_key)

    # Fallback esplicito: se non configurato altro, prova direttamente il key del campo.
    if label in (None, "") and fallback_field_key:
        label = item.get(fallback_field_key)
    if value in (None, "") and fallback_field_key:
        value = item.get(fallback_field_key)

    if label in (None, ""):
        label = (
            item.get("label")
            or item.get("v")
            or item.get("title")
            or item.get("name")
            or item.get("description")
            or item.get("rec_name")
            or item.get("id")
            or item.get("value")
            or ""
        )

    if value in (None, ""):
        value = (
            item.get("value")
            or item.get("k")
            or item.get("id")
            or item.get("rec_name")
        )

    if value in (None, ""):
        value = label

    return label, value


def _resolve_value_key(props: dict[str, Any]) -> str:
    if not isinstance(props, dict):
        return ""
    return props.get("id", "") or props.get("value", "")


def _safe_fetch_template_value(
    item: dict[str, Any],
    template_label_keys: list[str],
    field_key: str,
    src: str,
) -> Any:
    try:
        return fetch_dict_get_value(item, template_label_keys[:])
    except Exception:
        logger.warning(
            "formio select template resolve failed key=%s src=%s path=%s",
            field_key,
            src,
            ".".join(template_label_keys),
        )
        return None


def make_resource_list(field: dict, data: list) -> list[dict]:
    if not isinstance(field, dict):
        logger.warning(
            "formio select make_resource_list invalid field type=%s",
            type(field).__name__,
        )
        return []
    resource_list = data or []
    multi = field.get("multi", False)
    default_template = "<span>{{ item.label }}</span>"
    template_label_keys = decode_resource_template(default_template)
    idPath = None
    props = _normalize_component_properties(
        field.get("key", ""),
        field.get("properties", {}),
    )
    value_key = _resolve_value_key(props)
    src = field.get("src", False)
    values = []

    if src and src in ["resource", "custom"]:
        template = field.get("template") or default_template
        if multi:
            template_label_keys = decode_resource_template(template)
        if src == "custom" and not idPath:
            idPath = value_key or "id"

    field_key = field.get("key", "")
    for idx, raw_item in enumerate(resource_list):
        item = _obj_to_dict(raw_item)
        if not isinstance(item, dict):
            logger.warning(
                "formio select row normalize failed key=%s src=%s index=%s raw_type=%s",
                field_key,
                src,
                idx,
                type(raw_item).__name__,
            )
            item = {}

        if src == "resource":
            label = _safe_fetch_template_value(
                item,
                template_label_keys,
                field_key,
                "resource",
            )
            iid = (
                item.get("rec_name")
                or item.get(value_key)
                or item.get("id")
                or item.get("value")
            )
            if label in (None, "") or iid in (None, ""):
                f_label, f_value = _normalize_label_and_value(
                    item,
                    props.get("label", ""),
                    value_key,
                    fallback_field_key=field_key,
                )
                label = label if label not in (None, "") else f_label
                iid = iid if iid not in (None, "") else f_value
        elif src == "custom":
            label = _safe_fetch_template_value(
                item,
                template_label_keys,
                field_key,
                "custom",
            )
            if not item.get(idPath):
                logger.error(
                    "No key %s in resources for source Custom",
                    idPath,
                )
            iid = item.get(idPath)
            if label in (None, "") or iid in (None, ""):
                f_label, f_value = _normalize_label_and_value(
                    item,
                    props.get("label", ""),
                    value_key,
                    fallback_field_key=field_key,
                )
                label = label if label not in (None, "") else f_label
                iid = iid if iid not in (None, "") else f_value
        else:
            label, iid = _normalize_label_and_value(
                item,
                props.get("label", ""),
                value_key,
                fallback_field_key=field_key,
            )

        values.append({"label": label, "value": iid})
    return values


async def get_formio_select_options(
        service, curr_model, field_key
) -> list[dict]:
    logger.info(
        "get_formio_select_options field=%s model=%s",
        field_key,
        curr_model,
    )
    context: dict[str, Any] = {
        "src": "",
        "url": "",
        "model_name": "",
        "select_fields_type": "",
        "raw_field_type": "",
        "field_type": "",
        "properties_type": "",
    }
    try:
        model = service.env.get(curr_model).model
        raw_select_fields = model.select_fields()
        context["select_fields_type"] = type(raw_select_fields).__name__
        select_fields = _normalize_select_fields(curr_model, raw_select_fields)
        raw_field = select_fields.get(field_key)
        context["raw_field_type"] = type(raw_field).__name__
        field = _normalize_field_definition(field_key, raw_field)
        context["field_type"] = type(field).__name__
        if not field:
            logger.warning(
                "formio select empty field config key=%s model=%s select_fields_type=%s raw_field_type=%s",
                field_key,
                curr_model,
                context["select_fields_type"],
                context["raw_field_type"],
            )
            return []
        raw_properties = field.get("properties", {})
        context["properties_type"] = type(raw_properties).__name__
        props = _normalize_component_properties(
            field_key,
            raw_properties,
        )
        src = field.get("src", False)
        context["src"] = str(src)
        url = field.get("url") or ""
        if not isinstance(url, str):
            url = str(url)
        context["url"] = url
        data = []

        logger.info(
            "formio select config key=%s model=%s src=%s url=%s properties_keys=%s",
            field_key,
            curr_model,
            context["src"],
            context["url"],
            list(props.keys()),
        )
        if src == "values":
            data = model.select_options(field_key)
            logger.info("formio select source=values count=%s", len(data))
        elif src and src == "url":
            if not url.lower().startswith(("http://", "https://")):
                model_name = props.get("model", "")
                context["model_name"] = model_name
                if not model_name:
                    logger.warning(
                        "formio select missing properties.model key=%s src=%s url=%s",
                        field_key,
                        context["src"],
                        context["url"],
                    )
                    return []
                domain = props.get("domain", {})
                if domain in ("", None):
                    domain = {}
                else:
                    parsed_domain = check_parse_json(domain)
                    domain = (
                        parsed_domain if isinstance(parsed_domain, dict) else {}
                    )
                data = await service.get_distinct(
                    model_name,
                    domain,
                    props.get("compute_label", ""),
                )
            else:
                header = build_remote_select_header(field)
                data = await remote_data_select_response(
                    service=service,
                    url=header.url,
                    path_value=header.path_value,
                    header_key=header.header_key,
                    header_value_key=header.header_value_key,
                )

        logger.info("formio select final count=%s", len(data))
        return make_resource_list(field, data)
    except Exception:
        logger.exception(
            "formio select failed key=%s model=%s src=%s url=%s model_name=%s select_fields_type=%s raw_field_type=%s field_type=%s properties_type=%s",
            field_key,
            curr_model,
            context.get("src", ""),
            context.get("url", ""),
            context.get("model_name", ""),
            context.get("select_fields_type", ""),
            context.get("raw_field_type", ""),
            context.get("field_type", ""),
            context.get("properties_type", ""),
        )
        return []
