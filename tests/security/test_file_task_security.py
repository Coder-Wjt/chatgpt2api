import threading
import pytest
from services.editable_file_task_service import EditableFileTaskService


def test_owner_admission_and_idempotency(tmp_path, monkeypatch):
    service = EditableFileTaskService(
        database_url="sqlite:///" + str(tmp_path / "tasks.db")
    )
    release = threading.Event()
    monkeypatch.setattr(service, "_run_task", lambda *a: release.wait(10))
    owner = {"id": "user-a"}
    try:
        first = service.submit_ppt(owner, client_task_id="one")
        service.submit_ppt(owner, client_task_id="two")
        assert service.submit_ppt(owner, client_task_id="one")["id"] == first["id"]
        with pytest.raises(RuntimeError, match="capacity"):
            service.submit_ppt(owner, client_task_id="three")
        assert service._repository.get("user-a", "three") is None
    finally:
        release.set()
        if hasattr(service, "shutdown"):
            service.shutdown()


def test_global_capacity_and_shutdown_cancellation(tmp_path, monkeypatch):
    from services.editable_file_task_service import EditableFileTaskCapacityError

    service = EditableFileTaskService(
        database_url="sqlite:///" + str(tmp_path / "tasks.db")
    )
    release = threading.Event()
    entered = threading.Event()
    active = []

    def work(key, *args):
        active.append(key)
        if len(active) == 2:
            entered.set()
        release.wait(5)

    monkeypatch.setattr(service, "_run_task", work)
    try:
        for i in range(6):
            service.submit_ppt({"id": str(i)}, client_task_id="one")
        assert entered.wait(2)
        assert len(active) == 2
        with pytest.raises(EditableFileTaskCapacityError):
            service.submit_ppt({"id": "overflow"})
        service._runner.shutdown(wait=False)
        assert service._repository.get("5", "one")["status"] == "error"
        assert "5" not in service._owner_pending
    finally:
        release.set()
        service.shutdown()
    assert not service._owner_pending


def test_failed_persistence_releases_admission(tmp_path, monkeypatch):
    service = EditableFileTaskService(
        database_url="sqlite:///" + str(tmp_path / "tasks.db")
    )

    def fail(task):
        raise OSError("fixture write failure")

    monkeypatch.setattr(service._repository, "create", fail)
    try:
        with pytest.raises(OSError):
            service.submit_ppt({"id": "a"})
        assert service._runner.status()["accepted"] == 0
        assert not service._owner_pending
    finally:
        service.shutdown()


def test_capacity_rejection_is_http_429(monkeypatch):
    import os
    from fastapi.testclient import TestClient
    from api.app import create_app
    from services.editable_file_task_service import (
        editable_file_task_service,
        EditableFileTaskCapacityError,
    )

    def full(*a, **kw):
        raise EditableFileTaskCapacityError("capacity exceeded")

    monkeypatch.setattr(editable_file_task_service, "submit_ppt", full)
    response = TestClient(create_app()).post(
        "/v1/ppt/generations",
        json={"prompt": "fixture"},
        headers={"authorization": "Bearer " + os.environ["CHATGPT2API_AUTH_KEY"]},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
