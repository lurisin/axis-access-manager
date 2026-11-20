"""Videocitofoni annunciati sul broker dall'add-on Hikvision Doorbell (0.28.11): configurazioni di discovery lette,
raggruppate per numero di serie, nome dal vivo e numero di serie della porta nel database e nei backup. Nomi e numeri
di serie di fantasia, con la struttura di quelli veri (codici ASCII di modello e numero di serie, 76 cifre)."""

import asyncio
import json
import sqlite3

import aiomqtt
import pytest

from app.db import LEGACY_COLUMNS, Database
from app.events import EventMonitor
from app.installation import InstallationService
from app.intercom import topic_name
from app.intercom_directory import IntercomDirectory, discovery_filters, shown_name
from app.notifications import TYPES, NotificationService
from app.restore import Runtime, apply, plan
from app.service import ServiceError

ARTISTI = "Ingresso Artisti - Videocitofono"
CARRAIO = "Cancello Carraio"
SERIAL_A = "6883457588484848484580827986654065414848504850534854484965666768484848484855"
SERIAL_B = "6883457588484848484580827986664066414848504850534854484969707172484848484857"


class Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


def config(name, serial, entity="call", *, identifiers=None, manufacturer="Hikvision", unique_id=None, obj=None):
    """Configurazione di discovery come la pubblica l'add-on Hikvision Doorbell: (topic, payload)."""
    clean = topic_name(name)
    if entity == "call":
        component, obj, uid = "sensor", obj or "Call-state", f"{serial}-call_state"
    else:
        component, obj, uid = "switch", obj or f"Door-{entity}-relay", f"{serial}-door_relay_{entity - 1}"
    base = f"hmd/{component}/{clean}/{obj}"
    payload = {
        "component": component,
        "device": {
            "name": name,
            "model": "DS-KX0000-PROVA",
            "manufacturer": manufacturer,
            "sw_version": "V1.0.0",
            "hw_version": "1-1-0",
            "identifiers": serial if identifiers is None else identifiers,
        },
        "name": "Call state" if entity == "call" else f"Door {entity} relay",
        "unique_id": unique_id or uid,
        "state_topic": f"{base}/state",
        "json_attributes_topic": f"{base}/attributes",
        "availability_topic": f"{base}/availability",
    }
    if component == "switch":
        payload |= {"command_topic": f"{base}/command", "payload_on": "ON", "payload_off": "OFF"}
    return f"homeassistant/{component}/{clean}/{obj}/config", json.dumps(payload).encode()


def announce(directory, name, serial, *, retained=True, relays=(1, 2)):
    for entity in ("call", *relays):
        directory.handle_config(*config(name, serial, entity), retained)


def test_configs_are_grouped_by_serial_with_string_or_list_identifiers():
    directory = IntercomDirectory(Clock())
    announce(directory, ARTISTI, SERIAL_A)
    # L'identificativo può essere anche un elenco: vale il primo
    for entity in ("call", 1):
        directory.handle_config(*config(CARRAIO, SERIAL_B, entity, identifiers=[SERIAL_B, "altro"]), True)
    [carraio, artisti] = directory.devices()
    assert artisti["serial"] == SERIAL_A and artisti["names"] == [ARTISTI] and artisti["relays"] == [1, 2]
    assert artisti["model"] == "DS-KX0000-PROVA"
    assert artisti["call"]["state"] == ["hmd/sensor/Ingresso-Artisti---Videocitofono/Call-state/state"]
    assert artisti["relay_topics"][2]["state"] == ["hmd/switch/Ingresso-Artisti---Videocitofono/Door-2-relay/state"]
    assert carraio["serial"] == SERIAL_B and carraio["relays"] == [1]
    # Solo configurazioni conservate: il nome dal vivo non si sa
    assert artisti["name"] == "" and shown_name(artisti) == ARTISTI


