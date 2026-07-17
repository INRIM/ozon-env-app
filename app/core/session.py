from __future__ import annotations

from datetime import datetime
from typing import Any
from typing import Optional

from ozonenv.core.BaseModels import User
from pydantic import Field


class AppSession(User):
    """User model extended with SSO and per-request session fields.

    `token`/`sso_token`/`sso_refresh`/`claims` carry the raw Keycloak
    credential bundle and decoded JWT claims (email, roles, groups...) —
    marked `exclude=True` so they never leak through `get_dict()`/
    `model_dump()` towards an external client (e.g. `GET /get_session`),
    same treatment the base `User.password` field already gets. This is
    defense in depth: the primary fix is `GET /get_session` explicitly
    stripping them regardless of this flag (see
    docs/SECURITY_KEYCLOAK_TOKEN_ANALYSIS.it.md, finding #1) — but unlike
    that per-endpoint pop, this covers every current and future dump exit
    (e.g. the `user.session.persist` webhook payload, finding #9)."""

    token: dict[str, Any] | str = Field(default_factory=dict, exclude=True)
    sso_token: str = Field(default="", exclude=True)
    sso_refresh: str = Field(default="", exclude=True)
    claims: dict[str, Any] = Field(default_factory=dict, exclude=True)
    sso_expire: Optional[datetime] = None
    expire_datetime: Optional[datetime] = None
    login_complete: bool = False
    is_tech: bool = False
    divisione_uo: str = ""
    app: dict[str, Any] = Field(default_factory=dict)
    apps: dict[str, Any] = Field(default_factory=dict)
    last_update: float = 0.0

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "extra": "allow",
    }
