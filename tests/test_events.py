import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import parse_options
from app.events import Event, EventMonitor, parse_event
from app.ha import HomeAssistant
from app.main import create_app

PREFIX = "accessi/via_consulta_controller"


def payload(data: dict, timestamp: int = 1729800000000) -> bytes:
    return json.dumps({"timestamp": timestamp, "message": {"data": data}}).encode()


def test_parse_event_strips_prefix():
    event = parse_event(PREFIX, f"{PREFIX}/eventi/IdPoint/Request/IdData/$source/IdPoint/x", payload({"Card": "04a1"}))
    assert event.category == "IdPoint"
    assert event.topic == "IdPoint/Request/IdData/$source/IdPoint/x"
    # Le sorgenti nel topic diventano dati come gli altri
    assert event.data == {"Card": "04a1", "IdPoint": "x"}
    assert event.timestamp.year == 2024


def test_parse_event_takes_access_point_and_sources_from_topic_and_message():
    # Come al banco: l'AccessGranted del pulsante ha il punto di accesso solo nel topic, senza dati
    token = "Axis-b8a44fe6a074:1792938186.362930000"
    topic = f"{PREFIX}/eventi/AccessControl/AccessGranted/Anonymous/$source/AccessPointToken/{token}"
    assert parse_event(PREFIX, topic, payload({})).data == {"AccessPointToken": token}
    # Con la sorgente anche nel messaggio, e i dati che vincono sui doppioni
    body = {"message": {"source": {"DoorToken": "porta"}, "key": {}, "data": {"State": "Open"}}}
    event = parse_event(PREFIX, f"{PREFIX}/eventi/Door/State/DoorPhysicalState", json.dumps(body).encode())
    assert event.data == {"DoorToken": "porta", "State": "Open"}
    tilt = parse_event(PREFIX, f"{PREFIX}/eventi/Device/Tampering/TiltDetected", b'{"message": {}}')
    assert tilt.data == {} and tilt.category == "Device"


def test_monitor_tracks_door_contact():
    monitor = EventMonitor(prefix=PREFIX, door_token="porta")
    topic = f"{PREFIX}/eventi/Door/State/DoorPhysicalState/$source/DoorToken/porta"
    monitor.handle(topic, payload({"State": "Open"}))
    assert monitor.door_physical == {"porta": "Open"}
    monitor.handle(topic, payload({"State": "Closed"}))
    assert monitor.door_physical == {"porta": "Closed"}


def test_pushbutton_aux_is_read_the_other_way_round_and_not_before_a_change():
    monitor = EventMonitor(prefix=PREFIX, door_token="porta")
    aux = f"{PREFIX}/eventi/Device/IO/Port/$source/port/0"
    classic = f"{PREFIX}/eventi/Device/IO/Port/$source/port/3"
    # Ogni evento con la sua ora, come dal controller: due eventi uguali alla stessa ora sono lo stesso evento
    clock = iter(range(1729800000000, 1729800100000, 100))
    # Collaudo 26/10, IO1 come REX (inout pu/gnd): all'avvio 0 fino alla prima pressione, che dà solo 0→1
    monitor.handle(aux, payload({"state": "0"}, next(clock)))
    monitor.handle(classic, payload({"state": "0"}, next(clock)))
    assert monitor.input_active("0", pushbutton=True) is False
    assert monitor.input_active("3") is False
    monitor.handle(aux, payload({"state": "1"}, next(clock)))
    assert monitor.input_active("0", pushbutton=True) is False
    # Poi 1 a riposo e 0 mentre si preme; un ingresso classico resta 0 = riposo
    monitor.handle(aux, payload({"state": "0"}, next(clock)))
    assert monitor.input_active("0", pushbutton=True) is True
    monitor.handle(aux, payload({"state": "1"}, next(clock)))
    assert monitor.input_active("0", pushbutton=True) is False
    monitor.handle(classic, payload({"state": "1"}, next(clock)))
    assert monitor.input_active("3") is True
    assert monitor.input_active(None) is None and monitor.input_active("1", pushbutton=True) is None

    # Controller riavviato: si riparte dal primo valore, che non conta
    monitor.handle(f"{PREFIX}/stato", b"offline")
    assert monitor.io_ports == {} and monitor.io_changed == set()
    monitor.handle(f"{PREFIX}/stato", b"online")
    monitor.handle(aux, payload({"state": "0"}, next(clock)))
    assert monitor.input_active("0", pushbutton=True) is False


def test_monitor_remembers_since_when_the_controller_is_off_the_broker():
    monitor = EventMonitor(prefix=PREFIX, door_token="porta")
    monitor.handle(f"{PREFIX}/stato", b"offline")
    since = monitor.controller_offline_since
    assert since and monitor.controller_online is False
    # Un altro "offline" (per esempio il messaggio conservato dopo una riconnessione) non sposta l'inizio
    monitor.handle(f"{PREFIX}/stato", b"offline")
    assert monitor.controller_offline_since == since
    monitor.handle(f"{PREFIX}/stato", b"online")
    assert monitor.controller_offline_since == ""


def test_parse_event_ignores_other_topics_and_bad_payloads():
    assert parse_event(PREFIX, "altro/eventi/Door", payload({})) is None
    assert parse_event(PREFIX, f"{PREFIX}/eventi/Door", b"non json") is None


