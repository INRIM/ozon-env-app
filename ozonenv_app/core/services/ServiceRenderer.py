import logging
import os

from ozonenv.core.BaseModels import Session
from ozonenv.core.BaseModels import Settings
from starlette.requests import Request

from ozonenv_app.core.ozon.OzonEnvApp import OzonEnvApp
from ozonenv_app.core.services.DateEngine import DateEngine
from ozonenv_app.core.utils.template_settings import templates

logger = logging.getLogger(__name__)


class ServiceRenderer:
    def __init__(self, request: Request):
        self.ozon: OzonEnvApp = None
        self.request: Request = request
        self.session: Session = None
        self.app_settings: Settings = None
        self.basetmp = os.getenv("APP_TAMPLATE_DIR", "")
        self.dte: DateEngine = None
        self.path_obj = {
            'template': "",
            'components': "components/",
            'reports': "reports/",
            'mail': "mail/",
        }

    def get_login_act(self):
        return (
            '/logout'
            if self.ozon.session.token and not self.session.is_public
            else '/login/'
        )

    async def prepare_render(
        self, form_schema, submit, tmpname, rtype="template"
    ):
        logger.info(f"{form_schema}, {submit}, {tmpname}, {rtype}")
        self.ozon = self.request.scope['ozon']
        self.session = self.ozon.session
        self.app_settings: Settings = self.ozon.env.orm.app_settings
        self.dte: DateEngine = DateEngine(
            SERVER_DTTIME_MASK=self.app_settings.server_datetime_mask
        )
        today_date = self.dte.get_tooday_ui()
        template = f"{self.path_obj[rtype]}{tmpname}.html"
        values = {
            'app_name': self.app_settings.module_label,
            "title": "",
            'version': self.app_settings.version,
            # 'env': "test",
            'login_act': self.get_login_act(),
            'login_user': self.session.user.get('full_name', ""),
            'avatar': self.session.user.get('avatar', ""),
            'today_date': today_date,
            "form_model": "",
            "menu_headers": [],
            "beforerows": [],
            "afterrrows": [],
            "backtop": True,
            "error": "",
            "export_button": False,
            "rows": [],
            "user_menu_items": [],
            "request": self.request,
            "logo_img_url": self.app_settings.logo_img_url,
            "builder_mode": self.session.get('app', {}).get("builder", False),
            "form_schema": form_schema,
            "submit": submit,
            "token": self.ozon.env.user_session.token,
        }

        if self.session.is_admin:
            builde_toggle_item = self.render_template(
                f"components/checkbox/form_toggle.html",
                {
                    "key": "builder_mode",
                    "value": self.session.get('app').get("builder"),
                    "authtoken": self.session.token,
                    "label": "Builder",
                    "custom_action": True,
                },
            )
            values['user_menu_items'].append(builde_toggle_item)
        return template, values

    async def render_layout(
        self, form_schema="", submit="", page="layout", rtype="template"
    ):
        if not submit:
            submit = f"/submit/{ form_schema }"
        template, values = await self.prepare_render(
            form_schema, submit, page, rtype
        )
        return self.response_template(template, values)

    async def render_module(
        self,
        form_schema: str,
        rec_name: str,
        submit="",
        page="layout",
        rtype="template",
    ):
        if not submit:
            submit = f"/submit/{ form_schema }"
        template, values = await self.prepare_render(
            form_schema, submit, page, rtype
        )
        values['rec_name'] = rec_name
        return self.response_template(template, values)

    async def prepare_render_builder(self, form_schema, submit, ptype, page):
        logger.info(f"{form_schema}, {submit}, {ptype}, {page}")
        template, values = await self.prepare_render(form_schema, submit, page)

        values['ptype'] = ptype

        return template, values

    async def render_builder(self, ptype="form", rec_name="", page="builder"):
        logger.info(f"{ptype}, {rec_name}, {page}")
        """
        :param page:  name of template
        :param ptype:  form | resource
        :param rec_name: record name or empty
        :return:
        """

        submit = f"/builder/submit"
        if rec_name:
            submit = f"{submit}/{rec_name}"
        template, values = await self.prepare_render_builder(
            rec_name, submit, ptype, page
        )
        return self.response_template(template, values)

    def render_template(self, name: str, context: dict):
        template = templates.get_template(name)
        return template.render(context)

    def response_template(self, name: str, context: dict):
        resp = templates.TemplateResponse(name, context)
        return resp
