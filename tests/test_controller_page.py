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
    db.add_event(ts=now.isoformat(), kind="negato", person_name="Luca", door="porta", detail="fuori fascia oraria")
    db.add_event(ts=now.isoformat(), kind="negato", card="04AA", door="porta", detail="tessera non registrata")
    items = _collect(service, mqtt_connected=False, controller_online=False)
    assert {i["type"] for i in items[:3]} == {"door_alarm", "mqtt", "controller"}
    # Dalla più grave: allarme e collegamenti, poi la tessera sconosciuta, infine il singolo negato
    assert [i["severity"] for i in items] == ["crit", "crit", "crit", "warn", "low"]
    assert [i["type"] for i in items][3:] == ["unknown_card", "denied"]
    alarm = next(i for i in items if i["type"] == "door_alarm")
    assert alarm["title"] == "Allarme Porta" and alarm["text"] == "Porta forzata" and alarm["door"] == "Scala"
    denied = items[-1]
    assert denied["text"] == "Luca · fuori fascia oraria" and denied["count"] == 1
    assert items[3]["text"] == "Tessera 04AA" and items[3]["link"] == "#da-registrare"

    listing = service.listing(items)
    assert listing["open"] == 5
    # Chi non vede il registro né le persone vede solo i collegamenti
    assert {i["type"] for i in service.listing(items, set())["items"]} == {"mqtt", "controller"}

    service.mark_seen(["mqtt", alarm["id"]], items)
    assert service.listing(items)["open"] == 3
    # Il collegamento torna: la notifica sparisce e, se si ripresenta, va rivista
    items = _collect(service, controller_online=False)
    assert service.listing(items)["open"] == 3
    items = _collect(service, mqtt_connected=False, controller_online=False)
    assert service.listing(items)["open"] == 4

    service.set_levels({"denied": "off", "door_alarm": "off"}, "admin")
    types = {i["type"] for i in _collect(service)}
    assert "denied" not in types and "door_alarm" not in types and "unknown_card" in types
    with pytest.raises(ServiceError, match="livello non valido"):
        service.set_levels({"denied": "info"}, "admin")


def test_denied_without_card_is_not_an_unknown_card(db):
    # Collaudo 0.21.3: il pulsante negato fuori orario compariva come "Tessera Sconosciuta · Tessera non registrata"
    service = NotificationService(db)
    now = datetime.now(tz=UTC).isoformat()
    db.add_event(ts=now, kind="negato", source="controller", door="porta", detail="fuori fascia oraria")
    db.add_event(
        ts=now, kind="negato", source="Pulsante Portineria", door="porta", detail="Pulsante Uscita · Aux 1 · Boh"
    )
    items = _collect(service)
    assert [(i["type"], i["title"], i["severity"]) for i in items] == [
        ("denied", "Accesso Negato", "low"),
        ("denied", "Accesso Negato", "low"),
    ]
    assert {i["text"] for i in items} == {"fuori fascia oraria", "Pulsante Portineria · Pulsante Uscita · Aux 1 · Boh"}


def test_stuck_input_is_a_fault(db):
    service = NotificationService(db)
    stuck = {"controller_id": 1, "io": "IO1", "name": "Pulsante Portineria", "since": "2026-10-26T12:18:30+00:00"}
    [item] = _collect(service, stuck_inputs=[{**stuck, "seconds": 600}])
    assert item["title"] == "Ingresso Bloccato" and item["severity"] == "warn" and item["link"] == "#impianti/c1"
    assert item["text"].startswith("Pulsante Portineria risulta premuto da 10 minuti")
    assert item["text"].endswith("per i pulsanti usa l'ingresso REX della porta")
    [item] = _collect(service, stuck_inputs=[{**stuck, "io": "REX", "seconds": 7300}])
    assert "da 2 ore" in item["text"] and "ingresso REX" not in item["text"]


