import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from websockets.asyncio.server import serve

from app import live_events
from app.events import EventMonitor, event_from_ws, event_key, parse_event
from app.live_events import LiveEvents
from app.notifications import NotificationService
from app.vapix import A1210Client, VapixError, cert_fingerprint

PREFIX = "accessi/x"
DOOR = "porta_via_consulta_ingresso_scala"


def notify(topic, source, data, timestamp=1792951050860):
    """Messaggio events:notify come al banco del 25/10 (03_ws_events.log)."""
    return {
        "apiVersion": "1.0",
        "method": "events:notify",
        "params": {
            "notification": {
                "topic": topic,
                "timestamp": timestamp,
                "message": {"source": source, "key": {}, "data": data},
            }
        },
    }


def mqtt(path, data, timestamp=1792951050860):
    return parse_event(
        PREFIX, f"{PREFIX}/eventi/{path}", json.dumps({"timestamp": timestamp, "message": {"data": data}}).encode()
    )


def test_websocket_events_in_the_same_format_as_mqtt():
    event = event_from_ws(
        notify("tns1:Door/State/DoorMode", {"DoorToken": DOOR}, {"State": "Locked"})["params"]["notification"]
    )
    assert event.category == "Door"
    assert event.topic == f"Door/State/DoorMode/$source/DoorToken/{DOOR}"
    assert event.data == {"DoorToken": DOOR, "State": "Locked"}
    assert event.timestamp == datetime.fromtimestamp(1792951050.860, tz=UTC)
    # Stesso evento via MQTT: stessa impronta
    same = mqtt(f"Door/State/DoorMode/$source/DoorToken/{DOOR}", {"State": "Locked"})
    assert event_key(event) == event_key(same)
    # Namespace dell'Axis tolti anche a metà, sorgenti nel topic come le pubblica l'A1210 su MQTT
    port = event_from_ws(notify("tns1:Device/tnsaxis:IO/Port", {"port": "0"}, {"state": "1"})["params"]["notification"])
    assert port.topic == "Device/IO/Port/$source/port/0"
    reader = notify("tns1:IdPoint/tnsaxis:Power", {"Area": "", "Name": "Pulsante a/b", "IdPointToken": "Axis-1"}, {})
    power = event_from_ws(reader["params"]["notification"])
    assert power.topic == "IdPoint/Power/$source/Area//Name/Pulsante a_b/IdPointToken/Axis-1"
    assert power.data["Name"] == "Pulsante a/b"
    assert event_from_ws({"timestamp": 1}) is None and event_from_ws("x") is None


def test_same_event_from_websocket_and_mqtt_counts_once():
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    seen = []
    monitor.on_event = lambda event, retained: seen.append((event.topic.split("/$")[0], event.data.get("State")))
    alarm = notify("tns1:Door/State/DoorAlarm", {"DoorToken": DOOR}, {"State": "DoorForcedOpen"})
    assert monitor.apply_event(event_from_ws(alarm["params"]["notification"])) is True
    payload = json.dumps({"timestamp": 1792951050860, "message": {"data": {"State": "DoorForcedOpen"}}}).encode()
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorAlarm/$source/DoorToken/{DOOR}", payload)
    assert seen == [("Door/State/DoorAlarm", "DoorForcedOpen")]
    assert monitor.categories["Door"].count == 1
    # Un altro valore alla stessa ora, o lo stesso valore a un'altra ora, è un altro evento
    monitor.apply_event(mqtt(f"Door/State/DoorAlarm/$source/DoorToken/{DOOR}", {"State": "Normal"}))
    monitor.apply_event(mqtt(f"Door/State/DoorAlarm/$source/DoorToken/{DOOR}", {"State": "Normal"}, 1792951051000))
    assert len(seen) == 3


def test_controller_off_the_broker_loses_nothing_with_the_websocket():
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    monitor.io_ports = {"0": True}
    monitor.ws_live = True
    # Collaudo 29/10: controller scollegato dal broker, ma il WebSocket porta gli eventi: niente da recuperare
    monitor.handle(f"{PREFIX}/stato", b"offline")
    assert monitor.outages == 0 and monitor.io_ports == {"0": True}
    assert monitor.controller_offline_since
    monitor.ws_live = False
    monitor.handle(f"{PREFIX}/stato", b"online")
    monitor.handle(f"{PREFIX}/stato", b"offline")
    assert monitor.outages == 1 and monitor.io_ports == {}


