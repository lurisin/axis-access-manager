import json

import pytest

from app.config import parse_options
from app.events import EventMonitor
from app.ha_mqtt import HaBridge


class FakeClient:
    def __init__(self):
        self.published = {}

    async def publish(self, topic, payload, qos=0, retain=False):
        self.published[topic] = payload


@pytest.fixture
def bridge(db, settings):
    monitor = EventMonitor(prefix="accessi/x", door_token="porta")
    return HaBridge(settings, monitor, db)


async def test_discovery_publishes_entities_and_state(bridge):
    client = FakeClient()
    await bridge.on_connect(client)

    config = json.loads(client.published["homeassistant/button/axis_access_manager/apri_porta/config"])
    assert config["command_topic"] == "axis_access_manager/comando"
    assert config["payload_press"] == "apri"
    event = json.loads(client.published["homeassistant/event/axis_access_manager/eventi/config"])
    assert "accesso_negato" in event["event_types"]
    assert client.published["axis_access_manager/disponibile"] == "online"
    assert json.loads(client.published["axis_access_manager/stato"])["porta"] == "Sconosciuto"


async def test_disabled_integration_removes_entities(db):
    settings = parse_options({"ha_integration": False, "door_token": "porta"})
    bridge = HaBridge(settings, EventMonitor(prefix="x"), db)
    client = FakeClient()
    await bridge.on_connect(client)
    assert client.published["homeassistant/button/axis_access_manager/apri_porta/config"] == b""
    assert bridge.will() is None


async def test_publish_event_updates_last_access(bridge, monkeypatch):
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, json.loads(payload), retain))
        return True

    monkeypatch.setattr(bridge.monitor, "publish", publish)
    await bridge.publish_event({"kind": "negato", "person_name": "Anna", "card": "04aa", "ts": "t"})
    await bridge.publish_event({"kind": "sconosciuto_tipo"})

    assert sent[0][0] == "axis_access_manager/evento"
    assert sent[0][1]["event_type"] == "accesso_negato"
    assert sent[1] == (
        "axis_access_manager/ultimo",
        {"persona": "Anna", "tessera": "04aa", "esito": "accesso_negato", "dettaglio": "", "ora": "t"},
        True,
    )
    assert len(sent) == 2