def test_notifications_group_repeated_events(db):
    service = NotificationService(db)
    now = datetime.now(tz=UTC)

    def denied(minutes_ago, **fields):
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        return db.add_event(ts=ts, kind="negato", door="porta", detail="fuori fascia oraria", **fields)

    # Tre tentativi della stessa persona a 2 minuti l'uno dall'altro: una notifica media con il conteggio
    for minutes in (20, 18, 16):
        denied(minutes, person_id=1, person_name="Ciccio Bello")
    # Un quarto tentativo un'ora prima: troppo lontano, è un'altra notifica
    denied(80, person_id=1, person_name="Ciccio Bello")
    # Tessera sconosciuta ripetuta: critica
    denied(5, card="c0110a01")
    denied(4, card="c0110a01")
    items = _collect(service)
    assert [(i["type"], i["severity"], i["count"]) for i in items] == [
        ("unknown_card", "crit", 2),
        ("denied", "warn", 3),
        ("denied", "low", 1),
    ]
    group = items[1]
    assert group["title"] == "Accesso Negato ×3" and len(group["events"]) == 3 and group["event"] == group["events"][-1]
    assert group["first_ts"] < group["ts"]
    # Presa visione, poi un nuovo tentativo nella stessa finestra: la notifica torna da gestire
    service.mark_seen([group["id"]], items)
    assert service.listing(_collect(service))["open"] == 2
    denied(15, person_id=1, person_name="Ciccio Bello")
    listing = service.listing(_collect(service))
    assert listing["open"] == 3
    assert next(i for i in listing["items"] if i["count"] == 4)["seen"] is False


def test_old_info_level_counts_as_active(db):
    db.set_setting("notifications", '{"unknown_card": "info", "denied": "off"}')
    levels = NotificationService(db).levels()
    assert levels["unknown_card"] == "urgent" and levels["denied"] == "off"


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
    assert client.get("/api/summary").json()["notifications"] == listing["open"]
    assert client.post("/api/notifications/seen", json={}).json() == {"open": 0}
    types = client.put("/api/notifications/settings", json={"denied": "urgent"}).json()["types"]
    assert next(t for t in types if t["key"] == "denied")["level"] == "urgent"
    assert client.put("/api/notifications/settings", json={"x": "urgent"}).status_code == 400


def test_status_without_controller(client, controller):
    status = client.get("/api/status", params={"controller": "false"}).json()
    assert status["controller"] is None and "home_assistant" in status
    assert client.get("/api/status").json()["controller"]["ok"] is True


async def test_relinked_reader_device_from_controller(db, controller):
    """Senza storico: il dispositivo si legge dalla configurazione del lettore o dalla porta."""
    point = controller.access_points[0]
    point["DoorDeviceUUID"] = "uuid-porta"
    point["IdPointDevice"] = [{"IdPoint": "lettore-1", "DeviceUUID": ""}]
    service = ProgrammingService(db, controller, 1, asyncio.Lock())
    assert await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "in"}], "admin") == 1
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-1", "DeviceUUID": "uuid-porta"}]

    async def configs():
        return [{"token": "lettore-2", "DeviceUUID": "uuid-lettore", "Configuration": []}]

    controller.list_idpoint_configurations = configs
    await service.set_links([{"reader": "lettore-2", "door": "porta", "direction": "in"}], "admin")
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-2", "DeviceUUID": "uuid-lettore"}]


def test_dashboard_layout(client):
    assert client.get("/api/dashboard/layout").json()["widgets"] is None
    layout = [{"type": "porte", "size": 4, "height": 3}, {"type": "negati_oggi", "size": 1}]
    assert client.put("/api/dashboard/layout", json={"widgets": layout}).json() == {"widgets": layout}
    assert client.get("/api/dashboard/layout").json()["widgets"] == layout
    # Riquadri sconosciuti, ripetuti o con larghezza o altezza fuori misura non si salvano
    assert client.put("/api/dashboard/layout", json={"widgets": [{"type": "meteo", "size": 1}]}).status_code == 400
    twice = [{"type": "porte", "size": 1}, {"type": "porte", "size": 2}]
    assert client.put("/api/dashboard/layout", json={"widgets": twice}).status_code == 400
    assert client.put("/api/dashboard/layout", json={"widgets": [{"type": "porte", "size": 5}]}).status_code == 422
    tall = [{"type": "porte", "size": 2, "height": 13}]
    assert client.put("/api/dashboard/layout", json={"widgets": tall}).status_code == 422
    assert client.get("/api/dashboard/layout").json()["widgets"] == layout
    # Ogni utente ha il suo cruscotto
    other = client.get("/api/dashboard/layout", headers={"X-Remote-User-Name": "mario"}).json()
    assert other["widgets"] is None
    assert client.delete("/api/dashboard/layout").json() == {"widgets": None}
    assert client.get("/api/dashboard/layout").json()["widgets"] is None
    assert "system" in client.get("/api/dashboard").json()


