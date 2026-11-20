"""Porta legata al videocitofono per numero di serie (0.28.11), nell'app vera con un broker MQTT in memoria.

Il 17/11 l'app non ha sentito il videocitofono per un giorno intero (13 chiamate, 22 «Apri» dal monitor, nessun evento
e nessun avviso): il nome salvato nell'app non era più quello dell'add-on Hikvision Doorbell, e i topic si fanno con il
nome. Ora la porta prende il numero di serie del videocitofono annunciato sul broker e lo segue anche quando cambia
nome. Solo il FakeController, mai una porta vera; nomi e numeri di serie di fantasia."""

import json
import time

import pytest
from fastapi.testclient import TestClient
from test_intercom_directory import ARTISTI, CARRAIO, SERIAL_A, SERIAL_B, config
from test_main import FakeBroker

from app import events, main
from app.config import MqttSettings, parse_options
from app.intercom import topic_name
from app.main import create_app

RENAMED = "Ingresso Artisti"
FORWARDED = ("access_door", "porta", False)


def topics(name: str, relay: int = 1) -> dict[str, str]:
    clean = topic_name(name)
    return {
        "call": f"hmd/sensor/{clean}/Call-state/state",
        "relay": f"hmd/switch/{clean}/Door-{relay}-relay/state",
        "attributes": f"hmd/switch/{clean}/Door-{relay}-relay/attributes",
    }


class Site:
    """Broker in memoria con l'add-on Hikvision che pubblica, e l'app collegata come sull'impianto."""

    def __init__(self, controller, tmp_path):
        self.broker = FakeBroker()
        self.controller = controller
        self.path = tmp_path / "videocitofoni.db"
        self.client: TestClient | None = None
        self.app = None

    def retain(self, name: str, serial: str, relays=(1, 2)) -> None:
        """Configurazioni conservate sul broker da prima dell'avvio dell'app."""
        for entity in ("call", *relays):
            topic, payload = config(name, serial, entity)
            self.broker.retained[topic] = payload

    def start(self, **options) -> TestClient:
        settings = parse_options(
            {
                "a1210_host": "a",
                "door_token": "porta",
                "access_profile": "profilo_sempre",
                "mqtt_topic_prefix": "accessi/x",
                "intercom_open_door": True,
                **options,
            }
        )
        self.app = create_app(settings, allow_any_client=True, controller=self.controller, db_path=self.path)
        self.client = TestClient(self.app)
        self.client.__enter__()
        assert wait(lambda: self.app.state.monitor.connected)
        self.controller.door = "Locked"
        return self.client

    def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> None:
        """L'add-on Hikvision pubblica adesso: chi è iscritto lo riceve non conservato."""

        async def send():
            sender = self.broker.client()
            await sender.__aenter__()
            try:
                await sender.publish(topic, payload, retain=retain)
            finally:
                await sender.__aexit__(None, None, None)

        self.client.portal.call(send)

    def announce(self, name: str, serial: str, relays=(1, 2)) -> None:
        """L'add-on ripartito (dopo un cambio di nome nelle sue opzioni) pubblica le configurazioni, conservate."""
        for entity in ("call", *relays):
            self.publish(*config(name, serial, entity), retain=True)

    def subscribed(self, topic: str) -> bool:
        return any(topic in c.filters for c in self.broker.clients)

    def ring_and_open(self, name: str, relay: int = 1) -> None:
        t = topics(name, relay)
        assert wait(lambda: self.subscribed(t["relay"]) and self.subscribed(t["call"]))
        self.publish(t["call"], "ringing")
        self.publish(t["attributes"], json.dumps({"unlock_type": "HOUSEHOLDER", "control_source": "indoor"}))
        self.publish(t["relay"], "ON")
        self.publish(t["relay"], "OFF")

    def rows(self) -> list[tuple[str, str, str, str]]:
        return [(e["kind"], e["detail"], e["source"], e["door"]) for e in reversed(self.app.state.db.list_events())]

    def door(self, token: str = "porta") -> dict:
        return next(d for d in self.app.state.db.list_doors() if d["door_token"] == token)

    def close(self) -> None:
        if self.client is not None:
            self.client.__exit__(None, None, None)


