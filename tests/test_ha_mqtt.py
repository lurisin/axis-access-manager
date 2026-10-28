import asyncio
import dataclasses
import json
import time

import pytest

from app.config import parse_options
from app.events import EventMonitor
from app.ha_mqtt import HaBridge, seconds_to_midnight, topic_base


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
    # Pulsante negato, senza tessera né persona: evento sì, ultimo accesso no
    await bridge.publish_event({"kind": "negato", "source": "Relè Citofono", "ts": "t2"})

    assert sent[0][0] == "axis_access_manager/evento"
    assert sent[0][1]["event_type"] == "accesso_negato"
    assert sent[1] == (
        "axis_access_manager/ultimo",
        {"persona": "Anna", "tessera": "04aa", "esito": "accesso_negato", "dettaglio": "", "ora": "t"},
        True,
    )
    assert sent[2][0] == "axis_access_manager/evento" and sent[2][1]["origine"] == "Relè Citofono"
    assert len(sent) == 3 and bridge.last_access["persona"] == "Anna"


async def test_last_access_survives_restart(db, settings):
    # Dopo un riavvio il sensore riparte dall'ultimo passaggio del registro, non da "nessuno"
    db.add_event(ts="2026-10-25T08:00:00+00:00", kind="concesso", person_name="Anna", card="04aa")
    db.add_event(ts="2026-10-25T08:05:00+00:00", kind="apertura_remota")
    db.add_event(ts="2026-10-25T08:06:00+00:00", kind="citofono", detail="ha suonato")
    bridge = HaBridge(settings, EventMonitor(prefix="accessi/x", door_token="porta"), db)
    client = FakeClient()
    await bridge.on_connect(client)
    last = json.loads(client.published["axis_access_manager/ultimo"])
    assert last["persona"] == "Apertura Remota" and last["esito"] == "apertura_remota"


def test_second_door_gets_its_own_entities_and_commands(settings, db):
    from app.events import EventMonitor
    from app.ha_mqtt import HaBridge, parse_command

    doors = [
        {"id": 1, "door_token": "porta", "name": "Ingresso", "ha_buttons": True},
        {"id": 2, "door_token": "porta2", "name": "Cantina", "ha_buttons": True},
    ]
    monitor = EventMonitor(prefix="x", door_token="porta")
    monitor.door_modes = {"porta": "Locked", "porta2": "Accessed"}
    bridge = HaBridge(settings, monitor, db, doors)
    entities = {object_id: config for _, object_id, config in bridge._entities()}
    assert entities["apri_porta"]["payload_press"] == "apri"
    assert entities["apri_porta_a_lungo_2"]["name"] == "Apri Cantina a Lungo"
    assert entities["apri_porta_2"]["payload_press"] == "apri:porta2"
    assert "porte['porta2']" in entities["stato_porta_2"]["value_template"]
    assert bridge.state()["porte"] == {"porta": "Bloccata", "porta2": "Aperta a Tempo"}
    assert parse_command("apri_lungo:porta2") == (True, "porta2")
    assert parse_command("apri") == (False, "")
    assert parse_command("sblocca") is None


def test_old_entity_ids_stay_with_their_door(settings, db):
    def buttons(doors, first_token):
        bridge = HaBridge(dataclasses.replace(settings, door_token=first_token), EventMonitor(prefix="x"), db, doors)
        return {
            o: c["payload_press"] for _, o, c in bridge._entities() if o.startswith("apri_porta") and "lungo" not in o
        }

    ingresso = {"id": 1, "door_token": "porta", "name": "Ingresso", "ha_buttons": True}
    cantina = {"id": 2, "door_token": "porta2", "name": "Cantina", "ha_buttons": True}
    assert buttons([ingresso, cantina], "porta") == {"apri_porta": "apri", "apri_porta_2": "apri:porta2"}
    # Controller tolto e aggiunto con le porte in un altro ordine: Apri Porta apre ancora l'ingresso
    readded = [{**cantina, "id": 3}, {**ingresso, "id": 4}]
    assert buttons(readded, "porta2") == {"apri_porta": "apri:porta", "apri_porta_3": "apri:porta2"}
    # Token nuovo dal Ripristino Completo: stessa porta
    db.replace_token("porta", "Axis-nuovo")
    assert buttons([{**ingresso, "door_token": "Axis-nuovo"}], "Axis-nuovo") == {"apri_porta": "apri"}
    # Porta tolta dall'app: i vecchi identificativi non passano a un'altra porta
    assert buttons([cantina], "porta2") == {"apri_porta_2": "apri:porta2"}