class FakeClient:
    def __init__(self, port):
        self.port = port
        self.sessions = 0

    async def ws_session(self):
        self.sessions += 1
        return f"gettone{self.sessions}"

    def ws_url(self, token):
        return f"ws://127.0.0.1:{self.port}/vapix/ws-data-stream?wssession={token}&sources=events"

    def ws_ssl(self):
        return None


async def _controller(handler):
    server = await serve(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(live_events, "INITIAL_STATE_SECONDS", 0.2)
    monkeypatch.setattr(live_events, "RETRY_DELAYS", (0.05,))


async def _shutdown(server):
    server.close()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(server.wait_closed(), 2)


async def _until(check, seconds=3.0):
    for _ in range(int(seconds / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condizione non raggiunta")


async def test_events_from_the_websocket(quick):
    requests, configures = [], []

    async def handler(ws):
        requests.append(ws.request.path)
        configures.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"apiVersion": "1.0", "context": "aam", "method": "events:configure", "data": {}}))
        # Subito lo stato attuale (ora dell'ultimo cambio), poi gli eventi
        await ws.send(json.dumps(notify("tns1:Door/State/DoorMode", {"DoorToken": DOOR}, {"State": "Locked"})))
        await ws.send(json.dumps(notify("tns1:AccessControl/AccessGranted/Credential", {"AccessPointToken": "ap"}, {})))
        await asyncio.sleep(0.4)
        await ws.send(json.dumps(notify("tns1:Door/State/DoorAlarm", {"DoorToken": DOOR}, {"State": "DoorForcedOpen"})))
        await asyncio.sleep(0.2)

    server, port = await _controller(handler)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    received, statuses = [], []
    monitor.on_event = lambda event, retained: received.append((event.topic.split("/$")[0], retained))
    monitor.on_status = lambda: statuses.append(monitor.ws_live)
    live = LiveEvents(monitor, FakeClient(port), lambda: True)
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: len(received) == 3)
        assert live.connected and monitor.ws_live and live.down_since == ""
        assert requests == ["/vapix/ws-data-stream?wssession=gettone1&sources=events"]
        filters = [f["topicFilter"] for f in configures[0]["params"]["eventFilterList"]]
        assert filters == ["tns1:AccessControl//.", "tns1:Door//.", "tns1:IdPoint//.", "tns1:Device//."]
        # Lo stato iniziale non fa righe nel registro, i passaggi sì
        assert received == [
            ("Door/State/DoorMode", True),
            ("AccessControl/AccessGranted/Credential", False),
            ("Door/State/DoorAlarm", False),
        ]
        assert monitor.door_modes == {DOOR: "Locked"}
        # Il controller chiude: eventi interrotti, si riprova con un gettone nuovo
        await _until(lambda: live.client.sessions >= 2)
        assert monitor.outages >= 1 and statuses[:2] == [True, False]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown(server)
    assert monitor.ws_live is False and live.connected is False


async def test_filters_refused_by_the_controller(quick):
    async def handler(ws):
        await ws.recv()
        error = {"code": 2104, "message": "eventFilterList key is not found"}
        await ws.send(json.dumps({"apiVersion": "1.0", "method": "events:configure", "error": error}))
        await asyncio.sleep(1)

    server, port = await _controller(handler)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    live = LiveEvents(monitor, FakeClient(port), lambda: True)
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: live.last_error)
        assert live.last_error == "il controller ha rifiutato i filtri degli eventi (2104)"
        assert live.down_since and not live.connected and monitor.outages == 0 and not monitor.ws_live
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown(server)