def test_other_devices_com_relays_and_removed_configs_are_left_out():
    directory = IntercomDirectory(Clock())
    announce(directory, ARTISTI, SERIAL_A)
    directory.handle_config(*config(CARRAIO, SERIAL_B, manufacturer="Altra Marca"), True)
    directory.handle_config(*config(CARRAIO, SERIAL_B, 1, unique_id=f"{SERIAL_B}-com_relay_0"), True)
    directory.handle_config(*config(CARRAIO, SERIAL_B, 1, obj="Com-1-relay"), True)
    directory.handle_config("homeassistant/switch/presa_cucina/presa/config", b'{"device": {"name": "Presa"}}', True)
    directory.handle_config("homeassistant/switch/x/Door-1-relay/config", b"non json", True)
    # Topic di stato con + o #: mai un filtro al posto di un topic
    topic, payload = config(CARRAIO, SERIAL_B)
    directory.handle_config(topic, payload.replace(b"Call-state/state", b"#"), True)
    assert [d["serial"] for d in directory.devices()] == [SERIAL_A]
    # Configurazione vuota: tolta
    directory.handle_config(config(ARTISTI, SERIAL_A, 2)[0], b"", True)
    assert directory.devices()[0]["relays"] == [1]


def test_after_a_rename_the_live_name_wins_and_retained_only_keeps_the_stored_one():
    clock = Clock()
    directory = IntercomDirectory(clock)
    changes = []
    directory.on_change = lambda: changes.append(1)
    announce(directory, ARTISTI, SERIAL_A)
    announce(directory, "Ingresso Artisti", SERIAL_A)
    [device] = directory.devices()
    # Due configurazioni per lo stesso numero di serie (l'add-on non toglie mai quelle vecchie): nessun nome vince
    assert device["names"] == ["Ingresso Artisti", ARTISTI] and device["name"] == ""
    assert shown_name(device, ARTISTI) == ARTISTI and shown_name(device, "Altro") == "Ingresso Artisti"
    assert changes
    # Un messaggio di stato dal vivo sui topic del nome vecchio: vince lui
    clock.now += 5
    directory.mark_live("hmd/sensor/Ingresso-Artisti---Videocitofono/Call-state/state")
    assert directory.devices()[0]["name"] == ARTISTI
    # La configurazione pubblicata adesso (add-on ripartito con il nome nuovo): vince il nome nuovo
    clock.now += 5
    directory.handle_config(*config("Ingresso Artisti", SERIAL_A), False)
    assert directory.devices()[0]["name"] == "Ingresso Artisti"
    # Ripubblicata conservata (ricollegamento): la prova dal vivo resta
    directory.handle_config(*config("Ingresso Artisti", SERIAL_A), True)
    assert directory.devices()[0]["name"] == "Ingresso Artisti"


def test_after_a_reconnect_only_new_live_evidence_renames_but_the_last_live_name_is_shown():
    """Dopo un ricollegamento le configurazioni conservate tornano una alla volta, prima quella del nome vecchio: la
    prova dal vivo di prima non decide più il nome (la porta non torna al nome vecchio), ma per mostrarlo vale ancora
    l'ultimo visto dal vivo, non il primo in ordine alfabetico."""
    clock = Clock()
    directory = IntercomDirectory(clock)
    directory.connected()
    announce(directory, "Artisti Ingresso", SERIAL_A)
    clock.now += 5
    announce(directory, "Ingresso Artisti", SERIAL_A, retained=False)
    assert directory.devices()[0]["name"] == "Ingresso Artisti"
    clock.now += 60
    directory.connected()
    announce(directory, "Artisti Ingresso", SERIAL_A)
    announce(directory, "Ingresso Artisti", SERIAL_A)
    [device] = directory.devices()
    assert device["name"] == "" and device["known"] == "Ingresso Artisti"
    assert shown_name(device) == "Ingresso Artisti" and shown_name(device, "Artisti Ingresso") == "Artisti Ingresso"


