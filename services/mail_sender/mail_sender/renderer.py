from __future__ import annotations

from typing import Any

from jinja2 import ChainableUndefined
from jinja2 import Environment


class MailRenderer:
    """Render Jinja del mail_template + wrap nel base template HTML.

    Compatibile col vecchio sistema: subject/recipient/corpoDellaMail sono
    template Jinja valutati col context `{data, form, user, app}`; il corpo
    renderizzato viene iniettato nel base template come `{{ html|safe }}`.
    `ChainableUndefined` rende stringa vuota i placeholder mancanti
    (es. `{{ data.foo.bar }}`) senza sollevare.
    """

    def __init__(self, base_template_html: str, app_name: str = "") -> None:
        self._app_name = app_name
        # autoescape disattivato: i campi sono template HTML/testo che
        # producono HTML (come il sistema originale). I template sono
        # amministrati (mail_template da DB), non input utente diretto.
        self._env = Environment(  # nosemgrep
            autoescape=False,
            undefined=ChainableUndefined,
        )
        self._base = self._env.from_string(base_template_html)

    def render_str(self, template_str: Any, context: dict[str, Any]) -> str:
        text = "" if template_str is None else str(template_str)
        if not text:
            return ""
        return self._env.from_string(text).render(**context)

    def render(
        self,
        template: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[str, list[str], str]:
        subject = self.render_str(template.get("subject", ""), context).strip()
        recipient_raw = self.render_str(template.get("recipient", ""), context)
        recipients = [
            part.strip() for part in recipient_raw.split(",") if part.strip()
        ]
        inner = self.render_str(template.get("corpoDellaMail", ""), context)
        app_name = (
            (context.get("app", {}) or {}).get("app_name") or self._app_name
        )
        html = self._base.render(html=inner, app_name=app_name)
        return subject, recipients, html