async def test_no_websocket_while_the_controller_does_not_answer(quick, monkeypatch):
    monkeypatch.setattr(live_events, "IDLE_WAIT", 0.05)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    answering = {"now": False}
    client = FakeClient(1)
    live = LiveEvents(monitor, client, lambda: answering["now"])
    live.down_since = "2026-10-31T08:00:00+00:00"
    task = asyncio.create_task(live.run())
    try:
        await asyncio.sleep(0.2)
        # Niente tentativi verso un controller che non risponde (o sostituito), e non conta come guasto
        assert client.sessions == 0 and live.down_since == ""
        answering["now"] = True
        live.wake.set()
        await _until(lambda: client.sessions >= 1 and live.last_error)
        assert live.last_error.startswith("collegamento non riuscito") and live.down_since
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_websocket_session_token():
    answers = iter([(200, "AbCdEfGhIj0123456789\n"), (401, ""), (404, "<html>Not found</html>")])

    def respond(request):
        assert request.url.path == "/axis-cgi/wssession.cgi"
        status, text = next(answers)
        return httpx.Response(status, text=text)

    client = A1210Client("10.0.0.5", "u", "p", transport=httpx.MockTransport(respond))
    assert await client.ws_session() == "AbCdEfGhIj0123456789"
    with pytest.raises(VapixError, match="credenziali"):
        await client.ws_session()
    with pytest.raises(VapixError, match="risposta 404"):
        await client.ws_session()
    assert client.ws_url("abc") == "wss://10.0.0.5/vapix/ws-data-stream?wssession=abc&sources=events"
    assert client.ws_ssl().check_hostname is False
    await client.aclose()


def _events_fault(db, ws, now, **overrides):
    values = {
        "mqtt_configured": True,
        "mqtt_connected": True,
        "controller_configured": True,
        "controller_online": None,
        "sync": {"ok": True},
        "door_names": {},
        "primary_id": 1,
        "controllers": [{"id": 1, "name": "Rack", "online": True, "unreachable": False}],
        "ws": ws,
        "now": now,
    }
    return NotificationService(db).collect(**{**values, **overrides})


def test_live_events_fault_follows_websocket_and_mqtt(db):
    now = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    down = {"connected": False, "down_since": "2026-10-31T08:50:00+00:00", "error": "risposta 401"}
    # MQTT non configurato sul controller (facoltativo) e WebSocket che non si collega da 10 minuti
    [fault] = _events_fault(db, down, now)
    assert fault["id"] == "events@2026-10-31T08:50:00+00:00" and fault["title"] == "Eventi del Controller Non Arrivano"
    assert "(WebSocket: risposta 401; MQTT: il controller non pubblica sul broker)" in fault["text"]
    # Con il WebSocket collegato, o con il controller sul broker, gli eventi arrivano: nessun guasto
    assert _events_fault(db, {"connected": True, "down_since": None, "error": ""}, now) == []
    assert _events_fault(db, down, now, controller_online=True) == []
    # Un tentativo appena fallito non è ancora un guasto
    assert _events_fault(db, down, now - timedelta(minutes=9, seconds=30)) == []
    # Controller scollegato dal broker con il WebSocket collegato: non è un guasto (era il guasto del 29/10)
    live = {"connected": True, "down_since": None, "error": ""}
    assert _events_fault(db, live, now, controller_online=False, controller_offline_since="2026-10-31T08:00:00Z") == []


def test_broker_needed_only_for_home_assistant_with_the_websocket(db):
    now = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    live = {"connected": True, "down_since": None, "error": ""}
    [none] = _events_fault(db, live, now, mqtt_configured=False, mqtt_connected=False)
    assert none["id"] == "mqtt:none" and none["text"].startswith("Home Assistant non riceve stato")
    assert none["text"].endswith("Gli eventi del controller arrivano comunque con il WebSocket.")
    # Senza l'integrazione con Home Assistant il broker non serve
    assert _events_fault(db, live, now, mqtt_configured=False, mqtt_connected=False, ha_integration=False) == []


def test_live_copy_of_an_event_first_seen_as_state_goes_to_the_log():
    """Allarme nei primi secondi del WebSocket (preso per stato) e poi in diretta via MQTT: vale per il registro."""
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    seen = []
    monitor.on_event = lambda event, retained: seen.append(retained)
    alarm = event_from_ws(
        notify("tns1:Door/State/DoorAlarm", {"DoorToken": DOOR}, {"State": "DoorForcedOpen"})["params"]["notification"]
    )
    assert monitor.apply_event(alarm, retained=True) is True
    payload = json.dumps({"timestamp": 1792951050860, "message": {"data": {"State": "DoorForcedOpen"}}}).encode()
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorAlarm/$source/DoorToken/{DOOR}", payload)
    assert seen == [True, False] and monitor.categories["Door"].count == 1
    # Una terza copia, conservata o no, non fa più niente
    monitor.handle(f"{PREFIX}/eventi/Door/State/DoorAlarm/$source/DoorToken/{DOOR}", payload)
    monitor.apply_event(alarm, retained=True)
    assert seen == [True, False]