def test_quiet_for_counts_from_the_last_config():
    """Le porte con il solo nome si legano quando le configurazioni conservate sono arrivate tutte: si conta
    dall'ultima arrivata, non dal collegamento (le pubblicazioni per Home Assistant possono venire prima)."""
    clock = Clock()
    directory = IntercomDirectory(clock)
    assert directory.quiet_for() is None
    directory.connected()
    clock.now += 3
    assert directory.quiet_for() == 3
    announce(directory, ARTISTI, SERIAL_A)
    clock.now += 0.5
    assert directory.quiet_for() == 0.5
    # Anche una configurazione che non è di un videocitofono vuol dire che stanno ancora arrivando
    directory.handle_config("homeassistant/switch/Luce-Scale/Door-1-relay/config", b'{"device": {}}')
    assert directory.quiet_for() == 0
    directory.disconnected()
    assert directory.quiet_for() is None


async def test_a_retried_subscription_ends_with_the_connection():
    """Un'iscrizione che si riprova non è un'operazione sul controller: non fa aspettare lo spegnimento, e finisce
    quando il collegamento si perde (al prossimo si chiedono tutte)."""

    class Client:
        def __init__(self):
            self.asked = []

        async def subscribe(self, topic_filter, qos=0):
            self.asked.append(topic_filter)
            raise aiomqtt.MqttError("Operation timed out")

    monitor = EventMonitor(prefix="accessi/x")
    monitor.client, monitor.connected = Client(), True
    assert monitor.add_subscription("hmd/switch/Nuovo/Door-1-relay/state", lambda *a: None)
    await asyncio.sleep(0.05)
    assert monitor._tasks == set() and len(monitor._subscribing) == 1
    [task] = monitor._subscribing
    monitor._lost()
    await asyncio.sleep(0.01)
    assert task.cancelled() and monitor._subscribing == set() and len(monitor.client.asked) == 1


def test_only_configs_from_the_current_connection_count():
    clock = Clock()
    directory = IntercomDirectory(clock)
    announce(directory, ARTISTI, SERIAL_A)
    assert directory.since() is None
    clock.now += 1
    directory.connected()
    # Tolta mentre l'app era scollegata: il broker non la manda più, e non c'è
    assert directory.devices() == [] and directory.since() == 0
    announce(directory, CARRAIO, SERIAL_B)
    clock.now += 12
    assert [d["serial"] for d in directory.devices()] == [SERIAL_B] and directory.since() == 12
    directory.disconnected()
    # Scollegati: valgono quelle dell'ultimo collegamento
    assert directory.since() is None and [d["serial"] for d in directory.devices()] == [SERIAL_B]


def test_discovery_filters_take_the_configs_of_the_hikvision_add_on():
    call, relay = discovery_filters("homeassistant")
    assert aiomqtt.Topic(config(ARTISTI, SERIAL_A)[0]).matches(call)
    assert aiomqtt.Topic(config(ARTISTI, SERIAL_A, 2)[0]).matches(relay)


def test_a_subscription_added_while_dispatching_does_not_stop_the_dispatch():
    """Un videocitofono riconosciuto mentre si smista un messaggio aggiunge altri topic da seguire: prima il giro sui
    filtri si fermava con «dictionary changed size during iteration»."""
    monitor = EventMonitor(prefix="accessi/x")
    seen = []

    def first(topic, payload, retained):
        seen.append("primo")
        for n in range(5):
            monitor.add_subscription(f"hmd/switch/Altro/Door-{n}-relay/state", lambda *a: None)

    monitor.extra_subscriptions["hmd/sensor/+/Call-state/state"] = first
    monitor.extra_subscriptions["hmd/sensor/#"] = lambda t, p, r: seen.append("secondo")
    assert monitor._dispatch_extra(aiomqtt.Topic("hmd/sensor/Citofono/Call-state/state"), b"ringing")
    assert seen == ["primo", "secondo"] and len(monitor.extra_subscriptions) == 7
    # Un filtro già seguito resta a chi lo segue
    assert monitor.add_subscription("hmd/sensor/#", first) is False


