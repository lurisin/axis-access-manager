import asyncio

import aiomqtt
import pytest

from app.events import EventMonitor
from app.intercom import IntercomBridge, IntercomSettings, topic_name, unlock_source
from app.service import AccessService

CALL = "hmd/sensor/Citofono-scala/Call-state/state"
RELAY = "hmd/switch/Citofono-scala/Door-1-relay/state"
RELAY_ATTRIBUTES = "hmd/switch/Citofono-scala/Door-1-relay/attributes"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(db, controller, settings):
    return AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))


def bridge_for(service, db, clock, open_door=True):
    return IntercomBridge(
        IntercomSettings("Citofono scala", open_door=open_door),
        log=db.add_event,
        open_door=lambda source, note: service.open_door(False, source, note=note),
        clock=clock,
    )


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def details(db):
    return [(e["kind"], e["detail"], e["source"]) for e in reversed(db.list_events())]


def test_topics_follow_ha_mqtt_discoverable_names(service, db, clock):
    bridge = bridge_for(service, db, clock)
    assert topic_name("Citofono scala") == "Citofono-scala"
    assert bridge.topics == [CALL, RELAY, RELAY_ATTRIBUTES]


async def test_open_from_app_sends_one_access_door(service, db, controller, clock):
    bridge = bridge_for(service, db, clock)
    bridge.handle(CALL, b"ringing")
    clock.now += 8
    bridge.handle(RELAY_ATTRIBUTES, b'{"unlock_type": "HOUSEHOLDER", "control_source": "app"}')
    bridge.handle(RELAY, b"ON")
    bridge.handle(RELAY, b"ON")  # stesso tasto, secondo messaggio
    await settle()
    bridge.handle(RELAY, b"OFF")
    bridge.handle(CALL, b"idle")

    assert controller.calls == [("access_door", "porta", False)]
    assert details(db) == [
        ("citofono", "ha suonato", "Citofono scala"),
        ("apertura_remota", "8 s dopo la chiamata", "App Hik-Connect (Citofono)"),
    ]


async def test_never_more_than_one_open_within_debounce(service, db, controller, clock):
    bridge = bridge_for(service, db, clock)
    bridge.handle(RELAY, b"ON")
    await settle()
    service.monitor.door_mode = "Locked"
    clock.now += 2
    bridge.handle(RELAY, b"ON")
    await settle()
    clock.now += 10
    bridge.handle(RELAY, b"ON")
    await settle()

    assert controller.calls.count(("access_door", "porta", False)) == 2


async def test_door_not_locked_is_logged(service, db, controller, clock):
    controller.door = "Unlocked"
    bridge = bridge_for(service, db, clock)
    bridge.handle(RELAY, b"ON")
    await settle()

    assert controller.calls == []
    kind, detail, _ = details(db)[-1]
    assert kind == "citofono" and detail.startswith("apertura non eseguita: la porta non è bloccata")


async def test_retained_messages_are_ignored(service, db, controller, clock):
    bridge = bridge_for(service, db, clock)
    bridge.handle(CALL, b"ringing", retained=True)
    bridge.handle(RELAY, b"ON", retained=True)
    await settle()

    assert controller.calls == []
    assert db.list_events() == []


async def test_credentials_stored_on_intercom_are_not_forwarded(service, db, controller, clock):
    bridge = bridge_for(service, db, clock)
    bridge.handle(RELAY_ATTRIBUTES, b'{"unlock_type": "CARD", "control_source": "04aa"}')
    bridge.handle(RELAY, b"ON")
    await settle()

    assert controller.calls == []
    assert details(db)[-1][1] == "apertura con Tessera del citofono non inoltrata all'A1210"


async def test_forwarding_disabled_only_logs(service, db, controller, clock):
    bridge = bridge_for(service, db, clock, open_door=False)
    bridge.handle(RELAY, b"ON")
    await settle()

    assert controller.calls == []
    assert "inoltro all'A1210 disattivato" in details(db)[-1][1]


def test_call_outcomes(service, db, clock):
    bridge = bridge_for(service, db, clock)
    bridge.handle(CALL, b"ringing")
    bridge.handle(CALL, b"idle")
    bridge.handle(CALL, b"ring")
    clock.now += 4
    bridge.handle(CALL, b"onCall")
    bridge.handle(CALL, b"idle")
    bridge.handle(CALL, b"ringing")
    bridge.handle(CALL, b"dismissed")

    assert [d[1] for d in details(db)] == [
        "ha suonato",
        "nessuna risposta",
        "ha suonato",
        "risposta dopo 4 s",
        "ha suonato",
        "chiamata chiusa",
    ]


def test_monitor_routes_extra_subscriptions():
    seen = []
    monitor = EventMonitor(prefix="accessi/x")
    monitor.extra_subscriptions["hmd/sensor/+/Call-state/state"] = lambda t, p, r: seen.append((t, p, r))
    monitor._dispatch_extra(aiomqtt.Topic(CALL), b"ringing", retained=True)
    monitor._dispatch_extra(aiomqtt.Topic("hmd/sensor/Altro/Online/state"), b"x")

    assert seen == [(CALL, b"ringing", True)]


def test_unlock_source_labels():
    assert unlock_source({"unlock_type": "HOUSEHOLDER", "control_source": "app"}) == "App Hik-Connect"
    assert unlock_source({"unlock_type": "HOUSEHOLDER", "control_source": "indoorMonitor"}) == "Monitor Interno"
    assert unlock_source({"unlock_type": "HOUSEHOLDER"}) == "App O Monitor"
    assert unlock_source({"unlock_type": "CENTER_PLATFORM"}) == "Piattaforma Remota"
    assert unlock_source({}) == "App O Monitor"
