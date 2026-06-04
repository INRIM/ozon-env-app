from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_TEMPLATE = (
    Path(__file__).resolve().parent / "templates" / "mail_base_template.html"
)


@dataclass(frozen=True)
class MailSenderConfig:
    poll_interval: float
    app_name: str
    base_url: str
    base_template_path: str

    @classmethod
    def from_env(cls) -> "MailSenderConfig":
        return cls(
            poll_interval=float(os.getenv("MAIL_POLL_INTERVAL", "30")),
            app_name=(
                os.getenv("OZON_APP_NAME")
                or os.getenv("APP_CODE")
                or "App"
            ),
            base_url=os.getenv("EXTERNAL_BASE_URL", ""),
            base_template_path=os.getenv(
                "MAIL_BASE_TEMPLATE", str(_DEFAULT_TEMPLATE)
            ),
        )

    def base_template_html(self) -> str:
        return Path(self.base_template_path).read_text(encoding="utf-8")
