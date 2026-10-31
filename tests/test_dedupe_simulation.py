"""Passaggi di tessera simulati con i messaggi veri dell'A1210, nell'app intera: lo stesso passaggio arriva dal
WebSocket, da MQTT e dal registro del controller e nel Registro deve comparire una volta sola.

I messaggi WebSocket sono quelli registrati al banco il 25/10 (/mnt/project-files/03_ws_events.log, terzo giro):
una tessera sconosciuta (c0110b18, con il diniego doppio /Card e senza) e una valida (04a11a34). Le copie MQTT
hanno il formato con cui l'A1210 pubblica sul broker: sorgenti nel topic dopo $source e stessa ora dell'evento.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from app import live_events
from app.live_events import LiveEvents
from app.main import create_app

PREFIX = "accessi/x"
READER = {
    "Area": "",
    "Location": "",
    "Name": "Via Consulta - Lettore Esterno",
    "IdPointToken": "lettore_via_consulta_ingresso_scala",
    "Category": "IdPoint",
}
ACCESS_POINT = {"AccessPointToken": "accesso_via_consulta_ingresso_scala"}
DENIED = {
    "CardNr": "",
    "Card": "c0110b18",
    "BitCount": "",
    "FormatType": "SIMPLE_ALPHA_NUMERIC",
    "IdentifierValue": "6330313130623138",
    "IdentifierType": "pt:Card",
}
GRANTED = {
    "CredentialToken": "Axis-b8a44fe6a074:1792889564.863735000",
    "CredentialType": "StandardCredential",
    "CredentialHolderName": "utente_ciccio_bello_261025005209",
}
DOOR = {"DoorToken": "porta"}
# (topic WebSocket, ora in ms, sorgente, dati) nell'ordine in cui sono arrivati al banco
PASSAGES = [
    (
        "tns1:IdPoint/tnsaxis:Activity",
        1792951066759,
        READER,
        {"Description": "probe 0.18 sconosciuta run3", "Reason": "CardPresented"},
    ),
    (
        "tns1:IdPoint/tnsaxis:Request/IdData",
        1792951066760,
        READER,
        {"Description": "probe 0.18 sconosciuta run3", "Card": "c0110b18", "Action": "Access"},
    ),
    ("tns1:AccessControl/Denied/CredentialNotFound/Card", 1792951066772, ACCESS_POINT, DENIED),
    ("tns1:AccessControl/Denied/CredentialNotFound", 1792951066772, ACCESS_POINT, DENIED),
    (
        "tns1:IdPoint/tnsaxis:Activity",
        1792951072852,
        READER,
        {"Description": "probe 0.18 valida run3", "Reason": "CardPresented"},
    ),
    (
        "tns1:IdPoint/tnsaxis:Request/IdData",
        1792951072852,
        READER,
        {"Description": "probe 0.18 valida run3", "Card": "04a11a34", "Action": "Access"},
    ),
    ("tns1:AccessControl/AccessGranted/Credential", 1792951072871, ACCESS_POINT, GRANTED),
    ("tns1:Door/State/DoorMode", 1792951073608, DOOR, {"State": "Accessed"}),
    ("tns1:Door/State/DoorMode", 1792951075860, DOOR, {"State": "Locked"}),
]
# Il passaggio come lo vede l'app: lettura e diniego della sconosciuta, lettura e accesso della valida
EXPECTED = [("negato", "c0110b18"), ("sconosciuta", "c0110b18"), ("concesso", ""), ("sconosciuta", "04a11a34")]


def ws_message(topic, ts, source, data):
    notification = {"topic": topic, "timestamp": ts, "message": {"source": source, "key": {}, "data": data}}
    return json.dumps({"apiVersion": "1.0", "method": "events:notify", "params": {"notification": notification}})


def mqtt_message(topic, ts, source, data):
    """Topic e payload come li pubblica l'A1210 con il prefisso dell'app."""
    path = "/".join(level.rsplit(":", 1)[-1] for level in topic.split("/"))
    sources = "/".join(f"{k}/{v}" for k, v in source.items())
    body = {"topic": topic, "timestamp": ts, "serial": "B8A44FE6A074"}
    body["message"] = {"source": source, "key": {}, "data": data}
    return f"{PREFIX}/eventi/{path}/$source/{sources}", json.dumps(body).encode()


def log_entry(rowid, topic, ts, source, data, delay_us):
    """Lo stesso evento nel registro interno del controller: ora sua, con i microsecondi, poco dopo l'evento."""
    levels = [level.rsplit(":", 1)[-1] for level in topic.split("/")]
    keys = [{"Key": f"topic{i}", "Value": level} for i, level in enumerate(levels)]
    moment = datetime.fromtimestamp(ts / 1000, tz=UTC) + timedelta(microseconds=delay_us)
    values = [{"Key": k, "Value": v} for k, v in {**source, **data}.items()]
    return {"rowid": rowid, "UtcTime": moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "KeyValues": keys + values}