def test_seen_notifications_go_in_the_audit_log(client):
    db = client.app.state.db
    now = datetime.now(tz=UTC)
    for minutes in (3, 2):
        db.add_event(ts=(now - timedelta(minutes=minutes)).isoformat(), kind="negato", card="04AA", door="porta")
    db.add_event(ts=now.isoformat(), kind="allarme", door="porta", detail="porta forzata")
    items = client.get("/api/notifications").json()["items"]
    unknown = next(i for i in items if i["type"] == "unknown_card")
    client.post("/api/notifications/seen", json={"ids": [unknown["id"]]})
    [last] = db.list_audit(limit=1)
    assert last["action"] == "presa visione" and last["detail"] == "Tessera Sconosciuta ×2 (Tessera 04AA)"
    client.post("/api/notifications/seen", json={})
    [last] = db.list_audit(limit=1)
    assert last["action"] == "presa visione" and last["detail"] == "Allarme Porta (Porta forzata)"
    # Già viste: nessuna nuova riga
    count = len(db.list_audit())
    client.post("/api/notifications/seen", json={})
    assert len(db.list_audit()) == count


def test_controller_events_follow_the_controller_name(db):
    db.add_event(ts="2026-10-25T10:00:00Z", kind="manomissione", detail="coperchio", source="Vecchio Nome")
    db.add_event(ts="2026-10-25T10:01:00Z", kind="manomissione", detail="manomissione porta", source="porta", door="p")
    assert db.set_controller_event_source("Rack Portineria", old="Altro") == 0
    assert db.set_controller_event_source("Rack Portineria", old="Vecchio Nome") == 1
    events = {e["detail"]: e["source"] for e in db.list_events()}
    assert events == {"coperchio": "Rack Portineria", "manomissione porta": "porta"}


def test_stuck_inputs_are_buttons_active_for_over_a_minute(db):
    from types import SimpleNamespace

    from app.events import EventMonitor
    from app.main import _stuck_inputs

    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    db.save_controller_input(controller_id, "IO1", {"name": "Pulsante Portineria", "role": "rex"})
    db.save_controller_input(controller_id, "IO2", {"name": "Allagamento", "role": "alarm"})
    monitor = EventMonitor(prefix="x")
    pressed = datetime(2026, 10, 26, 12, 18, 30, tzinfo=UTC)
    monitor.io_ports.update({"0": True, "1": True})
    monitor.io_since.update({"0": pressed, "1": pressed})
    state = SimpleNamespace(installation=SimpleNamespace(primary={"id": controller_id}), db=db, monitor=monitor)

    # Premuto da pochi secondi: normale; un ingresso di allarme attivo non è un pulsante bloccato
    assert _stuck_inputs(state, pressed + timedelta(seconds=30)) == []
    [stuck] = _stuck_inputs(state, pressed + timedelta(minutes=5))
    assert stuck == {
        "controller_id": controller_id,
        "io": "IO1",
        "name": "Pulsante Portineria",
        "since": "2026-10-26T12:18:30+00:00",
        "seconds": 300.0,
    }
    monitor.io_ports["0"] = False
    assert _stuck_inputs(state, pressed + timedelta(minutes=5)) == []


async def test_door_contact_is_read_from_the_controller_after_each_connection(controller):
    from types import SimpleNamespace

    from app.events import EventMonitor
    from app.main import _read_door_states

    monitor = EventMonitor(prefix="x")
    monitor.connected, monitor.controller_online = True, True
    changes = []
    state = SimpleNamespace(
        installation=SimpleNamespace(primary={"id": 1}, doors=lambda: [{"door_token": "porta"}]),
        health=SimpleNamespace(states={1: SimpleNamespace(online=True)}),
        controller=SimpleNamespace(paused="", door_state=controller.door_state),
        mqtt=object(),
        monitor=monitor,
        service=SimpleNamespace(notify_change=lambda: changes.append(1)),
        door_states_key=None,
    )

    # Al banco Contatto Porta restava sconosciuto: il contatto arriva via MQTT solo quando cambia
    await _read_door_states(state)
    assert monitor.door_physical == {"porta": "Closed"}
    assert monitor.door_modes == {"porta": "Locked"}
    assert changes == [1]

    # Una volta per connessione; dopo un'interruzione si rilegge, e la modalità arrivata via MQTT resta
    controller.door_physical, controller.door = "Open", "Accessed"
    await _read_door_states(state)
    assert monitor.door_physical == {"porta": "Closed"}
    monitor.outages += 1
    await _read_door_states(state)
    assert monitor.door_physical == {"porta": "Open"}
    assert monitor.door_modes == {"porta": "Locked"}

    # Contatto tolto: il controller risponde Unknown, l'entità torna sconosciuta
    controller.door_physical = "Unknown"
    await _read_door_states(state, force=True)
    assert monitor.door_physical == {"porta": "Unknown"}
