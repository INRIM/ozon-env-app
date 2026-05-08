from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Optional

from ozonenv.core.BaseModels import User
from pydantic import Field


class AppSession(User):
    """User model extended with SSO and per-request session fields."""

    sso_token: str = Field(default="")
    sso_refresh: str = Field(default="")
    sso_expire: Optional[datetime] = None
    expire_datetime: Optional[datetime] = None
    login_complete: bool = False
    divisione_uo: str = ""
    app: dict[str, Any] = Field(default_factory=dict)
    apps: dict[str, Any] = Field(default_factory=dict)
    last_update: float = 0.0

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "extra": "allow",
    }
