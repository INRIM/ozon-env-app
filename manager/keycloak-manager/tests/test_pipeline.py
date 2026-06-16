import pytest

from kc_provision import KeycloakAdminError

from keycloak_manager import pipeline


class FakeKC:
    def __init__(self, parent):
        self.parent = parent

    async def a_create_client(self, rep, skip_exists=False):
        cid = rep["clientId"]
        self.parent.clients[cid] = f"uuid-{cid}"
        self.parent.created.append(cid)
        return f"uuid-{cid}"

    async def a_add_mapper_to_client_scope(self, scope_uuid, rep):
        self.parent.mappers.setdefault(scope_uuid, []).append(rep)
        return b""


class FakeAdmin:
    """Interfaccia KeycloakAdmin (kc-provision) + .kc, in memoria."""

    def __init__(self, clients=()):
        self.clients = {c: f"uuid-{c}" for c in clients}
        self.secrets = {}
        self.scopes = {}
        self.mappers = {}
        self.assignments = []
        self.created = []
        self.kc = FakeKC(self)

    async def get_client(self, cid):
        return {"id": self.clients[cid], "clientId": cid} if cid in self.clients else None

    async def create_client(self, cid):
        self.clients[cid] = f"uuid-{cid}"
        self.created.append(cid)
        return {"id": f"uuid-{cid}", "clientId": cid}

    async def client_secret(self, uuid):
        return f"secret-{uuid}"

    async def get_client_scope(self, name):
        return {"id": self.scopes[name], "name": name} if name in self.scopes else None

    async def create_client_scope(self, name):
        self.scopes[name] = f"scope-{name}"
        self.mappers[f"scope-{name}"] = []
        return {"id": f"scope-{name}", "name": name}

    async def list_scope_mappers(self, scope_uuid):
        return self.mappers.get(scope_uuid, [])

    async def assign_default_scope(self, client_uuid, scope_uuid):
        self.assignments.append((client_uuid, scope_uuid))


async def test_ensure_app_client_creates_when_absent():
    admin = FakeAdmin()
    res = await pipeline.ensure_app_client(admin, "nob-app")
    assert res.created is True
    assert res.uuid == "uuid-nob-app"
    assert "nob-app" in admin.created


async def test_ensure_app_client_existing():
    admin = FakeAdmin(clients=["nob-app"])
    res = await pipeline.ensure_app_client(admin, "nob-app")
    assert res.created is False
    assert admin.created == []


async def test_ensure_m2m_creates_and_secret():
    admin = FakeAdmin()
    res = await pipeline.ensure_m2m_client(admin, "svc")
    assert res.created is True
    assert res.secret == "secret-uuid-svc"


async def test_bind_client_audience_mapper_and_assign():
    admin = FakeAdmin(clients=["nob-app", "svc"])
    res = await pipeline.bind_client_audience(
        admin,
        scope_name="svc-audience",
        app_client_id="nob-app",
        assign_to_client_ids=["svc", "nob-app"],
    )
    assert res.created_scope is True
    assert res.created_mapper is True
    assert res.assigned == ["svc", "nob-app"]
    # mapper = client-audience verso nob-app
    mapper = admin.mappers[res.scope_id][0]
    assert mapper["protocolMapper"] == "oidc-audience-mapper"
    assert mapper["config"]["included.client.audience"] == "nob-app"
    assert mapper["config"]["access.token.claim"] == "true"
    # assegnato a entrambi i client
    assert len(admin.assignments) == 2


async def test_bind_audience_aborts_when_target_missing():
    admin = FakeAdmin(clients=["nob-app"])  # manca 'svc'
    with pytest.raises(KeycloakAdminError, match="svc"):
        await pipeline.bind_client_audience(
            admin,
            scope_name="svc-audience",
            app_client_id="nob-app",
            assign_to_client_ids=["svc"],
        )


async def test_bind_audience_mapper_idempotent():
    admin = FakeAdmin(clients=["nob-app", "svc"])
    await pipeline.bind_client_audience(
        admin,
        scope_name="svc-audience",
        app_client_id="nob-app",
        assign_to_client_ids=["svc"],
    )
    res2 = await pipeline.bind_client_audience(
        admin,
        scope_name="svc-audience",
        app_client_id="nob-app",
        assign_to_client_ids=["svc"],
    )
    assert res2.created_scope is False
    assert res2.created_mapper is False