def test_old_broker_messages_do_not_change_the_state_with_the_websocket():
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    monitor.casing_open, monitor.casing_since = False, "2026-10-30T08:00:00+00:00"
    monitor.ws_live = True
    # Mosquitto riavviato: ripete il coperchio aperto conservato da settembre
    old = json.dumps({"timestamp": 1758400000000, "message": {"data": {"Open": "1"}}}).encode()
    monitor.handle(f"{PREFIX}/eventi/Device/Casing/Open/$source/Name/Casing", old, retained=True)
    assert monitor.casing_open is False and monitor.casing_since == "2026-10-30T08:00:00+00:00"
    # In diretta sì, e senza WebSocket anche i conservati (prima lettura dopo l'avvio)
    monitor.handle(f"{PREFIX}/eventi/Device/Casing/Open/$source/Name/Casing", old)
    assert monitor.casing_open is True
    monitor.ws_live = False
    closed = json.dumps({"timestamp": 1758400001000, "message": {"data": {"Open": "0"}}}).encode()
    monitor.handle(f"{PREFIX}/eventi/Device/Casing/Open/$source/Name/Casing", closed, retained=True)
    assert monitor.casing_open is False


def _ack():
    return json.dumps({"apiVersion": "1.0", "context": "aam", "method": "events:configure", "data": {}})


async def test_websocket_closes_when_the_controller_changes(quick):
    """Controller cambiato (scritture sospese) mentre il WebSocket è aperto: si chiude subito, niente più eventi."""

    async def handler(ws):
        await ws.recv()
        await ws.send(_ack())
        for i in range(500):
            await ws.send(
                json.dumps(
                    notify("tns1:Device/tnsaxis:IO/Port", {"port": "0"}, {"state": str(i % 2)}, 1792951050860 + i)
                )
            )
            await asyncio.sleep(0.01)

    server, port = await _controller(handler)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    answering = {"now": True}
    live = LiveEvents(monitor, FakeClient(port), lambda: answering["now"])
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: live.events >= 5)
        # Un solo controllo fallito (None) non basta per chiudere
        answering["now"] = None
        live.wake.set()
        await asyncio.sleep(0.1)
        assert live.connected
        answering["now"] = False
        live.wake.set()
        await _until(lambda: not live.connected)
        count = live.events
        await asyncio.sleep(0.2)
        assert live.events == count and not monitor.ws_live
        assert live.last_error == "il controller non risponde più o è cambiato"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown(server)


async def test_no_events_from_a_controller_suspended_while_connecting(quick):
    answering = {"now": True}

    async def handler(ws):
        await ws.recv()
        # Sospeso mentre si collegava (controllo periodico che trova un controller diverso)
        answering["now"] = False
        await ws.send(_ack())
        await asyncio.sleep(1)

    server, port = await _controller(handler)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    live = LiveEvents(monitor, FakeClient(port), lambda: answering["now"])
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: live.last_error)
        assert live.last_error == "il controller non risponde più o è cambiato" and not monitor.ws_live
        assert live.events == 0 and monitor.outages == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown(server)


class _Peer:
    def __init__(self, der):
        self.der = der

    def get_extra_info(self, name):
        assert name == "ssl_object"
        return self

    def getpeercert(self, binary_form=False):
        return self.der


class _Socket:
    def __init__(self, der):
        self.transport = _Peer(der)


def test_websocket_only_with_the_known_certificate():
    known = {"fp": ""}
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(1), lambda: True, fingerprint=lambda: known["fp"])
    # Controller non ancora riconosciuto: nessun confronto
    live._check_certificate(_Socket(b"certificato"))
    known["fp"] = cert_fingerprint(b"certificato")
    live._check_certificate(_Socket(b"certificato"))
    # Un altro apparecchio all'indirizzo, prima che il controllo periodico se ne accorga
    with pytest.raises(live_events.WebSocketError, match="certificato"):
        live._check_certificate(_Socket(b"un altro"))
    # Senza TLS (prove) non c'è niente da confrontare
    live._check_certificate(_Socket(None))
    assert known["fp"].count(":") == 31


