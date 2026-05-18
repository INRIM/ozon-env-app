import logging
from dataclasses import dataclass
from typing import Any

from app.services.components.selectComponentService import (
    build_remote_select_header,
)
from app.services.remote_service import remote_data_select_response
from app.services.utils import (
    check_parse_json,
    decode_resource_template,
    fetch_dict_get_value,
)

logger = logging.getLogger("uvicorn.error")

_SELECT_SORT = "list_order:asc,rec_name:asc"


@dataclass(slots=True)
class SelectFieldConfig:
    curr_model: str
    field_key: str
    field: dict[str, Any]
    props: dict[str, Any]
    src: str
    url: str
    resource_model: str = ""


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
    if hasattr(item, "get_dict"):
        dumped = item.get_dict()
        if isinstance(dumped, dict):
            return dumped
    if hasattr(item, "dict"):
        dumped = item.dict()
        if isinstance(dumped, dict):
            return dumped
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
        keys = [key.strip() for key in label_key.split(",") if key.strip()]
        if len(keys) > 1:
            parts = [str(item.get(key, "")).strip() for key in keys]
            label = " ".join(part for part in parts if part) or None
        else:
            label = item.get(label_key)

    if id_key:
        value = item.get(id_key)

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
    return str(props.get("id", "") or props.get("value", "") or "").strip()


def _extract_field_data(field: dict[str, Any]) -> dict[str, Any]:
    raw_data = field.get("data", {})
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, str):
        parsed = check_parse_json(raw_data)
        if isinstance(parsed, dict):
            return parsed
    return {}


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


def _resolve_resource_model_name(
    field: dict[str, Any],
    props: dict[str, Any],
) -> str:
    data = _extract_field_data(field)
    candidates = (
        field.get("resource_id", ""),
        field.get("resource", ""),
        data.get("resource", ""),
        props.get("resource", ""),
        props.get("model", ""),
    )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _parse_domain(raw_domain: Any) -> dict[str, Any]:
    if isinstance(raw_domain, dict):
        return raw_domain.copy()
    if raw_domain in ("", None):
        return {}
    parsed = check_parse_json(raw_domain)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_row_list(raw_data: Any) -> list[Any]:
    if isinstance(raw_data, list):
        return list(raw_data)
    if isinstance(raw_data, tuple):
        return list(raw_data)
    if isinstance(raw_data, dict):
        return [raw_data]
    if isinstance(raw_data, str):
        parsed = check_parse_json(raw_data)
        if parsed != raw_data:
            return _normalize_row_list(parsed)
    return []


def _load_inline_values(field: dict[str, Any]) -> list[Any]:
    data = _extract_field_data(field)
    return _normalize_row_list(data.get("values", field.get("values", [])))


def _load_inline_custom_rows(field: dict[str, Any]) -> list[Any]:
    data = _extract_field_data(field)
    candidates = (
        field.get("custom", []),
        data.get("custom", []),
        data.get("json", []),
    )
    for candidate in candidates:
        rows = _normalize_row_list(candidate)
        if rows:
            return rows
    return []


def _build_select_field_config(
    service: Any,
    curr_model: str,
    field_key: str,
) -> SelectFieldConfig | None:
    model = service.env.get(curr_model).model
    raw_select_fields = model.select_fields()
    select_fields = _normalize_select_fields(curr_model, raw_select_fields)
    raw_field = select_fields.get(field_key)
    field = _normalize_field_definition(field_key, raw_field)
    if not field:
        logger.warning(
            "formio select empty field config key=%s model=%s",
            field_key,
            curr_model,
        )
        return None

    props = _normalize_component_properties(
        field_key,
        field.get("properties", {}),
    )
    src = str(field.get("src", "") or "").strip().lower()
    url = str(field.get("url", "") or "").strip()
    resource_model = _resolve_resource_model_name(field, props)

    logger.info(
        "formio select field loaded key=%s model=%s src=%s url=%s props=%s resource_model=%s",
        field_key,
        curr_model,
        src,
        url,
        list(props.keys()),
        resource_model,
    )
    return SelectFieldConfig(
        curr_model=curr_model,
        field_key=field_key,
        field=field,
        props=props,
        src=src,
        url=url,
        resource_model=resource_model,
    )


