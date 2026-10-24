from fastapi.testclient import TestClient

from app.config import parse_options
from app.main import create_app

SETTINGS = parse_options({"a1210_host": "", "mqtt_topic_prefix": "accessi/x"})


def test_status_without_controller_configured(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with TestClient(create_app(SETTINGS, allow_any_client=True)) as client:
        data = client.get("/api/status").json()
    assert data["controller"] == {"ok": False, "error": "indirizzo del controller non configurato"}
    assert data["mqtt"]["configured"] is False


def test_index_is_served(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with TestClient(create_app(SETTINGS, allow_any_client=True)) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert "Controllo accessi" in response.text


def test_requests_outside_ingress_are_refused(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    with TestClient(create_app(SETTINGS, allow_any_client=False)) as client:
        assert client.get("/api/status").status_code == 403