async def test_connection_that_drops_right_away_backs_off(quick, monkeypatch, caplog):
    """Controller che accetta i filtri e chiude subito: attese sempre più lunghe, un avviso, guasto dal primo."""
    monkeypatch.setattr(live_events, "RETRY_DELAYS", (0.02, 0.05, 0.3))

    async def handler(ws):
        await ws.recv()
        await ws.send(_ack())

    server, port = await _controller(handler)
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    client = FakeClient(port)
    live = LiveEvents(monitor, client, lambda: True)
    task = asyncio.create_task(live.run())
    caplog.set_level(logging.DEBUG, logger="app.live_events")
    try:
        await _until(lambda: client.sessions >= 2 and live.down_since)
        first = live.down_since
        sessions = client.sessions
        await asyncio.sleep(0.5)
        # Arrivato alle attese lunghe: pochi tentativi, e il guasto conta dalla prima caduta
        assert client.sessions - sessions <= 3 and live.down_since == first
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(warnings) == 1 and len(infos) == 1
        # Ogni ritorno dopo una caduta rilegge lo stato e recupera gli eventi persi
        assert monitor.outages >= 2 * (client.sessions - 1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _shutdown(server)


async def test_stable_connection_that_drops_starts_over(quick, monkeypatch):
    monkeypatch.setattr(live_events, "STABLE_AFTER", 0)
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(1), lambda: True)
    live.down_since, live._attempt, live._logged_error = "2026-10-31T08:00:00+00:00", 3, "collegamento perso"
    live._connected()
    assert live.status()["down_since"] is None and live.status()["error"] == ""
    live._lost(live_events.WebSocketError("collegamento perso"))
    assert live._attempt == 0 and live.down_since > "2026-10-31T08:00:00+00:00"
    assert live._logged_error == "collegamento perso"


async def test_stopping_does_not_wait_for_a_silent_controller(quick):
    """Controller spento con il WebSocket aperto: una nuova configurazione non aspetta la chiusura ordinata."""
    stop, peers = asyncio.Event(), []

    async def handler(ws):
        peers.append(ws)
        await ws.recv()
        await ws.send(_ack())
        # Non legge più niente, nemmeno la chiusura
        ws.transport.pause_reading()
        await stop.wait()

    server, port = await _controller(handler)
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(port), lambda: True)
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: live.connected)
        started = time.monotonic()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert time.monotonic() - started < 1
    finally:
        stop.set()
        for peer in peers:
            peer.transport.abort()
        await _shutdown(server)


async def test_websocket_task_survives_errors_and_retries_soon(quick, monkeypatch):
    monkeypatch.setattr(live_events, "IDLE_WAIT", 0.05)
    answers = iter([RuntimeError("database"), True, None, True])
    calls = []

    def responding():
        calls.append(1)
        value = next(answers, True)
        if isinstance(value, Exception):
            raise value
        return value

    client = FakeClient(1)
    live = LiveEvents(EventMonitor(prefix=PREFIX), client, responding)
    live._attempt = 3
    task = asyncio.create_task(live.run())
    try:
        await _until(lambda: client.sessions >= 2)
        assert not task.done()
        # Controller che non ha risposto a un controllo: al ritorno attese di nuovo brevi
        assert live._attempt <= 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_websocket_fault_waits_a_minute_in_the_interface():
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(1), lambda: True)
    assert live.status()["failing"] is False
    live.down_since = (datetime.now(tz=UTC) - timedelta(seconds=10)).isoformat(timespec="seconds")
    assert live.status()["failing"] is False and live.status()["down_since"]
    live.down_since = (datetime.now(tz=UTC) - timedelta(seconds=61)).isoformat(timespec="seconds")
    assert live.status()["failing"] is True


def test_broker_needed_for_intercoms_too(db):
    now = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    live = {"connected": True, "down_since": None, "error": ""}
    [none] = _events_fault(
        db, live, now, mqtt_configured=False, mqtt_connected=False, ha_integration=False, intercoms=True
    )
    assert none["id"] == "mqtt:none" and none["text"].startswith("Le chiamate e le aperture dei videocitofoni")
    [lost] = _events_fault(
        db, live, now, mqtt_connected=False, intercoms=True, mqtt_disconnected_since="2026-10-31T08:00:00+00:00"
    )
    assert lost["title"] == "Collegamento MQTT Perso"
    assert "Home Assistant non riceve stato, pulsanti ed eventi dell'app; le chiamate" in lost["text"]


