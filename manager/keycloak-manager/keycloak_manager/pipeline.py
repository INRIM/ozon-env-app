from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kc_provision import KeycloakAdminError


@dataclass
class AppClientResult:
    client_id: str
    uuid: str
    created: bool


@dataclass
class M2MResult:
    client_id: str
    uuid: str
    secret: str
    created: bool


@dataclass
class AudienceResult:
    scope_id: str
    app_client_id: str
    created_scope: bool
    created_mapper: bool
    assigned: list[str]


async def ensure_app_client(
    admin: Any, client_id: str, *, confidential: bool = True
) -> AppClientResult:
    """Crea (se assente) il client app = resource server (standard flow)."""
    existing = await admin.get_client(client_id)
    if existing:
        return AppClientResult(client_id, existing["id"], False)
    rep = {
        "clientId": client_id,
        "protocol": "openid-connect",
        "enabled": True,
        "publicClient": not confidential,
        "standardFlowEnabled": True,
        "serviceAccountsEnabled": False,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
    }
    await admin.kc.a_create_client(rep, skip_exists=False)
    created = await admin.get_client(client_id)
    if not created:
        raise KeycloakAdminError(
            f"app client '{client_id}' non trovato dopo la creazione"
        )
    return AppClientResult(client_id, created["id"], True)


async def ensure_m2m_client(admin: Any, client_id: str) -> M2MResult:
    """Crea (se assente) il client M2M (client_credentials) + secret."""
    existing = await admin.get_client(client_id)
    if existing:
        secret = await admin.client_secret(existing["id"])
        return M2MResult(client_id, existing["id"], secret, False)
    rep = await admin.create_client(client_id)  # M2M: serviceAccountsEnabled
    secret = await admin.client_secret(rep["id"])
    return M2MResult(client_id, rep["id"], secret, True)


async def _ensure_client_audience_mapper(
    admin: Any, scope_uuid: str, mapper_name: str, app_client_id: str
) -> bool:
    """Mapper **client**-audience: aud del token = clientId dell'app."""
    for mapper in await admin.list_scope_mappers(scope_uuid):
        if mapper.get("name") == mapper_name:
            return False
    rep = {
        "name": mapper_name,
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "config": {
            "included.client.audience": app_client_id,
            "id.token.claim": "false",
            "access.token.claim": "true",
        },
    }
    await admin.kc.a_add_mapper_to_client_scope(scope_uuid, rep)
    return True


async def bind_client_audience(
    admin: Any,
    *,
    scope_name: str,
    app_client_id: str,
    assign_to_client_ids: list[str],
) -> AudienceResult:
    """Client scope con mapper client-audience verso `app_client_id`, assegnato
    come default ai client indicati (così i loro token portano aud=app_client_id).
    Idempotente."""
    scope = await admin.get_client_scope(scope_name)
    created_scope = False
    if not scope:
        scope = await admin.create_client_scope(scope_name)
        created_scope = True
    mapper_name = f"audience-{app_client_id}"
    created_mapper = await _ensure_client_audience_mapper(
        admin, scope["id"], mapper_name, app_client_id
    )
    assigned: list[str] = []
    for cid in assign_to_client_ids:
        rep = await admin.get_client(cid)
        if not rep:
            raise KeycloakAdminError(
                f"client '{cid}' non esiste: impossibile assegnare l'audience"
            )
        await admin.assign_default_scope(rep["id"], scope["id"])
        assigned.append(cid)
    return AudienceResult(
        scope_id=scope["id"],
        app_client_id=app_client_id,
        created_scope=created_scope,
        created_mapper=created_mapper,
        assigned=assigned,
    )