def test_monitor_tracks_categories_card_and_status():
    monitor = EventMonitor(prefix=PREFIX)
    monitor.handle(f"{PREFIX}/stato", b"online")
    monitor.handle(f"{PREFIX}/eventi/IdPoint/Request/IdData/x", payload({"Card": "04a1", "BitCount": "56"}))
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorMode/x", payload({"State": "Accessed"}))
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorMode/x", payload({"State": "Locked"}))

    assert monitor.controller_online is True
    assert monitor.categories["Door"].count == 2
    assert monitor.categories["IdPoint"].count == 1
    assert monitor.last_card["card"] == "04a1"
    assert "AccessControl" not in monitor.categories


def test_monitor_tracks_door_conditions_and_controller():
    monitor = EventMonitor(prefix=PREFIX, door_token="porta")
    changes = []
    monitor.on_status = lambda: changes.append(monitor.controller_online)
    door = "$source/DoorToken/porta"
    monitor.handle(f"{PREFIX}/stato", b"offline")
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorAlarm/{door}", payload({"State": "DoorForcedOpen"}))
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorWarning/{door}", payload({"State": "Normal"}))
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorTamper/{door}", payload({"State": "TamperDetected"}))
    monitor.handle(f"{PREFIX}/eventi/Device/Casing/Open/$source/Name/Casing", payload({"Open": "1"}))
    monitor.handle(f"{PREFIX}/eventi/Device/Status/SystemReady", payload({"ready": "1"}))
    monitor.handle(f"{PREFIX}/eventi/Device/IO/Port/$source/port/2", payload({"state": "1"}))
    monitor.handle(f"{PREFIX}/eventi/Device/IO/Port/$source/port/0", payload({"state": "0"}))

    assert changes == [False]
    conditions = monitor.door_conditions["porta"]
    assert conditions["allarme"]["active"] is True and conditions["allarme"]["state"] == "DoorForcedOpen"
    assert conditions["preallarme"]["active"] is False
    assert conditions["manomissione"]["active"] is True
    assert monitor.casing_open is True
    assert monitor.system_ready.startswith("2024-10-24")
    assert monitor.io_ports == {"2": True, "0": False}


# Controller degli eventi salvati (dalla 0.27.0)


def _app(options, controller, tmp_path: Path):
    ha = HomeAssistant(token="", snapshot_dir=tmp_path / "media")
    return create_app(
        options, allow_any_client=True, controller=controller, db_path=tmp_path / "e.db", home_assistant=ha
    )


def _controllers(db) -> dict[int, int | None]:
    return {row["id"]: row["controller_id"] for row in db._query("SELECT id, controller_id FROM events")}


def test_saved_events_carry_the_controller_that_sent_them(settings, controller, tmp_path, monkeypatch):
    app = _app(settings, controller, tmp_path)
    with TestClient(app) as client:
        state = app.state
        primary = state.installation.primary["id"]
        # Come gli eventi di prima della 0.27.0: senza controller, cioè il principale
        old = state.db.add_event(ts="2026-10-20T08:00:00+00:00", kind="concesso", door="porta")
        # Un evento in diretta (WebSocket o MQTT), una riga dell'app e gli eventi del registro interno del controller
        at = datetime(2026, 11, 7, 10, 0, tzinfo=UTC)
        read = Event("IdPoint", "IdPoint/Request/IdData/$source/IdPoint/lettore-1", at, {"Card": "c0110a99"})
        client.portal.call(state.service.record_event, read)
        client.portal.call(state.service.log_lock_alarm, "porta", "riblocco forzato")
        controller.log_events = json.loads((Path(__file__).parent / "data" / "eventlogger_a1210.json").read_text())
        client.portal.call(state.service.recover_events, datetime(2026, 10, 25, 17, 0, tzinfo=UTC))

        # Riga dell'app non salvata (disco pieno) e salvata appena il registro torna scrivibile: stesso controller
        def full(**fields):
            raise sqlite3.OperationalError("database or disk is full")

        monkeypatch.setattr(state.db, "add_event", full)
        client.portal.call(state.service.log_lock_alarm, "porta", "riblocco forzato dopo")
        monkeypatch.undo()
        client.portal.call(state.service.check_writable)
        rows = state.db._query("SELECT kind, detail, recovered FROM events")
        controllers = _controllers(state.db)
    assert controllers.pop(old) is None
    assert {"sconosciuta", "allarme", "negato"} <= {row["kind"] for row in rows}
    assert any(row["recovered"] for row in rows)
    assert "riblocco forzato dopo" in {row["detail"] for row in rows}
    assert set(controllers.values()) == {primary}


def test_events_follow_the_controller_when_it_is_replaced(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = _app(parse_options({}), controller, tmp_path)
    data = {"name": "A1210", "host": "10.0.0.5", "username": "root", "password": "x", "mqtt_topic_prefix": "acc"}
    with TestClient(app) as client:
        state = app.state

        def call() -> int:
            # Il servizio va ripreso a ogni chiamata: una nuova configurazione ne crea uno nuovo
            return client.portal.call(state.service.record_intercom, "sensor.citofono", "Citofono")["id"]

        # Nessun controller: NULL, come il principale
        alone = call()
        first = client.post("/api/controllers", json=data).json()["id"]
        with_first = call()
        assert client.delete(f"/api/controllers/{first}").json() == {"ok": True}
        # Controller sostituito: il numero non si riusa, e gli eventi nuovi sono del nuovo
        second = client.post("/api/controllers", json=data).json()["id"]
        with_second = call()
        controllers = _controllers(state.db)
    assert second != first
    assert (controllers[alone], controllers[with_first], controllers[with_second]) == (None, first, second)