def test_initial_state_from_the_site_controller():
    """Raffica iniziale letta in sola lettura dall'A1210 in loco il 31/10: coperchio aperto dall'avvio del 29/10."""
    monitor = EventMonitor(prefix=PREFIX, door_token=DOOR)
    burst = [
        ("tns1:Device/tnsaxis:Status/SystemReady", 1793310931717, {}, {"ready": "1"}),
        ("tns1:Device/tnsaxis:IO/Port", 1793311113230, {"port": "0"}, {"state": "1"}),
        ("tns1:Device/tnsaxis:IO/Port", 1793311113230, {"port": "-1"}, {"state": "1"}),
        ("tns1:Device/tnsaxis:IO/Port", 1793311110557, {"port": "1"}, {"state": "0"}),
        ("tns1:Device/tnsaxis:IO/VirtualPort", 1793310918319, {"port": "1"}, {"state": "0"}),
        ("tns1:Device/Trigger/DigitalInput", 1793310917354, {"InputToken": "2"}, {"LogicalState": "0"}),
        ("tns1:Device/tnsaxis:Casing/Open", 1793310917346, {"Name": "Casing"}, {"Open": "1"}),
        ("tns1:Device/tnsaxis:MQTT/ClientStatus", 1793311159728, {}, {"connected": "1", "active": "1"}),
        ("tns1:Door/State/DoorAlarm", 1793310920068, {"DoorToken": DOOR}, {"State": "Normal"}),
        ("tns1:Door/State/DoorTamper", 1793310920067, {"DoorToken": DOOR}, {"State": "NotInTamper"}),
        ("tns1:Door/State/DoorMode", 1793397329960, {"DoorToken": DOOR}, {"State": "Locked"}),
    ]
    for topic, ts, source, data in burst:
        assert monitor.apply_event(event_from_ws(notify(topic, source, data, ts)["params"]["notification"]), True)
    assert monitor.casing_open is True and monitor.casing_since == "2026-10-29T21:55:17+00:00"
    assert monitor.system_ready == "2026-10-29T21:55:31+00:00"
    assert monitor.io_ports["0"] is True and monitor.io_ports["1"] is False
    assert monitor.door_modes == {DOOR: "Locked"}
    alarm = monitor.door_conditions[DOOR]
    assert alarm["allarme"]["active"] is False and alarm["manomissione"]["active"] is False


def test_log_says_again_when_the_websocket_keeps_dropping(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="app.live_events")
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(1), lambda: True)
    closed = live_events.WebSocketError("collegamento chiuso dal controller")
    live._connected()
    monkeypatch.setattr(live_events, "STABLE_AFTER", 0)
    live._lost(closed)
    # Di nuovo «in diretta» nel log, ma cade subito: la caduta si scrive di nuovo, le successive no
    live._connected()
    monkeypatch.setattr(live_events, "STABLE_AFTER", 60)
    live._lost(closed)
    live._connected()
    live._lost(closed)
    levels = [(r.levelname, r.getMessage().split(":")[0]) for r in caplog.records if r.levelno >= logging.INFO]
    assert levels == [
        ("INFO", "Eventi del controller in diretta con il WebSocket"),
        ("WARNING", "WebSocket degli eventi non collegato"),
        ("INFO", "Eventi del controller in diretta con il WebSocket"),
        ("WARNING", "WebSocket degli eventi non collegato"),
    ]


def test_access_token_never_in_the_log(caplog):
    from websockets.exceptions import InvalidURI

    caplog.set_level(logging.DEBUG, logger="app.live_events")
    live = LiveEvents(EventMonitor(prefix=PREFIX), FakeClient(1), lambda: True)
    url = "wss://admin@10.0.0.5/vapix/ws-data-stream?wssession=SEGRETO0123456789ABC&sources=events"
    live._lost(InvalidURI(url, "username provided without password"))
    assert live.last_error == "indirizzo del controller non valido"
    assert caplog.records and all("SEGRETO" not in r.getMessage() for r in caplog.records)
    assert any("wssession=***" in r.getMessage() for r in caplog.records)