async def _load_values_source(
    model: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    if hasattr(model, "select_options") and callable(model.select_options):
        data = model.select_options(config.field_key)
        rows = _normalize_row_list(data)
        if rows:
            logger.info(
                "formio select source=values key=%s count=%s via=model.select_options",
                config.field_key,
                len(rows),
            )
            return rows

    rows = _load_inline_values(config.field)
    logger.info(
        "formio select source=values key=%s count=%s via=field.data.values",
        config.field_key,
        len(rows),
    )
    return rows


async def _load_resource_source(
    service: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    if not config.resource_model:
        logger.warning(
            "formio select missing resource model key=%s model=%s",
            config.field_key,
            config.curr_model,
        )
        return []

    resource_model = service.env.get(config.resource_model)
    domain = resource_model.get_domain({"active": True, "deleted": 0})
    data = await resource_model.find(
        domain=domain,
        sort=_SELECT_SORT,
        limit=0,
    )
    rows = _normalize_row_list(data)
    logger.info(
        "formio select source=resource key=%s resource_model=%s count=%s",
        config.field_key,
        config.resource_model,
        len(rows),
    )
    return rows


async def _load_local_url_source(
    service: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    model_name = str(config.props.get("model", "") or "").strip()
    if not model_name:
        logger.warning(
            "formio select missing properties.model key=%s src=url url=%s",
            config.field_key,
            config.url,
        )
        return []

    domain = _parse_domain(config.props.get("domain", {}))
    data = await service.get_distinct(
        model_name,
        domain,
        str(config.props.get("compute_label", "") or ""),
    )
    rows = _normalize_row_list(data)
    logger.info(
        "formio select source=model-distinct key=%s model=%s count=%s",
        config.field_key,
        model_name,
        len(rows),
    )
    return rows


async def _load_remote_url_source(
    service: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    header = build_remote_select_header(config.field)
    data = await remote_data_select_response(
        service=service,
        url=header.url,
        path_value=header.path_value,
        header_key=header.header_key,
        header_value_key=header.header_value_key,
    )
    rows = _normalize_row_list(data)
    logger.info(
        "formio select source=remote-url key=%s url=%s count=%s",
        config.field_key,
        header.url,
        len(rows),
    )
    return rows


async def _load_custom_source(
    _service: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    rows = _load_inline_custom_rows(config.field)
    logger.info(
        "formio select source=custom key=%s count=%s",
        config.field_key,
        len(rows),
    )
    return rows


async def _load_select_source_rows(
    service: Any,
    model: Any,
    config: SelectFieldConfig,
) -> list[Any]:
    if config.src == "values":
        return await _load_values_source(model, config)
    if config.src == "resource":
        return await _load_resource_source(service, config)
    if config.src == "custom":
        return await _load_custom_source(service, config)
    if config.src == "url":
        if config.url.lower().startswith(("http://", "https://")):
            return await _load_remote_url_source(service, config)
        return await _load_local_url_source(service, config)

    logger.warning(
        "formio select unsupported source key=%s model=%s src=%s",
        config.field_key,
        config.curr_model,
        config.src,
    )
    return []


def make_resource_list(field: dict[str, Any], data: list[Any]) -> list[dict[str, Any]]:
    if not isinstance(field, dict):
        logger.warning(
            "formio select make_resource_list invalid field type=%s",
            type(field).__name__,
        )
        return []

    resource_list = data or []
    field_key = str(field.get("key", "") or "").strip()
    props = _normalize_component_properties(field_key, field.get("properties", {}))
    value_key = _resolve_value_key(props)
    src = str(field.get("src", "") or "").strip().lower()

    default_template = "<span>{{ item.label }}</span>"
    template_label_keys = decode_resource_template(default_template)
    if src in {"resource", "custom"}:
        template = field.get("template") or default_template
        if field.get("multi", False):
            template_label_keys = decode_resource_template(template)

    id_path = value_key or "id"
    values: list[dict[str, Any]] = []

    for idx, raw_item in enumerate(resource_list):
        item = _obj_to_dict(raw_item)
        if not item:
            logger.warning(
                "formio select row normalize failed key=%s src=%s index=%s raw_type=%s",
                field_key,
                src,
                idx,
                type(raw_item).__name__,
            )

        if src == "resource":
            label = _safe_fetch_template_value(
                item,
                template_label_keys,
                field_key,
                "resource",
            )
            value = (
                item.get("rec_name")
                or item.get(value_key)
                or item.get("id")
                or item.get("value")
            )
            if label in (None, "") or value in (None, ""):
                fallback_label, fallback_value = _normalize_label_and_value(
                    item,
                    str(props.get("label", "") or ""),
                    value_key,
                    fallback_field_key=field_key,
                )
                if label in (None, ""):
                    label = fallback_label
                if value in (None, ""):
                    value = fallback_value
        elif src == "custom":
            label = _safe_fetch_template_value(
                item,
                template_label_keys,
                field_key,
                "custom",
            )
            if not item.get(id_path):
                logger.error(
                    "formio select custom missing id_path key=%s id_path=%s",
                    field_key,
                    id_path,
                )
            value = item.get(id_path)
            if label in (None, "") or value in (None, ""):
                fallback_label, fallback_value = _normalize_label_and_value(
                    item,
                    str(props.get("label", "") or ""),
                    value_key,
                    fallback_field_key=field_key,
                )
                if label in (None, ""):
                    label = fallback_label
                if value in (None, ""):
                    value = fallback_value
        else:
            label, value = _normalize_label_and_value(
                item,
                str(props.get("label", "") or ""),
                value_key,
                fallback_field_key=field_key,
            )

        values.append({"label": label, "value": value})
    return values


async def get_formio_select_options(
    service: Any,
    curr_model: str,
    field_key: str,
) -> list[dict[str, Any]]:
    logger.info(
        "get_formio_select_options field=%s model=%s",
        field_key,
        curr_model,
    )
    config: SelectFieldConfig | None = None
    try:
        model = service.env.get(curr_model).model
        config = _build_select_field_config(service, curr_model, field_key)
        if config is None:
            return []

        rows = await _load_select_source_rows(service, model, config)
        logger.info(
            "formio select final key=%s model=%s src=%s count=%s",
            field_key,
            curr_model,
            config.src,
            len(rows),
        )
        return make_resource_list(config.field, rows)
    except Exception:
        logger.exception(
            "formio select failed key=%s model=%s src=%s url=%s resource_model=%s",
            field_key,
            curr_model,
            getattr(config, "src", ""),
            getattr(config, "url", ""),
            getattr(config, "resource_model", ""),
        )
        return []
