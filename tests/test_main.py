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
    assert "Controllo Accessi" in response.text


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


def test_door_cannot_be_opened_from_the_app(client, controller):
    # L'apertura resta in Home Assistant (pulsanti MQTT), non nell'interfaccia dell'add-on
    assert client.post("/api/door/open", json={"extended": False}).status_code in (404, 405)
    assert controller.calls == []


def test_version_matches_config_yaml(client):
    config = Path(__file__).parent.parent / "axis_access_manager" / "config.yaml"
    version = re.search(r'^version:\s*"([^"]+)"', config.read_text(), re.MULTILINE).group(1)
    assert client.get("/api/me").json()["version"] == version


def test_first_user_is_admin_and_others_get_status_only(client, controller):
    admin = {"X-Remote-User-Name": "Rising", "X-Remote-User-Display-Name": "Rising Coder"}
    guest = {"X-Remote-User-Name": "ospite", "X-Remote-User-Display-Name": "Ospite"}
    me = client.get("/api/me", headers=admin).json()
    assert me["admin"] is True and me["username"] == "rising"

    me = client.get("/api/me", headers=guest).json()
    assert me["admin"] is False and me["perms"] == []
    assert client.get("/api/status", headers=guest).status_code == 200
    assert client.get("/api/summary", headers=guest).status_code == 200
    assert client.get("/api/persons", headers=guest).status_code == 403
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=guest).status_code == 403
    assert client.get("/api/events", headers=guest).status_code == 403
    assert client.get("/api/users", headers=guest).status_code == 403
    dashboard = client.get("/api/dashboard", headers=guest).json()
    assert dashboard["recent"] == [] and dashboard["expiring"] == []

    # L'amministratore dà all'ospite il ruolo di consultazione
    users = client.get("/api/users", headers=admin).json()
    assert {u["username"]: u["role"] for u in users["users"]} == {"rising": "amministratore", "ospite": "solo_stato"}
    consult = users["roles"]["consultazione"]["perms"]
    assert client.put("/api/users/ospite", json={"perms": consult}, headers=admin).status_code == 200
    assert client.get("/api/persons", headers=guest).status_code == 200
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=guest).status_code == 403
    assert controller.calls == []

    # Permesso di modifica: implica la consultazione
    response = client.post("/api/users", json={"username": "mario", "perms": ["persone_modifica"]}, headers=admin)
    assert response.json()["perms"] == ["persone", "persone_modifica"]
    mario = {"X-Remote-User-Name": "Mario"}
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=mario).status_code == 200
    assert (
        client.post("/api/groups", json={"name": "G", "schedule": "standard_always"}, headers=mario).status_code == 403
    )

    # Sempre almeno un amministratore
    response = client.put("/api/users/rising", json={"perms": []}, headers=admin)
    assert response.status_code == 400 and "amministratore" in response.json()["detail"]
    assert client.delete("/api/users/rising", headers=admin).status_code == 400
    assert client.delete("/api/users/mario", headers=admin).status_code == 200


def test_readonly_users_option_starts_as_consultation(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre", "readonly_users": ["Ospite"]}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "r.db")
    with TestClient(app) as client:
        client.get("/api/me", headers={"X-Remote-User-Name": "admin"})
        headers = {"X-Remote-User-Name": "ospite"}
        assert client.get("/api/persons", headers=headers).status_code == 200
        assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=headers).status_code == 403
        assert controller.calls == []


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


def test_categories_hidden_and_default_group_endpoints(client):
    group = client.post("/api/groups", json={"name": "Pulizie", "schedule": "standard_office_hours"}).json()["token"]
    category = client.post("/api/categories", json={"name": "Pulizie", "group_token": group, "color": "teal"}).json()
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "category_id": category["id"]}).json()
    assert person["cards"][0]["access_profile"] == group
    assert client.get("/api/categories").json()[0]["persons"] == 1
    assert "Pulizie" in client.get("/api/export/persons.csv").text.splitlines()[1]

    assert (
        client.put("/api/hidden", json={"kind": "schedule", "token": "standard_always", "hidden": True}).status_code
        == 200
    )
    schedules = {s["token"]: s for s in client.get("/api/schedules").json()}
    assert schedules["standard_always"]["hidden"] is True
    assert client.put("/api/default-group", json={"token": group}).status_code == 200
    assert client.get("/api/me").json()["default_group"] == group
    assert client.delete(f"/api/categories/{category['id']}").status_code == 200
