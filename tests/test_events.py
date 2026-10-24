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
