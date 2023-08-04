# Copyright INRIM (https://www.inrim.eu)
# See LICENSE file for full licensing details.
import json
import os

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(
    directory=os.getenv("APP_TAMPLATE_DIR", "/app/views/templates"))



# TODO test set Jinja2 work in async mode
# ,enable_async=True)


# setup Jinja2 filters

def cssid(input):
    """Custom filter"""
    return f"#{input}"


def cssid_div(input):
    """Custom filter"""
    return f"#{input}_div"


def parse_json(input):
    """Custom filter"""
    res = {}
    try:
        return json.loads(input)
    except Exception as e:
        return res


def format_currency(value):
    try:
        return "${:,.2f}".format(value)
    except Exception as e:
        return "0,0"


templates.env.filters['cssid'] = cssid
templates.env.filters['cssid_div'] = cssid_div
templates.env.filters['parse_json'] = parse_json
templates.env.filters['fcurrency'] = format_currency