async def test_a_live_subscription_the_broker_does_not_answer_is_asked_again(monkeypatch):
    """Il broker lento non risponde in tempo all'iscrizione ai topic di un videocitofono appena riconosciuto e resta
    collegato: senza un nuovo tentativo la porta non sentirebbe il videocitofono fino al prossimo collegamento."""
    from app import events

    monkeypatch.setattr(events, "SUBSCRIBE_RETRY", (0.01, 0.01))

    class Client:
        def __init__(self, failures):
            self.failures, self.asked = failures, []

        async def subscribe(self, topic_filter, qos=0):
            self.asked.append(topic_filter)
            if len(self.asked) <= self.failures:
                raise aiomqtt.MqttError("Operation timed out")

    monitor = EventMonitor(prefix="accessi/x")
    monitor.client, monitor.connected = Client(failures=1), True
    assert monitor.add_subscription("hmd/switch/Nuovo/Door-1-relay/state", lambda *a: None)
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert monitor.client.asked == ["hmd/switch/Nuovo/Door-1-relay/state"] * 2
    # Mai più di così; e con il collegamento perso si smette (al prossimo si chiedono tutti)
    monitor.client = Client(failures=9)
    assert monitor.add_subscription("hmd/switch/Nuovo/Door-2-relay/state", lambda *a: None)
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert len(monitor.client.asked) == 3
    monitor.client = lost = Client(failures=9)
    assert monitor.add_subscription("hmd/switch/Nuovo/Door-3-relay/state", lambda *a: None)
    monitor.connected = False
    for _ in range(50):
        await asyncio.sleep(0.01)
    assert len(lost.asked) == 1


async def test_the_monitor_says_when_the_broker_is_lost_or_the_listening_stops(monkeypatch):
    """La lista dei videocitofoni sa da quando l'app è collegata: anche quando il collegamento si perde o l'ascolto si
    ferma per una nuova configurazione."""
    import asyncio

    from app import events
    from app.config import MqttSettings

    class Client:
        attempts = 0

        def __init__(self, **options):
            pass

        async def __aenter__(self):
            Client.attempts += 1
            return self

        async def __aexit__(self, *exc):
            return False

        async def subscribe(self, topic_filter, qos=0):
            pass

        @property
        async def messages(self):
            if Client.attempts == 1:
                raise aiomqtt.MqttError("broker perso")
            await asyncio.Event().wait()
            yield

    monkeypatch.setattr(events.aiomqtt, "Client", Client)
    monkeypatch.setattr(events, "RECONNECT_DELAY", 0)
    directory = IntercomDirectory()
    monitor = EventMonitor(prefix="accessi/x")
    calls = []

    async def connected(client):
        calls.append("collegato")
        directory.connected()

    def lost():
        calls.append("perso")
        directory.disconnected()

    monitor.on_connect, monitor.on_disconnect = connected, lost
    task = asyncio.create_task(monitor.run(MqttSettings(host="broker.invalid")))
    for _ in range(100):
        if calls == ["collegato", "perso", "collegato"]:
            break
        await asyncio.sleep(0.01)
    assert calls == ["collegato", "perso", "collegato"] and directory.since() is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls[-1] == "perso" and directory.since() is None


# Notifica


def missing(**fields):
    return {
        "door_id": 3,
        "controller_id": 1,
        "door": "Ingresso Artisti",
        "door_token": "porta",
        "name": ARTISTI,
        "relay": None,
        "found": [],
        "since": "2026-11-20T09:00:00+00:00",
    } | fields


def collect(db, intercoms_missing):
    return NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={},
        door_names={"porta": "Ingresso Artisti"},
        intercoms_missing=intercoms_missing,
    )