def wait(condition, seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


@pytest.fixture
def site(controller, tmp_path, monkeypatch):
    async def resolve(options, *args, **kwargs):
        return MqttSettings(host="core-mosquitto", port=1883, username="addons", password="x")

    monkeypatch.setattr(main, "resolve_mqtt", resolve)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    plant = Site(controller, tmp_path)
    monkeypatch.setattr(events.aiomqtt, "Client", plant.broker.client)
    yield plant
    plant.close()


def test_17_11_the_door_follows_the_intercom_renamed_in_the_add_on(site, controller):
    """La porta ha ancora il nome vecchio e nessun numero di serie (prima della 0.28.11). Sul broker c'è la
    configurazione conservata del nome vecchio; l'add-on riparte con il nome nuovo e la pubblica adesso. La porta prende
    il numero di serie dal nome vecchio, poi il nome nuovo, e chiamate e aperture sui topic nuovi arrivano."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B)
    site.start(intercom_name=ARTISTI)
    state = site.app.state
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    # Solo il log dell'add-on: niente in Attività
    assert not [a for a in state.db.list_audit() if "videocitofono" in (a["action"] + a["detail"]).lower()]
    [bridge] = state.intercoms
    assert bridge.settings.serial == SERIAL_A

    site.announce(RENAMED, SERIAL_A)
    assert wait(lambda: site.door()["intercom_name"] == RENAMED)
    assert bridge.settings.name == RENAMED and state.doors[0]["intercom_name"] == RENAMED
    site.ring_and_open(RENAMED)
    assert wait(lambda: FORWARDED in controller.calls)
    assert ("citofono", "ha suonato", RENAMED, "porta") in site.rows()
    # Nessuna nuova configurazione: il bridge è lo stesso
    assert state.intercoms == [bridge]


def test_app_started_after_the_rename_follows_the_first_live_message(site, controller):
    """L'app riparte dopo il cambio di nome: sul broker ci sono le due configurazioni, tutte e due conservate, e non si
    sa quale nome usa l'add-on. La porta prende il numero di serie e tiene il suo nome; il primo messaggio dal vivo sui
    topic nuovi dice il nome giusto."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(RENAMED, SERIAL_A)
    site.start(intercom_name=ARTISTI)
    state = site.app.state
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    [device] = state.intercom_directory.devices()
    assert device["names"] == [RENAMED, ARTISTI] and device["name"] == ""
    assert site.door()["intercom_name"] == ARTISTI
    site.ring_and_open(RENAMED)
    assert wait(lambda: FORWARDED in controller.calls)
    assert ("citofono", "ha suonato", ARTISTI, "porta") in site.rows()
    assert wait(lambda: site.door()["intercom_name"] == RENAMED)
    assert state.intercoms[0].settings.name == RENAMED


def test_without_intercoms_the_list_is_there_to_choose_one(site):
    """Nessuna porta con il videocitofono: l'elenco serve per scegliere il primo."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B, relays=(1,))
    client = site.start()
    assert site.app.state.intercoms == []
    assert wait(lambda: len(client.get("/api/intercoms/found").json()["devices"]) == 2)
    found = client.get("/api/intercoms/found").json()
    assert found["broker"] is True and found["since"] is not None
    assert [(d["name"], d["relays"], d["used"]) for d in found["devices"]] == [
        (CARRAIO, [1], []),
        (ARTISTI, [1, 2], []),
    ]


def test_a_door_with_a_name_not_on_the_broker_is_notified(site):
    """Porta di prima con un nome che nessun videocitofono annuncia (come il 17/11): dopo SETTLE la notifica dice quali
    ci sono."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B, relays=(1,))
    client = site.start(intercom_name="Portineria")
    state = site.app.state
    assert wait(lambda: len(state.intercom_directory.devices()) == 2)
    state.intercom_directory.connected_at -= 301
    door = site.door()
    assert door["intercom_serial"] == ""
    [notice] = [i for i in client.get("/api/notifications").json()["items"] if i["type"] == "intercom_missing"]
    assert notice["text"] == f"{door['name']} · Portineria · trovati: {CARRAIO}, {ARTISTI}"
    [row] = client.get("/api/status?controller=false").json()["intercoms"]
    assert row["found"] is False and row["serial"] == ""


def test_a_door_bound_by_serial_still_hears_the_intercom_after_a_rename(site, controller):
    """Porta già legata al numero di serie; sul broker nessuna configurazione del nome della porta. L'add-on cambia
    nome: la porta continua a sentire il videocitofono e prende il nome nuovo."""
    site.retain(CARRAIO, SERIAL_B)
    client = site.start(intercom_name="Artisti")
    door = site.door()
    body = {**door, "intercom_serial": SERIAL_A}
    assert client.put(f"/api/doors/{door['id']}", json=body).status_code == 200
    assert site.door()["intercom_serial"] == SERIAL_A
    site.announce(RENAMED, SERIAL_A)
    assert wait(lambda: site.door()["intercom_name"] == RENAMED)
    site.ring_and_open(RENAMED)
    assert wait(lambda: FORWARDED in controller.calls)
    assert ("citofono", "ha suonato", RENAMED, "porta") in site.rows()


def test_the_other_intercom_never_opens_the_door_even_with_the_old_name(site, controller):
    """L'altro videocitofono (altro numero di serie) non arriva mai alla porta: né con il suo nome, né dopo aver preso
    il nome che la porta aveva prima (nomi scambiati nell'add-on)."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B)
    site.start(intercom_name=ARTISTI)
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    site.announce(RENAMED, SERIAL_A)
    assert wait(lambda: site.door()["intercom_name"] == RENAMED)
    carraio = topics(CARRAIO)
    site.publish(carraio["call"], "ringing")
    site.publish(carraio["relay"], "ON")
    # L'altro videocitofono prende il nome vecchio della porta
    site.announce(ARTISTI, SERIAL_B)
    old = topics(ARTISTI)
    devices = site.app.state.intercom_directory.devices
    assert wait(lambda: [d["name"] for d in devices() if d["serial"] == SERIAL_B] == [ARTISTI])
    site.publish(old["call"], "ringing")
    site.publish(old["attributes"], json.dumps({"unlock_type": "HOUSEHOLDER"}))
    site.publish(old["relay"], "ON")
    time.sleep(0.2)
    assert controller.calls == [] and not [r for r in site.rows() if r[0] == "citofono"]
    assert site.door()["intercom_name"] == RENAMED and site.door()["intercom_serial"] == SERIAL_A
    # Il videocitofono della porta invece sì
    site.ring_and_open(RENAMED)
    assert wait(lambda: FORWARDED in controller.calls)


def test_one_intercom_on_two_doors_after_the_binding_by_serial(site, controller):
    """Stesso videocitofono su due porte (relè 1 e 2): la chiamata la segue solo la prima porta, ogni relè apre la sua,
    anche dopo il numero di serie e il cambio di nome."""
    site.retain(ARTISTI, SERIAL_A)
    # La seconda porta ha un nome vecchio dello stesso videocitofono: prima del numero di serie sembrano due
    site.retain("Artisti Vecchio", SERIAL_A)
    client = site.start(intercom_name=ARTISTI)
    ctrl = site.app.state.db.list_controllers()[0]
    body = {"door_token": "scena", "name": "Scena", "intercom_name": "Artisti Vecchio", "intercom_relay": 2}
    assert client.post(f"/api/controllers/{ctrl['id']}/doors", json=body | {"intercom_open_door": True}).status_code
    assert wait(lambda: site.app.state.monitor.connected and len(site.app.state.intercoms) == 2)
    assert wait(lambda: site.door("scena")["intercom_serial"] == SERIAL_A == site.door()["intercom_serial"])
    site.announce(RENAMED, SERIAL_A)
    assert wait(lambda: site.door("scena")["intercom_name"] == RENAMED == site.door()["intercom_name"])
    first, second = site.app.state.intercoms
    assert second.call is first
    site.ring_and_open(RENAMED, relay=1)
    assert wait(lambda: FORWARDED in controller.calls)
    site.ring_and_open(RENAMED, relay=2)
    assert wait(lambda: ("access_door", "scena", False) in controller.calls)
    assert controller.calls == [FORWARDED, ("access_door", "scena", False)]
    rings = [r for r in site.rows() if r[1] == "ha suonato"]
    assert rings == [("citofono", "ha suonato", RENAMED, "porta")]


def test_new_topics_hold_back_an_open_without_a_call_like_a_reconnection(site, controller):
    """Topic appena seguiti dopo un cambio di nome: un Apri senza chiamata nei primi secondi può essere una pressione
    vecchia (13/11). Con la chiamata si inoltra."""
    site.retain(ARTISTI, SERIAL_A)
    site.start(intercom_name=ARTISTI)
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    # L'app è collegata al broker da molto: il ricollegamento non c'entra
    [bridge] = site.app.state.intercoms
    bridge._back_at = float("-inf")
    site.announce(RENAMED, SERIAL_A)
    assert wait(lambda: site.door()["intercom_name"] == RENAMED)
    new = topics(RENAMED)
    assert wait(lambda: site.subscribed(new["relay"]))
    site.publish(new["relay"], "ON")
    assert wait(lambda: [r for r in site.rows() if "non inoltrata" in r[1]])
    assert controller.calls == []
    site.ring_and_open(RENAMED)
    assert wait(lambda: FORWARDED in controller.calls)


def test_found_list_status_and_notification(site, controller):
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B, relays=(1,))
    client = site.start(intercom_name=ARTISTI, intercom_relay=2)
    state = site.app.state
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    door = site.door()
    found = client.get("/api/intercoms/found").json()
    assert found["broker"] is True and found["since"] is not None and found["since"] < 10
    assert found["devices"] == [
        {
            "serial": SERIAL_B,
            "name": CARRAIO,
            "names": [CARRAIO],
            "model": "DS-KX0000-PROVA",
            "relays": [1],
            "used": [],
        },
        {
            "serial": SERIAL_A,
            "name": ARTISTI,
            "names": [ARTISTI],
            "model": "DS-KX0000-PROVA",
            "relays": [1, 2],
            "used": [{"door_id": door["id"], "door": door["name"], "relay": 2}],
        },
    ]

    def status():
        [row] = client.get("/api/status?controller=false").json()["intercoms"]
        return row

    def notices():
        return [i for i in client.get("/api/notifications").json()["items"] if i["type"] == "intercom_missing"]

    # Appena collegati non si sa ancora
    assert status()["serial"] == SERIAL_A and status()["found"] is None
    state.intercom_directory.connected_at -= 11
    assert status()["found"] is True
    # Prima di SETTLE niente notifica, nemmeno con il videocitofono sparito
    site.publish(config(ARTISTI, SERIAL_A, 2)[0], b"", retain=True)
    assert wait(lambda: status()["found"] is False)
    assert notices() == []
    state.intercom_directory.connected_at -= 300
    [notice] = notices()
    assert notice["title"] == "Videocitofono Non Trovato"
    assert notice["text"] == f"{door['name']} · {ARTISTI} · relè 2 non trovato"
    assert notice["link"] == f"#impianti/c{door['controller_id']}/citofoni"
    # Presa visione: con il broker scollegato la notifica non si valuta, e la presa visione resta (anche dopo i primi
    # minuti dall'avvio, quando le viste dei guasti risolti si dimenticano)
    assert client.post("/api/notifications/seen", json={"ids": [notice["id"]]}).status_code == 200
    state.notifications.settled_at = 0
    state.monitor.connected = False
    assert notices() == [] and client.get("/api/intercoms/found").json() == {
        "broker": False,
        "since": None,
        "devices": [],
    }
    state.monitor.connected = True
    [again] = notices()
    assert again["id"] == notice["id"] and again["seen"] is True
    # Videocitofono tolto del tutto: gli altri annunciati nel testo
    for entity in ("call", 1):
        site.publish(config(ARTISTI, SERIAL_A, entity)[0], b"", retain=True)
    assert wait(lambda: notices() and "trovati" in notices()[0]["text"])
    assert notices()[0]["text"] == f"{door['name']} · {ARTISTI} · trovati: {CARRAIO}"
    # Tornato: la notifica sparisce
    site.announce(ARTISTI, SERIAL_A)
    assert wait(lambda: notices() == [])
    assert status()["found"] is True
