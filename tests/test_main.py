import re
from pathlib import Path

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


def test_version_matches_config_yaml(client):
    config = Path(__file__).parent.parent / "axis_access_manager" / "config.yaml"
    version = re.search(r'^version:\s*"([^"]+)"', config.read_text(), re.MULTILINE).group(1)
    assert client.get("/api/me").json()["version"] == version


def test_readonly_users_cannot_change_anything(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre", "readonly_users": ["Ospite"]}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "r.db")
    with TestClient(app) as client:
        headers = {"X-Remote-User-Name": "ospite"}
        assert client.get("/api/me", headers=headers).json()["readonly"] is True
        assert client.get("/api/persons", headers=headers).status_code == 200
        response = client.post("/api/door/open", json={}, headers=headers)
        assert response.status_code == 403
        assert controller.calls == []
        assert client.post("/api/persons", json={"name": "A", "card": "04aa"}).status_code == 200


def test_rules_and_dashboard_endpoints(client):
    schedule = client.post(
        "/api/schedules",
        json={"name": "Ufficio", "rules": [{"days": ["MO"], "start": "08:00", "end": "12:00"}], "holidays": True},
    ).json()["token"]
    group = client.post("/api/groups", json={"name": "Dipendenti", "schedule": schedule}).json()["token"]
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "group": group}).json()
    assert client.put(f"/api/persons/{person['id']}/access", json={"group": "profilo_sempre"}).status_code == 200
    assert client.post("/api/holidays/italia", json={"year": 2026}).status_code == 200
    assert len(client.get("/api/holidays").json()) == 12

    groups = client.get("/api/groups").json()
    assert {g["name"] for g in groups["groups"]} == {"Sempre", "Dipendenti"}
    assert client.delete(f"/api/groups/{group}").status_code == 200

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["persons"] == 1 and len(dashboard["week"]) == 7
    detail = client.get(f"/api/persons/{person['id']}").json()
    assert detail["events"] == []


def test_csv_export_and_import(client):
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"})
    response = client.get("/api/export/persons.csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert "Anna;04aa;Sempre" in response.text
    preview = client.post("/api/import/persons", json={"csv": "Mario;04bb;Sempre", "apply": False}).json()
    assert preview["riepilogo"]["ok"] == 1
    assert client.get("/api/export/events.csv").status_code == 200