def test_notification_texts(db):
    items = collect(
        db,
        [
            missing(),
            missing(door_id=4, door="Carraio", found=[CARRAIO, "Ingresso Artisti"]),
            missing(door_id=5, door="Scala", relay=2),
        ],
    )
    rows = {i["id"].split("@")[0]: (i["title"], i["text"], i["severity"], i["link"]) for i in items}
    assert rows == {
        "intercom_missing:3": (
            "Videocitofono Non Trovato",
            f"Ingresso Artisti · {ARTISTI} · nessun videocitofono annunciato",
            "warn",
            "#impianti/c1/citofoni",
        ),
        "intercom_missing:4": (
            "Videocitofono Non Trovato",
            f"Carraio · {ARTISTI} · trovati: {CARRAIO}, Ingresso Artisti",
            "warn",
            "#impianti/c1/citofoni",
        ),
        "intercom_missing:5": (
            "Videocitofono Non Trovato",
            f"Scala · {ARTISTI} · relè 2 non trovato",
            "warn",
            "#impianti/c1/citofoni",
        ),
    }
    assert all(i["fault"] for i in items)
    assert TYPES["intercom_missing"][0] == "Videocitofoni Non Trovati"


def test_plain_notification_does_not_name_the_broker(db):
    from app.plain import forbidden

    service = NotificationService(db)
    current = collect(db, [missing(found=[CARRAIO])])
    listing = service.listing(current, {"stato"}, plain=True)
    [item] = [i for i in listing["items"] if i["type"] == "intercom_missing"]
    assert item["text"] == f"Ingresso Artisti · {ARTISTI}. Avvisa l'amministratore dell'impianto."
    assert not forbidden(item["title"]) and not forbidden(item["text"])
    [kind] = [t for t in listing["types"] if t["key"] == "intercom_missing"]
    assert not forbidden(kind["name"]) and not forbidden(kind["description"])


# Database e backup


DOOR = {
    "door_token": "porta",
    "name": "Ingresso Artisti",
    "snapshot_camera": "",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": ARTISTI,
    "intercom_open_door": 1,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}


def test_a_database_without_the_serial_column_gets_it_and_saves_doors(tmp_path):
    """Database già allo schema 10 (0.28.10): la colonna si aggiunge all'avvio, mai con LEGACY_COLUMNS (0.28.8)."""
    assert not [c for c in LEGACY_COLUMNS if c[1] == "intercom_serial"]
    path = tmp_path / "vecchio.db"
    db = Database(path)
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    door_id = db.save_door(None, controller_id, DOOR)
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE doors DROP COLUMN intercom_serial")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version

    db = Database(path)
    [door] = db.list_doors()
    assert door["intercom_serial"] == "" and door["intercom_name"] == ARTISTI
    db.save_door(door_id, controller_id, {**door, "intercom_serial": SERIAL_A})
    db.save_door(None, controller_id, {**DOOR, "door_token": "carraio", "name": "Carraio"})
    db.close()
    db = Database(path)
    saved = {d["door_token"]: d for d in db.list_doors()}
    assert saved["porta"]["intercom_serial"] == SERIAL_A and saved["carraio"]["intercom_serial"] == ""
    db.close()


@pytest.fixture
def installation(db):
    service = InstallationService(db)
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    return service, controller_id


