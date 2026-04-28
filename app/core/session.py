from __future__ import annotations

from datetime import datetime

from ozonenv.core.BaseModels import Session
from pydantic import Field


class AppSession(Session):
    sso_token: str = Field(default="")
    sso_refresh: str = Field(default="")
    sso_expire: datetime | None = None
