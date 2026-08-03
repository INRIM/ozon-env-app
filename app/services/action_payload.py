from __future__ import annotations

from typing import Any


LEGACY_ACTION_CREDENTIAL_FIELDS = frozenset(
    {
        "authtoken",
        "authToken",
        "auth_token",
    }
)


def sanitize_action_payload(payload: Any) -> dict[str, Any]:
    """Rimuove credenziali legacy dai payload azione.

    Le action sono autenticate esclusivamente dal cookie di sessione o dal
    bearer del trasporto. Una credenziale nel body non viene validata e non
    deve raggiungere il runtime o essere persistita come dato applicativo.
    """
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key not in LEGACY_ACTION_CREDENTIAL_FIELDS
    }
