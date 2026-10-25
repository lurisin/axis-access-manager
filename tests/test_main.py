import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.main import create_app


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as test_client:
        yield test_client


def test_status_without_controller_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "", "mqtt_topic_prefix": "accessi/x"})
    with TestClient(create_app(settings, allow_any_client=True, db_path=tmp_path / "a.db")) as client:
        data = client.get("/api/status").json()
    assert data["controller"] == {"ok": False, "error": "indirizzo del controller non configurato"}
    assert data["mqtt"]["configured"] is False


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Controllo accessi" in response.text


def test_requests_outside_ingress_are_refused(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=False, controller=controller, db_path=tmp_path / "b.db")
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 403


def test_startup_imports_existing_cards(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "c.db")
    with TestClient(app) as client:
        client.post("/api/sync")
        persons = client.get("/api/persons").json()
    assert persons[0]["name"] == "Mario Rossi"


def test_person_lifecycle_through_api(client, controller):
    headers = {"X-Remote-User-Display-Name": "Rising Coder"}
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=headers).json()
    card_id = person["cards"][0]["id"]

    assert client.patch(f"/api/persons/{person['id']}", json={"name": "Anna B"}).json()["name"] == "Anna B"
    assert client.patch(f"/api/cards/{card_id}", json={"enabled": False}).json()["cards"][0]["enabled"] is False
    assert client.delete(f"/api/cards/{card_id}").json() == {"ok": True, "person": None}
    assert client.get("/api/persons").json() == []


def test_errors_are_reported_in_italian(client):
    response = client.post("/api/persons", json={"name": "Anna", "card": "non-hex"})
    assert response.status_code == 400
    assert "numero tessera non valido" in response.json()["detail"]


def test_door_open(client, controller):
    assert client.post("/api/door/open", json={"extended": False}).json() == {"ok": True}
    assert ("access_door", "porta", False) in controller.calls
    events = client.get("/api/events").json()
    assert events[0]["kind"] == "apertura_remota"
