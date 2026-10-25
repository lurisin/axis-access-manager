import json

from app.events import EventMonitor, parse_event

PREFIX = "accessi/via_consulta_controller"


def payload(data: dict, timestamp: int = 1729800000000) -> bytes:
    return json.dumps({"timestamp": timestamp, "message": {"data": data}}).encode()


def test_parse_event_strips_prefix():
    event = parse_event(PREFIX, f"{PREFIX}/eventi/IdPoint/Request/IdData/$source/IdPoint/x", payload({"Card": "04a1"}))
    assert event.category == "IdPoint"
    assert event.topic == "IdPoint/Request/IdData/$source/IdPoint/x"
    assert event.data == {"Card": "04a1"}
    assert event.timestamp.year == 2024


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