class SiteClient:
    """Il client del controller per il WebSocket, verso un server locale con i messaggi del banco."""

    def __init__(self, port):
        self.port = port

    async def ws_session(self):
        return "AbCdEfGhIj0123456789"

    def ws_url(self, token):
        return f"ws://127.0.0.1:{self.port}/vapix/ws-data-stream?wssession={token}&sources=events"

    def ws_ssl(self):
        return None


@pytest.fixture
def app_client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(live_events, "INITIAL_STATE_SECONDS", 0.3)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "sim.db")
    with TestClient(app) as client:
        yield client


async def _simulate(state, order, initial=False, mqtt_delay=0.03):
    """WebSocket e MQTT insieme. order: "ws" se il WebSocket arriva prima, "mqtt" se arriva prima MQTT.
    initial: i passaggi arrivano nei primi istanti dopo i filtri (raffica dello stato attuale)."""
    copies = [mqtt_message(*p) for p in PASSAGES]

    async def mqtt_copies(delay):
        await asyncio.sleep(delay)
        for topic, payload in copies:
            state.monitor.handle(topic, payload)
            await asyncio.sleep(0.005)

    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"apiVersion": "1.0", "context": "aam", "method": "events:configure", "data": {}}))
        if not initial:
            await asyncio.sleep(0.5)
        if order == "mqtt":
            await mqtt_copies(0)
            await asyncio.sleep(mqtt_delay)
        else:
            asyncio.get_running_loop().create_task(mqtt_copies(mqtt_delay))
        for passage in PASSAGES:
            await ws.send(ws_message(*passage))
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.5)

    server = await serve(handler, "127.0.0.1", 0)
    live = LiveEvents(state.monitor, SiteClient(server.sockets[0].getsockname()[1]), lambda: True)
    task = asyncio.create_task(live.run())
    try:
        for _ in range(200):
            if live.events >= len(PASSAGES):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
    return live


def _rows(state):
    return sorted(
        (e["kind"], e["card"] or "")
        for e in state.db.list_events(limit=50)
        if e["kind"] in ("sconosciuta", "lettura", "concesso", "negato")
    )


@pytest.mark.parametrize(
    ("order", "initial"),
    [("ws", False), ("mqtt", False), ("ws", True)],
    ids=["websocket-prima", "mqtt-prima", "nei-primi-istanti"],
)
def test_each_passage_once_from_websocket_mqtt_and_controller_log(app_client, controller, order, initial):
    state = app_client.app.state
    state.monitor.connected, state.monitor.controller_online = True, True
    live = app_client.portal.call(_simulate, state, order, initial)
    assert live.events == len(PASSAGES)
    assert _rows(state) == sorted(EXPECTED)
    # Ogni evento contato una volta, anche se arrivato due volte
    assert state.monitor.categories["AccessControl"].count == 3
    assert state.monitor.categories["Door"].count == 2
    assert state.monitor.door_modes["porta"] == "Locked"
    # Poi il recupero dal registro del controller (interruzione): stessi passaggi, nessuna riga in più
    controller.log_events = [
        log_entry(1256 + i, *passage, delay_us=150 + 300 * i) for i, passage in enumerate(PASSAGES)
    ]
    first = datetime.fromtimestamp(PASSAGES[0][1] / 1000, tz=UTC)
    recovered = app_client.portal.call(state.service.recover_events, first - timedelta(minutes=1))
    assert recovered == 0
    assert _rows(state) == sorted(EXPECTED)
    # Tessera sconosciuta da registrare: una lettura sola per passaggio
    unknown = {c["number"]: c for c in state.db.list_unknown_cards()}
    assert unknown["c0110b18"]["count"] == 1
