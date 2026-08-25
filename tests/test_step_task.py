import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.step_router import router
from app.deps.app_env import get_authed_env
from app.deps.app_env import get_service
from app.services.common import ResponseObject
from app.services.common import ResponseObjectData
from app.services.step_task import complete_step_task


class StepModel:
    model_fields = {"rec_name": object(), "todo": object()}

    @classmethod
    def step_field(cls, name):
        if name == "todo_supervised_approval":
            return {
                "rec_name": "supervised_todo",
                "action_type": "task",
                "url_action": "/step/request/approval",
                # La key la legge ModelMaker dal componente checkbox del
                # fieldset: le palette spediscono "todo".
                "checkbox_field": "todo",
            }
        if name == "todo_supervised_no_checkbox":
            return {"rec_name": "supervised_todo", "checkbox_field": ""}
        if name == "todo_supervised_ghost":
            return {"rec_name": "supervised_todo", "checkbox_field": "nope"}
        return None


class FakeService:
    def __init__(self, *, editable=True, load_fail=False):
        self.editable = editable
        self.load_fail = load_fail
        self.upserts = []
        self.record_model = SimpleNamespace(model=StepModel())

    def _get_model(self, model_name):
        if model_name != "request":
            raise HTTPException(status_code=404, detail="model not found")
        return self.record_model

    async def load_record(self, model_name, rec_name):
        return ResponseObject(
            fail=self.load_fail,
            message="record not found" if self.load_fail else "",
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data={"rec_name": rec_name},
                editable=self.editable,
            )
        )

    async def upsert(self, model_name, data=None, rec_name=""):
        self.upserts.append((model_name, data.copy(), rec_name))
        return ResponseObject(
            content=ResponseObjectData(
                mode="form",
                model=model_name,
                rec_name=rec_name,
                data=data,
            )
        )


def test_complete_step_sets_checkbox_false_and_saves_update():
    service = FakeService()

    response = asyncio.run(
        complete_step_task(
            service,
            "request",
            "approval",
            {
                "rec_name": "req-1",
                "todo": True,
                "title": "Request",
            },
        )
    )

    assert response.content.data["todo"] is False
    assert service.upserts == [
        (
            "request",
            {
                "rec_name": "req-1",
                "todo": False,
                "title": "Request",
            },
            "req-1",
        )
    ]


def test_complete_step_rejects_missing_rec_name():
    service = FakeService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(complete_step_task(service, "request", "approval", {}))

    assert exc.value.status_code == 422
    assert service.upserts == []


def test_complete_step_rejects_missing_configuration():
    service = FakeService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            complete_step_task(
                service, "request", "missing", {"rec_name": "req-1"}
            )
        )

    assert exc.value.status_code == 404
    assert service.upserts == []


def test_complete_step_rejects_step_without_checkbox_component():
    service = FakeService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            complete_step_task(
                service, "request", "no_checkbox", {"rec_name": "req-1"}
            )
        )

    assert exc.value.status_code == 422
    assert service.upserts == []


def test_complete_step_rejects_checkbox_not_on_model():
    service = FakeService()

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            complete_step_task(
                service, "request", "ghost", {"rec_name": "req-1"}
            )
        )

    assert exc.value.status_code == 422
    assert service.upserts == []


def test_complete_step_rejects_non_writable_record():
    service = FakeService(editable=False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            complete_step_task(
                service, "request", "approval", {"rec_name": "req-1"}
            )
        )

    assert exc.value.status_code == 403
    assert service.upserts == []


def test_complete_step_does_not_create_a_missing_record():
    service = FakeService(load_fail=True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            complete_step_task(
                service, "request", "approval", {"rec_name": "missing"}
            )
        )

    assert exc.value.status_code == 404
    assert service.upserts == []


def test_step_endpoint():
    service = FakeService()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_authed_env] = lambda: None
    app.dependency_overrides[get_service] = lambda: service

    response = TestClient(app).post(
        "/step/request/approval",
        json={"rec_name": "req-1", "todo": True},
    )

    assert response.status_code == 200
    assert response.json()["content"]["data"]["todo"] is False
