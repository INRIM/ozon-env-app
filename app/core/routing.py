from __future__ import annotations

from ozonenv.core.BaseModels import User


def extract_sector_code(user: User) -> str:
    return (
        user.sector_code
        or user.user_data.get("sector_code")
        or user.user_data.get("divisione_code")
        or user.owner_sector
        or ""
    )