async def test_controller_and_door_condition_entities(bridge):
    client = FakeClient()
    monitor = bridge.monitor
    monitor.controller_online = True
    monitor.door_conditions["porta"] = {"allarme": {"active": True, "state": "DoorForcedOpen", "ts": "t"}}
    monitor.io_ports["3"] = False
    await bridge.on_connect(client)

    def config(component, object_id):
        return json.loads(client.published[f"homeassistant/{component}/axis_access_manager/{object_id}/config"])

    assert config("binary_sensor", "controller_online")["device_class"] == "connectivity"
    assert config("binary_sensor", "allarme_porta")["name"] == "Allarme Porta"
    assert config("binary_sensor", "manomissione_porta")["device_class"] == "tamper"
    assert config("binary_sensor", "ingresso_allarme")["name"] == "Ingresso Allarme"
    assert config("sensor", "ultimo_avvio_controller")["device_class"] == "timestamp"
    lock = config("binary_sensor", "elettroserratura_porta")
    assert lock["name"] == "Elettroserratura Porta" and lock["device_class"] == "lock"
    assert lock["value_template"] == "{{ value_json.serrature['porta'] }}"

    state = json.loads(client.published["axis_access_manager/stato"])
    assert state["controller"] == {"online": "ON", "coperchio": "None", "avvio": "None"}
    assert state["ingressi"] == {"0": "None", "1": "None", "2": "None", "3": "OFF"}
    # Un AUX collegato come pulsante arriva al contrario: 1 a riposo, 0 premuto (dopo il primo cambio)
    controller_id = bridge.db.save_controller(
        None, {f: "" for f in bridge.db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0}
    )
    bridge.db.save_controller_input(controller_id, "IO1", {"name": "Pulsante Portineria", "role": "rex"})
    monitor.io_ports["0"] = True
    assert bridge.state()["ingressi"]["0"] == "OFF"
    monitor.io_changed.add("0")
    monitor.io_ports["0"] = False
    assert bridge.state()["ingressi"]["0"] == "ON"
    # Classe lock: acceso = sbloccata, spento = bloccata, sconosciuta senza dati
    assert state["serrature"] == {"porta": "None"}
    for mode, value in (("Locked", "OFF"), ("Accessed", "ON"), ("Unlocked", "ON"), ("LockedDown", "OFF")):
        monitor.door_modes["porta"] = mode
        assert bridge.state()["serrature"] == {"porta": value}
    # Contatto porta (classe door): acceso = aperta, sconosciuto senza contatto o con un guasto
    contact = config("binary_sensor", "contatto_porta")
    assert contact["name"] == "Contatto Porta" and contact["device_class"] == "door"
    assert contact["value_template"] == "{{ value_json.contatti['porta'] }}"
    assert state["contatti"] == {"porta": "None"}
    for physical, value in (("Open", "ON"), ("Closed", "OFF"), ("Fault", "None")):
        monitor.door_physical["porta"] = physical
        assert bridge.state()["contatti"] == {"porta": value}
    porta = state["condizioni"]["porta"]
    assert porta["allarme"] == {"stato": "ON", "tipo": "Porta Forzata", "ultimo_cambio": "t"}
    assert porta["preallarme"]["stato"] == "None"


