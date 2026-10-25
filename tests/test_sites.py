import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.installation import DEFAULT_AREA, DEFAULT_SITE, InstallationService
from app.main import create_app
from app.service import ServiceError
from app.transfer import events_csv

CONTROLLER = {"name": "A1210", "host": "10.0.0.5", "username": "root", "password": "x", "mqtt_topic_prefix": "a/b"}


def _old_door(db, controller_id, area_id=0):
    return db.save_door(
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
            "area_id": area_id,
        },
    )


def test_existing_doors_get_a_default_site_once(db):
    # Impianto della 0.5: controller e porta senza struttura
    controller_id = db.save_controller(None, {**CONTROLLER, "verify_ssl": 0})
    door_id = _old_door(db, controller_id)
    installation = InstallationService(db)
    assert installation.ensure_default_site() is True
    assert installation.ensure_default_site() is False
    [site] = installation.locations()
    area = site["areas"][0]
    assert site["name"] == DEFAULT_SITE and area["name"] == DEFAULT_AREA
    # L'area passa al controller e la porta la eredita
    assert area["controllers"] == [controller_id] and area["doors"] == [door_id]
    assert db.get_controller(controller_id)["area_id"] == area["id"] and db.get_door(door_id)["area_id"] == 0


def test_door_area_moves_to_its_controller(db):
    # Impianto della 0.6: l'area era sulla porta
    installation = InstallationService(db)
    teatro = installation.save_site(None, {"name": "Teatro Eliseo"}, "admin")["id"]
    consulta = installation.save_area(None, teatro, {"name": "Via Consulta"}, "admin")["id"]
    controller_id = db.save_controller(None, {**CONTROLLER, "verify_ssl": 0})
    door_id = _old_door(db, controller_id, consulta)
    assert installation.ensure_default_site() is True
    assert db.get_controller(controller_id)["area_id"] == consulta
    assert db.get_door(door_id)["area_id"] == 0
    assert installation.door_places()[door_id]["area"] == "Via Consulta"
    assert [s["name"] for s in db.list_sites()] == ["Teatro Eliseo"]


def test_sites_areas_and_door_places(db):
    installation = InstallationService(db)
    teatro = installation.save_site(None, {"name": "Teatro"}, "admin")["id"]
    scala = installation.save_area(None, teatro, {"name": "Accesso Scala"}, "admin")["id"]
    with pytest.raises(ServiceError, match="esiste già una struttura"):
        installation.save_site(None, {"name": "teatro"}, "admin")
    with pytest.raises(ServiceError, match="esiste già l'area"):
        installation.save_area(None, teatro, {"name": "Accesso Scala"}, "admin")

    # Un controller nuovo senza area va nell'unica area che c'è, e le sue porte con lui
    controller_id = installation.save_controller(None, CONTROLLER, "admin")["id"]
    assert db.get_controller(controller_id)["area_id"] == scala
    door_id = installation.save_door(None, controller_id, {"door_token": "porta", "name": "scala"}, "admin")["id"]
    assert db.get_door(door_id)["area_id"] == 0
    assert installation.door_places()[door_id] == {
        "site": "Teatro",
        "area": "Accesso Scala",
        "site_id": teatro,
        "area_id": scala,
    }
    assert installation.controller_places()[controller_id]["area"] == "Accesso Scala"

    # Spostando il controller si spostano le porte; una porta può avere un'area sua
    hotel = installation.save_site(None, {"name": "Hotel Giulia"}, "admin")["id"]
    piano = installation.save_area(None, hotel, {"name": "Primo Piano"}, "admin")["id"]
    installation.save_controller(controller_id, {**CONTROLLER, "password": "", "area_id": piano}, "admin")
    assert installation.door_places()[door_id]["site"] == "Hotel Giulia"
    installation.save_door(door_id, controller_id, {"door_token": "porta", "name": "Scala", "area_id": scala}, "admin")
    assert installation.door_places()[door_id]["area"] == "Accesso Scala"
    # Modificare la porta senza indicare l'area la lascia dov'è
    installation.save_door(door_id, controller_id, {"door_token": "porta", "name": "Scala", "area_id": None}, "admin")
    assert db.get_door(door_id)["area_id"] == scala
    with pytest.raises(ServiceError, match="area non trovata"):
        installation.save_door(door_id, controller_id, {"door_token": "porta", "name": "Scala", "area_id": 99}, "admin")
    with pytest.raises(ServiceError, match="area non trovata"):
        installation.save_controller(controller_id, {**CONTROLLER, "area_id": 99}, "admin")

    # Eliminando l'area la porta torna a quella del controller; eliminando la struttura restano senza area
    installation.delete_area(scala, "admin")
    assert db.get_door(door_id)["area_id"] == 0
    assert installation.door_places()[door_id]["area"] == "Primo Piano"
    installation.delete_site(hotel, "admin")
    assert db.get_controller(controller_id)["area_id"] == 0
    loose = installation.locations()[-1]
    assert loose["id"] == 0 and loose["areas"][0]["doors"] == [door_id]
    assert loose["areas"][0]["controllers"] == [controller_id]
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
