import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.main import create_app
from app.notifications import NotificationService
from app.programming import ProgrammingService
from app.service import ServiceError

OUT = {
    "token": "ap-out",
    "Entity": "porta",
    "EntityType": "axtdc:Door",
    "Name": "Uscita",
    "Enabled": True,
    "IdPointDevice": [],
    "Attribute": [{"Name": "Direction", "Value": "out"}],
}


def _links(controller):
    return {
        (i["IdPoint"], p["Entity"], p["Attribute"][0]["Value"])
        for p in controller.access_points
        for i in p["IdPointDevice"]
    }


async def test_links_move_readers_between_access_points(db, controller):
    controller.access_points[0]["IdPointDevice"] = [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-a1210"}]
    controller.access_points.append(dict(OUT))
    service = ProgrammingService(db, controller, 1, asyncio.Lock())

    # Niente cambia: nessuna scrittura
    assert await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "in"}], "admin") == 0
    assert controller.calls == []

    # Lettore interno collegato in uscita: prende il dispositivo degli altri lettori
    links = [
        {"reader": "lettore-1", "door": "porta", "direction": "in"},
        {"reader": "lettore-2", "door": "porta", "direction": "out"},
    ]
    assert await service.set_links(links, "admin") == 1
    assert _links(controller) == {("lettore-1", "porta", "in"), ("lettore-2", "porta", "out")}
    out = next(p for p in controller.access_points if p["token"] == "ap-out")
    assert out["IdPointDevice"] == [{"IdPoint": "lettore-2", "DeviceUUID": "uuid-a1210"}]

    # Spostare un lettore: prima il punto che lo riceve, poi quello che lo perde
    controller.calls.clear()
    moved = [{"reader": "lettore-1", "door": "porta", "direction": "out"}]
    assert await service.set_links(moved, "admin") == 2
    assert [c[1] for c in controller.calls] == ["ap-out", "ap-in"]
    assert _links(controller) == {("lettore-1", "porta", "out")}

    # Disattivare un collegamento disattiva il suo punto di accesso
    await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "out", "enabled": False}], "admin")
    assert next(p for p in controller.access_points if p["token"] == "ap-out")["Enabled"] is False

    with pytest.raises(ServiceError, match="non ha un punto di accesso"):
        await service.set_links([{"reader": "lettore-1", "door": "porta2", "direction": "in"}], "admin")
    with pytest.raises(ServiceError, match="lettore non trovato"):
        await service.set_links([{"reader": "manca", "door": "porta", "direction": "in"}], "admin")

    # Ogni punto cambiato è nello storico e si può annullare
    changes = db.list_controller_changes(1)
    assert len(changes) == 4
    await service.undo(changes[0]["id"], "admin")
    assert next(p for p in controller.access_points if p["token"] == "ap-out")["Enabled"] is True
    assert not any(c[0] == "access_door" for c in controller.calls)


async def test_relinked_reader_keeps_its_device(db, controller):
    """Scollegato l'unico lettore, il dispositivo non è più sul controller: si riprende dallo storico."""
    controller.access_points[0]["IdPointDevice"] = [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-a1210"}]
    service = ProgrammingService(db, controller, 1, asyncio.Lock())
    assert await service.set_links([], "admin") == 1
    assert controller.access_points[0]["IdPointDevice"] == []
    await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "in"}], "admin")
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-a1210"}]

    # Ricollegato da una versione precedente senza dispositivo: il primo salvataggio lo ripara
    controller.access_points[0]["IdPointDevice"] = [{"IdPoint": "lettore-1", "DeviceUUID": ""}]
    assert await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "in"}], "admin") == 1
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-a1210"}]

    # Stesso ripristino dalla scheda del punto di accesso
    controller.access_points[0]["IdPointDevice"] = []
    await service.update_access_point("ap-in", {"readers": ["lettore-1"]}, "admin")
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-a1210"}]


def _collect(service, **overrides):
    values = {
        "mqtt_configured": True,
        "mqtt_connected": True,
        "controller_configured": True,
        "controller_online": True,
        "sync": {"ok": True},
        "door_names": {"porta": "Scala"},
    }
    return service.collect(**{**values, **overrides})


def test_notifications(db):
    service = NotificationService(db)
    now = datetime.now(tz=UTC)
    assert _collect(service) == []

    db.add_event(ts=(now - timedelta(hours=1)).isoformat(), kind="allarme", door="porta", detail="porta forzata")
    db.add_event(ts=(now - timedelta(hours=30)).isoformat(), kind="allarme", door="porta")
    db.add_event(ts=now.isoformat(), kind="negato", person_name="Luca", door="porta")
    db.note_unknown_card("04AA", now.isoformat())
    items = _collect(service, mqtt_connected=False, controller_online=False)
    assert {i["type"] for i in items} == {"door_alarm", "mqtt", "controller", "unknown_card"}
    alarm = next(i for i in items if i["type"] == "door_alarm")
    assert alarm["level"] == "urgent" and alarm["text"] == "Scala · porta forzata"

    listing = service.listing(items)
    assert listing["urgent"] == 3
    # Chi non vede il registro né le persone vede solo i collegamenti
    assert {i["type"] for i in service.listing(items, set())["items"]} == {"mqtt", "controller"}

    service.mark_seen(["mqtt", alarm["id"]], items)
    assert service.listing(items)["urgent"] == 1
    # Il collegamento torna: la notifica sparisce e, se si ripresenta, va rivista
    items = _collect(service, controller_online=False)
    assert service.listing(items)["urgent"] == 1
    items = _collect(service, mqtt_connected=False, controller_online=False)
    assert service.listing(items)["urgent"] == 2

    service.set_levels({"denied": "info", "door_alarm": "off"}, "admin")
    types = {i["type"] for i in _collect(service)}
    assert "denied" in types and "door_alarm" not in types
    with pytest.raises(ServiceError, match="livello non valido"):
        service.set_levels({"denied": "forte"}, "admin")


@pytest.fixture
def client(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "a1210.local", "door_token": "porta", "mqtt_topic_prefix": "accessi/x"})
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "page.db")
    with TestClient(app) as test_client:
        yield test_client


def test_controller_page_through_api(client, controller):
    [ctrl] = client.get("/api/installation").json()["controllers"]
    assert ctrl["place"]["area"] and ctrl["description"] == ""
    body = {"links": [{"reader": "lettore-2", "door": "porta", "direction": "in"}]}
    assert client.put("/api/programming/links", json=body).json() == {"changed": 1}
    assert controller.access_points[0]["IdPointDevice"][0]["IdPoint"] == "lettore-2"

    listing = client.get("/api/notifications").json()
    assert {t["key"] for t in listing["types"]} >= {"door_alarm", "controller", "mqtt"}
    assert client.get("/api/summary").json()["notifications"] == listing["urgent"]
    assert client.post("/api/notifications/seen", json={}).json() == {"urgent": 0}
    types = client.put("/api/notifications/settings", json={"denied": "urgent"}).json()["types"]
    assert next(t for t in types if t["key"] == "denied")["level"] == "urgent"
    assert client.put("/api/notifications/settings", json={"x": "urgent"}).status_code == 400


def test_status_without_controller(client, controller):
    status = client.get("/api/status", params={"controller": "false"}).json()
    assert status["controller"] is None and "home_assistant" in status
    assert client.get("/api/status").json()["controller"]["ok"] is True
