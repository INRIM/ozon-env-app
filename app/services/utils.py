import json
from collections.abc import AsyncIterable, Iterable
from typing import Any
import copy
import re
from bson.decimal128 import Decimal128


def sanitize_mongo_data(data: Any) -> Any:
    """Converte ricorsivamente i tipi MongoDB come Decimal128 in tipi Python nativi."""
    if isinstance(data, dict):
        return {k: sanitize_mongo_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_mongo_data(item) for item in data]
    elif isinstance(data, Decimal128):
        return float(data.to_decimal())
    return data

def _to_dict(item: Any) -> Any:
    if item is None:
        return None
    if isinstance(item, dict):
        return sanitize_mongo_data(item)
    if hasattr(item, "model_dump"):
        return sanitize_mongo_data(item.model_dump())
    if hasattr(item, "get_dict"):
        return sanitize_mongo_data(item.get_dict())
    if hasattr(item, "dict"):
        return sanitize_mongo_data(item.dict())
    return sanitize_mongo_data(item)

async def _iter_items(data: Any):
    if isinstance(data, dict):
        yield data
        return
    if isinstance(data, AsyncIterable) or hasattr(data, "__aiter__"):
        async for item in data:
            yield item
        return
    if isinstance(data, Iterable) and not isinstance(data, (str, bytes, bytearray)):
        for item in data:
            yield item
        return
    yield data

async def _stream_ndjson(data: Any):
    try:
        async for item in _iter_items(data):
            row = _to_dict(item)
            if row is None:
                continue
            yield f"{json.dumps(row, ensure_ascii=False, default=str)}\n"
    except Exception as exc:
        yield f"{json.dumps({'error': str(exc)}, ensure_ascii=False, default=str)}\n"

async def _stream_ndjson_with_start_packet(data_cursor: Any, meta: Any):
    try:
        # 1. Start Packet: Svuotiamo i dati per alleggerire la busta
        if hasattr(meta, "content") and hasattr(meta.content, "data"):
            meta.content.data = []

        # Facciamo il dump del modello in JSON
        if hasattr(meta, "model_dump_json"):
            yield f"{meta.model_dump_json()}\n"
        else:
            yield f"{json.dumps(_to_dict(meta), ensure_ascii=False, default=str)}\n"

        # 2. Record Row: Inviamo i record riga per riga
        async for item in _iter_items(data_cursor):
            row = _to_dict(item)
            if row is None:
                continue
            yield f"{json.dumps(row, ensure_ascii=False, default=str)}\n"

    except Exception as exc:
        err = {"_stream_error": True, "message": str(exc)}
        # RISOLTO: Yield inserita per restituire il json d'errore allo stream!
        yield f"{json.dumps(err, ensure_ascii=False)}\n"

def check_parse_json(str_test):
    try:
        import ujson
    except ImportError:
        import json as ujson
    try:
        str_test = json.loads(str_test)
    except ValueError:
        if isinstance(str_test, str):
            str_test = str_test.replace("'", '"')
            try:
                str_test = ujson.loads(str_test)
            except ValueError:
                return str_test
    return str_test

def decode_resource_template(tmp):
    res = re.sub(r"<.*?>", " ", tmp)
    strcleaned = re.sub(r'\{{ |\ }}', "", res)
    list_kyes = strcleaned.strip().split(".")
    return list_kyes[1:]

def fetch_dict_get_value(dict_src, list_keys):
    if len(list_keys) == 0:
        return
    node = list_keys[0]
    list_keys.remove(node)
    nextdict = dict_src.get(node)
    if len(list_keys) >= 1:
        return fetch_dict_get_value(nextdict, list_keys)
    else:
        return dict_src.get(node)

def extract_remote_data(datar: Any) -> Any:
    data = copy.deepcopy(datar)
    if isinstance(datar, dict) and datar.get("result"):
        result = datar.get("result")
        if isinstance(result, dict) and result.get("select_list"):
            return result.get("select_list", [])
        if isinstance(result, list):
            return result
    return data