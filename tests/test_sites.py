import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.installation import DEFAULT_AREA, DEFAULT_SITE, InstallationService
from app.main import create_app
from app.service import ServiceError
from app.transfer import events_csv

CONTROLLER = {"name": "A1210", "host": "10.0.0.5", "username": "root", "password": "x", "mqtt_topic_prefix": "a/b"}


def test_existing_doors_get_a_default_site_once(db):
    installation = InstallationService(db)
    controller_id = installation.save_controller(None, CONTROLLER, "admin")["id"]
    db.save_door(
        None,
        controller_id,
        {
            **dict.fromkeys(db.DOOR_FIELDS, ""),
            "door_token": "porta",
            "name": "Scala",
            "snapshot_on_granted": 0,
            "ha_buttons": 1,
            "intercom_open_door": 0,
            "intercom_relay": 1,
            "area_id": 0,
        },
    )
    assert installation.ensure_default_site() is True
    assert installation.ensure_default_site() is False
    [site] = installation.locations()
    assert site["name"] == DEFAULT_SITE and site["areas"][0]["name"] == DEFAULT_AREA
    assert site["areas"][0]["doors"] == [db.list_doors()[0]["id"]]


def test_sites_areas_and_door_places(db):
    installation = InstallationService(db)
    controller_id = installation.save_controller(None, CONTROLLER, "admin")["id"]
    teatro = installation.save_site(None, {"name": "Teatro"}, "admin")["id"]
    scala = installation.save_area(None, teatro, {"name": "Accesso Scala"}, "admin")["id"]
    with pytest.raises(ServiceError, match="esiste già una struttura"):
        installation.save_site(None, {"name": "teatro"}, "admin")
    with pytest.raises(ServiceError, match="esiste già l'area"):
        installation.save_area(None, teatro, {"name": "Accesso Scala"}, "admin")

    # Una porta nuova senza area va nell'unica area che c'è
    door_id = installation.save_door(None, controller_id, {"door_token": "porta", "name": "scala"}, "admin")["id"]
    assert db.get_door(door_id)["area_id"] == scala
    assert installation.door_places()[door_id] == {
        "site": "Teatro",
        "area": "Accesso Scala",
        "site_id": teatro,
        "area_id": scala,
    }
    # Modificare la porta senza indicare l'area la lascia dov'è
    installation.save_door(door_id, controller_id, {"door_token": "porta", "name": "Scala", "area_id": None}, "admin")
    assert db.get_door(door_id)["area_id"] == scala
    with pytest.raises(ServiceError, match="area non trovata"):
        installation.save_door(door_id, controller_id, {"door_token": "porta", "name": "Scala", "area_id": 99}, "admin")

    # Eliminando l'area la porta resta, senza area
    installation.delete_area(scala, "admin")
    assert db.get_door(door_id)["area_id"] == 0
    loose = installation.locations()[-1]
    assert loose["id"] == 0 and loose["areas"][0]["doors"] == [door_id]
    installation.delete_site(teatro, "admin")
    assert db.list_sites() == []


def test_event_filters_by_door(db):
    db.add_event(ts="2026-10-25T08:00:00+00:00", kind="concesso", person_name="Anna", door="scala")
    db.add_event(ts="2026-10-25T08:01:00+00:00", kind="negato", person_name="Luca", door="hotel")
    db.add_event(ts="2026-10-25T08:02:00+00:00", kind="concesso", person_name="Vecchio")
    assert [e["person_name"] for e in db.list_events(door="scala")] == ["Anna"]
    assert [e["person_name"] for e in db.list_events(door="scala,")] == ["Vecchio", "Anna"]
    assert db.count_matching_events(kind="concesso") == 2
    assert db.door_event_counts("2026-10-25T00:00:00+00:00")["hotel"] == {"negato": 1}
    assert db.last_door_events(("concesso",))["scala"]["person_name"] == "Anna"

    rows = db.list_events()
    assert "Porta" not in events_csv(rows, {"scala": "Scala"}).splitlines()[0]
    two = events_csv(rows, {"scala": "Accesso Scala", "hotel": "Ingresso Hotel"})
    assert "Porta" in two.splitlines()[0] and "Ingresso Hotel" in two


@pytest.fixture
def client(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "a1210.local", "door_token": "porta", "mqtt_topic_prefix": "accessi/x"})
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "sites.db")
    with TestClient(app) as test_client:
        yield test_client


def test_door_pages_through_api(client):
    # L'impianto importato dalle vecchie opzioni finisce nella struttura predefinita
    [site] = client.get("/api/locations").json()
    assert site["name"] == DEFAULT_SITE
    site_id, area_id = site["id"], site["areas"][0]["id"]
    assert client.put(f"/api/sites/{site_id}", json={"name": "Teatro"}).json() == {"id": site_id}
    assert client.put(f"/api/areas/{area_id}", json={"name": "Scala"}).json() == {"id": area_id}

    [door] = client.get("/api/door-overview").json()
    assert door["site"] == "Teatro" and door["area"] == "Scala"
    assert door["today"] == {"granted": 0, "denied": 0, "alarms": 0}

    detail = client.get(f"/api/door-detail/{door['id']}").json()
    assert detail["name"] == door["name"] and isinstance(detail["readers"], list)
    assert detail["events"] == [] and detail["groups"] is not None
    assert client.get("/api/door-detail/999").status_code == 404

    summary = client.get("/api/summary").json()
    assert summary["doors"][0]["site"] == "Teatro"
    assert client.get("/api/events/count", params={"kind": "concesso"}).json() == {"count": 0}

    hotel = client.post("/api/sites", json={"name": "Hotel"}).json()["id"]
    assert client.post(f"/api/sites/{hotel}/areas", json={"name": "Reception"}).status_code == 200
    assert client.delete(f"/api/sites/{hotel}").json() == {"ok": True}
