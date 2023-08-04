from typing import Any
import json
from ozonenv.core.db.BsonTypes import JsonEncoder


def encode_dataj(obj) -> str:
    return json.dumps(obj, cls=JsonEncoder)


def check_parse_json(to_test: str) -> Any:
    try:
        to_test = json.loads(to_test)
    except ValueError:
        to_test = to_test.replace("'", "\"")
        try:
            to_test = json.loads(to_test)
        except ValueError:
            return False
    return to_test