async def test_input_entities_take_the_name_chosen_in_the_app(bridge, db):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    db.save_controller_input(controller_id, "IO1", {"name": "Relè Citofono", "role": "intercom", "door_token": "porta"})
    db.save_controller_input(controller_id, "REX", {"name": "Pulsante Portineria", "role": "rex"})
    client = FakeClient()
    await bridge.on_connect(client)

    def config(object_id):
        return json.loads(client.published[f"homeassistant/binary_sensor/axis_access_manager/{object_id}/config"])

    # Stesso id dell'entità (storico e automazioni restano), nome nuovo
    assert config("ingresso_aux_1")["name"] == "Relè Citofono"
    assert config("ingresso_aux_2")["name"] == "Ingresso AUX 2"


async def test_renaming_an_input_republishes_only_input_entities(bridge, db, monkeypatch):
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, json.loads(payload)["name"]))
        return True

    monkeypatch.setattr(bridge.monitor, "publish", publish)
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    db.save_controller_input(controller_id, "IO2", {"name": "Pulsante Magazzino", "role": "rex", "door_token": "porta"})
    await bridge.publish_input_names()
    # Niente riconnessione: lo stato della porta e le altre entità non passano da "sconosciuto"
    assert len(sent) == 4 and all("/ingresso_" in topic for topic, _ in sent)
    assert ("homeassistant/binary_sensor/axis_access_manager/ingresso_aux_2/config", "Pulsante Magazzino") in sent


async def test_stale_discovery_configs_are_cleared(db, settings, monkeypatch):
    # Seconda porta tolta dall'app: le sue entità conservate dal broker si svuotano alla connessione
    monitor = EventMonitor(prefix="accessi/x", door_token="porta")
    HaBridge(settings, monitor, db, [{"id": 1, "door_token": "porta", "name": "Porta", "ha_buttons": True}])
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, payload, retain))
        return True

    monkeypatch.setattr(monitor, "publish", publish)
    handler = monitor.extra_subscriptions["homeassistant/+/axis_access_manager/+/config"]
    handler("homeassistant/button/axis_access_manager/apri_porta_2/config", b"{}", True)
    handler("homeassistant/sensor/axis_access_manager/stato_porta_2/config", b"{}", True)
    handler("homeassistant/button/axis_access_manager/apri_porta/config", b"{}", True)
    # Pubblicate adesso (non conservate) o già vuote: niente da fare
    handler("homeassistant/button/axis_access_manager/apri_porta_3/config", b"{}", False)
    handler("homeassistant/button/axis_access_manager/apri_porta_4/config", b"", True)
    await asyncio.sleep(0)
    assert sorted(sent) == [
        ("homeassistant/button/axis_access_manager/apri_porta_2/config", b"", True),
        ("homeassistant/sensor/axis_access_manager/stato_porta_2/config", b"", True),
    ]


async def test_device_and_topics_stay_stable(db, settings, monkeypatch):
    first = HaBridge(settings, EventMonitor(prefix="accessi/x", door_token="porta"), db)
    device = first._device()["identifiers"]
    # Token della prima porta cambiato: stesso dispositivo in Home Assistant
    other = HaBridge(dataclasses.replace(settings, door_token="nuova"), EventMonitor(prefix="accessi/x"), db)
    assert other._device()["identifiers"] == device == ["axis_access_manager_porta"]
    # Stesso prefisso del controller: lo stato non può finire sul topic online/offline del controller
    same = HaBridge(settings, EventMonitor(prefix="axis_access_manager"), db)
    assert same.state_topic == "axis_access_manager/ha/stato"
    assert HaBridge(settings, EventMonitor(prefix="accessi"), db).state_topic == "axis_access_manager/stato"
    assert topic_base("accessi/eventi", "accessi") == "axis_access_manager"
    # Allo spegnimento le entità diventano non disponibili: il broker scarta il messaggio di addio
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, payload, retain))
        return True

    monkeypatch.setattr(first.monitor, "publish", publish)
    first.start()
    await first.stop()
    assert sent == [("axis_access_manager/disponibile", "offline", True)]
    assert first._midnight is None


def test_seconds_to_midnight():
    noon = time.mktime((2026, 6, 10, 12, 0, 0, 0, 0, -1))
    assert seconds_to_midnight(noon) == 12 * 3600 + 1