def test_saving_a_door_checks_and_keeps_the_serial(installation, db):
    service, controller_id = installation
    door_id = service.save_door(None, controller_id, {**DOOR, "intercom_serial": f" {SERIAL_A} "}, "admin")["id"]
    assert db.get_door(door_id)["intercom_serial"] == SERIAL_A
    # La pagina di prima non manda il numero di serie: con lo stesso nome resta
    service.save_door(door_id, controller_id, {**DOOR, "name": "Artisti"}, "admin")
    assert db.get_door(door_id)["intercom_serial"] == SERIAL_A
    # Con un altro nome no: si ritrova dal nome
    service.save_door(door_id, controller_id, {**DOOR, "intercom_name": CARRAIO}, "admin")
    assert db.get_door(door_id)["intercom_serial"] == ""
    service.save_door(door_id, controller_id, {**DOOR, "intercom_serial": SERIAL_A}, "admin")
    # Videocitofono tolto: niente numero di serie e niente inoltro
    service.save_door(door_id, controller_id, {**DOOR, "intercom_name": "", "intercom_serial": ""}, "admin")
    door = db.get_door(door_id)
    assert (door["intercom_name"], door["intercom_serial"], door["intercom_open_door"]) == ("", "", False)
    service.save_door(door_id, controller_id, {**DOOR, "intercom_serial": SERIAL_A}, "admin")
    service.save_door(door_id, controller_id, {**DOOR, "intercom_name": ""}, "admin")
    assert db.get_door(door_id)["intercom_serial"] == ""
    with pytest.raises(ServiceError, match="scegli il videocitofono"):
        service.save_door(door_id, controller_id, {**DOOR, "intercom_name": "", "intercom_serial": SERIAL_B}, "admin")
    for wrong in ("6883 4575", "68/83", "6" * 129):
        with pytest.raises(ServiceError, match="numero di serie del videocitofono non valido"):
            service.save_door(door_id, controller_id, {**DOOR, "intercom_serial": wrong}, "admin")
    # 76 cifre, come sull'impianto
    service.save_door(door_id, controller_id, {**DOOR, "intercom_serial": SERIAL_B}, "admin")
    assert db.get_door(door_id)["intercom_serial"] == SERIAL_B


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    from test_backup import Plant

    return Plant(db, controller, settings, tmp_path / "backup")


async def test_backup_and_restore_keep_the_serial(plant, db):
    door = db.get_door(plant.scala)
    db.save_door(plant.scala, plant.controller_id, {**door, "intercom_name": ARTISTI, "intercom_serial": SERIAL_A})
    backup = await plant.backup()
    db.save_door(plant.scala, plant.controller_id, {**db.get_door(plant.scala), "intercom_serial": SERIAL_B})
    items, result = await plant.restore(backup["id"], ["porte"])
    [item] = [i for i in items if i["key"] == "porta:porta"]
    assert [c["label"] for c in item["changes"]] == ["Numero di Serie del Videocitofono"]
    assert not result["failed"] and db.get_door(plant.scala)["intercom_serial"] == SERIAL_A


async def test_backup_from_before_the_serial_keeps_the_current_one(plant, db):
    """Un backup di prima della 0.28.11 non ha il numero di serie: non è una differenza, e il ripristino lascia quello
    di adesso se il videocitofono è lo stesso; con un altro nome lo toglie (si ritrova dal nome)."""
    door = db.get_door(plant.scala)
    db.save_door(plant.scala, plant.controller_id, {**door, "intercom_name": ARTISTI, "intercom_relay": 2})
    backup = await plant.backup()
    _, saved = plant.backups.load(backup["id"])
    for part in saved.values():
        for row in part.get("app_doors", []):
            row.pop("intercom_serial")
    db.save_door(
        plant.scala,
        plant.controller_id,
        {**db.get_door(plant.scala), "intercom_serial": SERIAL_A, "intercom_relay": 1},
    )
    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = [i for i in plan(saved, current, ["porte"], plant.controller_id) if i["key"] == "porta:porta"]
    assert [c["label"] for c in item["changes"]] == ["Relè del Tasto Apri"]
    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    assert not (await apply([item], [item["key"]], runtime, "admin"))["failed"]
    door = db.get_door(plant.scala)
    assert (door["intercom_relay"], door["intercom_serial"]) == (2, SERIAL_A)

    # Backup di prima con un altro videocitofono: il numero di serie di adesso non è suo
    db.save_door(plant.scala, plant.controller_id, {**door, "intercom_name": CARRAIO, "intercom_serial": SERIAL_B})
    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = [i for i in plan(saved, current, ["porte"], plant.controller_id) if i["key"] == "porta:porta"]
    assert not (await apply([item], [item["key"]], runtime, "admin"))["failed"]
    door = db.get_door(plant.scala)
    assert (door["intercom_name"], door["intercom_serial"]) == (ARTISTI, "")
