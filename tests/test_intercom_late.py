"""Videocitofono con messaggi in ritardo: il 13/11 sera una pressione già aperta dal controller è stata inoltrata di
nuovo all'A1210, e la porta si è aperta una seconda volta senza che nessuno l'avesse chiesto.

In quell'impianto il relè del citofono è cablato sull'ingresso REX dell'A1210 («Relè Citofono»): il controller apre
da solo, anche con Home Assistant spento. Orologio finto e messaggi MQTT finti, passati dal monitor come quelli veri;
solo il FakeController, mai una porta vera. Nomi di fantasia."""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone

import aiomqtt
import pytest

from app.events import Event, EventMonitor
from app.intercom import IntercomBridge, IntercomSettings
from app.notifications import NotificationService
from app.service import AccessService

CALL = "hmd/sensor/Citofono-Cortile/Call-state/state"
CALL_AVAILABILITY = "hmd/sensor/Citofono-Cortile/Call-state/availability"
RELAY = "hmd/switch/Citofono-Cortile/Door-1-relay/state"
RELAY_ATTRIBUTES = "hmd/switch/Citofono-Cortile/Door-1-relay/attributes"
RELAY_AVAILABILITY = "hmd/switch/Citofono-Cortile/Door-1-relay/availability"
# Punto di accesso dell'ingresso REX, ruolo Relè Citofono
REX_POINT = "ap-citofono"
# Il 13/11 l'Italia è un'ora avanti rispetto a UTC
ITALIA = timezone(timedelta(hours=1))
FORWARDED = ("access_door", "porta", False)
ALREADY_OPEN = "apertura dal citofono non inoltrata: la porta l'ha già aperta il controller dal Relè Citofono (REX)"


class Clock:
    """Orologio finto: quello monotono del bridge e l'ora del controller vanno avanti insieme."""

    def __init__(self, local: str):
        self.start = datetime.fromisoformat(f"2026-11-13T{local}").replace(tzinfo=ITALIA)
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return (self.start + timedelta(seconds=self.now - 1000.0)).astimezone(UTC)

    def at(self, local: str) -> None:
        """Avanti fino all'ora locale del 13/11 (hh:mm:ss)."""
        target = datetime.fromisoformat(f"2026-11-13T{local}").replace(tzinfo=ITALIA)
        seconds = (target - self.wall()).total_seconds()
        assert seconds >= 0, "l'orologio non torna indietro"
        self.now += seconds

    def iso(self) -> str:
        return self.wall().isoformat(timespec="seconds")


def utc(local: str) -> str:
    """Ora locale del 13/11 come la scrive il registro del controller (UTC)."""
    moment = datetime.fromisoformat(f"2026-11-13T{local}").replace(tzinfo=ITALIA)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_entry(rowid: int, local: str) -> dict:
    """Pressione del relè del citofono sull'ingresso REX nel registro interno del controller: un consenso."""
    keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(("AccessControl", "AccessGranted", "Anonymous"))]
    point = {"Key": "AccessPointToken", "Value": REX_POINT}
    return {"rowid": rowid, "UtcTime": utc(local), "KeyValues": [*keys, point]}


class Plant:
    """Porta, controller con il relè del citofono sull'ingresso REX (se wired) e bridge del videocitofono, collegati
    come in main._wire."""

    def __init__(self, db, controller, settings, clock: Clock, *, wired=True, open_door=True, saved=None):
        self.db, self.controller, self.clock = db, controller, clock
        self.monitor = EventMonitor(prefix="accessi/x", door_token="porta")
        self.service = AccessService(db, controller, settings, self.monitor)
        self.service.access_point_doors[REX_POINT] = "porta"
        if wired:
            fields = {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0}
            controller_id = db.save_controller(None, fields)
            db.save_controller_input(
                controller_id,
                "REX",
                {"name": "Relè Citofono", "role": "intercom", "door_token": "porta", "access_point_token": REX_POINT},
            )
        self.saved: list = []
        self.bridge = IntercomBridge(
            IntercomSettings("Citofono Cortile", open_door=open_door),
            log=lambda **fields: self.service.log_event(**(fields | {"ts": clock.iso()}), door="porta"),
            open_door=lambda source, note: self.service.open_door(False, source, "porta", note),
            clock=clock,
            controller_openings=lambda since, until: self.service.intercom_openings("porta", since, until),
            wall=clock.wall,
            wired=saved,
            on_wired=self.saved.append,
        )
        for topic in self.bridge.subscriptions:
            self.monitor.extra_subscriptions.setdefault(topic, self.bridge.handle)

    def mqtt(self, topic: str, payload: str, retained: bool = False) -> None:
        """Messaggio dal broker, come lo smista EventMonitor._message."""
        assert self.monitor._dispatch_extra(aiomqtt.Topic(topic), payload.encode(), retained)

    def press(self, local: str) -> None:
        """Il relè del citofono chiude il contatto REX: il consenso arriva in diretta dal WebSocket."""
        moment = datetime.fromisoformat(utc(local).replace("Z", "+00:00"))
        self.service.record_event(
            Event("AccessControl", "AccessControl/AccessGranted/Anonymous/", moment, {"AccessPointToken": REX_POINT})
        )

    async def unlock(self, local: str, unlock_type="HOUSEHOLDER", control="", seconds_on=2, **attributes) -> None:
        """Apri dal videocitofono: attributi e relè ON, poi OFF dopo qualche secondo."""
        self.clock.at(local)
        values = {"unlock_type": unlock_type, **({"control_source": control} if control else {}), **attributes}
        self.mqtt(RELAY_ATTRIBUTES, json.dumps(values))
        self.mqtt(RELAY, "ON")
        await settle()
        self.clock.now += seconds_on
        self.mqtt(RELAY, "OFF")

    def rows(self) -> list[tuple[str, str, str]]:
        return [(e["kind"], e["detail"], e["source"]) for e in reversed(self.db.list_events())]


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return Clock("20:02:58")


async def test_13_11_late_presses_after_the_restart_do_not_open_the_door_again(db, controller, settings, clock):
    """La cronologia vera del 13/11, dai log del titolare."""
    # 20:01:30 Home Assistant si spegne per l'aggiornamento: broker e bridge del citofono cadono con lui.
    # 20:02:42 Apri dal monitor del citofono: il relè cablato sul REX apre la porta, anche con Home Assistant spento.
    # Due pressioni (5 s) che il registro del controller tiene anche senza nessuno collegato
    controller.log_events = [log_entry(868, "20:02:42"), log_entry(869, "20:02:47")]
    # 20:02:57 il bridge torna sul broker; 20:02:58 riparte l'add-on, che si collega al broker e trova gli stati
    # conservati del bridge
    plant = Plant(db, controller, settings, clock)
    clock.at("20:02:59")
    plant.bridge.broker_connected()
    for topic in (CALL_AVAILABILITY, RELAY_AVAILABILITY):
        plant.mqtt(topic, "online", retained=True)
    plant.mqtt(CALL, "idle", retained=True)
    plant.mqtt(RELAY, "OFF", retained=True)
    # 20:03:01 recupero all'avvio dal registro del controller: l'evento 868, recuperato
    clock.at("20:03:01")
    assert await plant.service.recover_events(datetime(2026, 11, 13, 19, 0, tzinfo=UTC)) == 1
    [opened] = db.list_events()
    assert opened["recovered"] and opened["kind"] == "apertura_remota" and opened["source"] == "Relè Citofono"
    assert opened["detail"] == "Relè Citofono · REX · più pressioni" and opened["ts"] == "2026-11-13T19:02:42+00:00"
    # 20:03:04 e 20:03:09: il bridge riconsegna le due pressioni, non conservate dal broker, ciascuna ON per circa 2 s.
    # Nessuna chiamata in quel minuto (il sensore era non disponibile fino alle 20:02:57)
    await plant.unlock("20:03:04")
    await plant.unlock("20:03:09")
    await settle()

    # Prima l'app mandava un AccessDoor e la serratura restava alimentata dalle 20:03:04 alle 20:03:10
    assert FORWARDED not in controller.calls and controller.calls == []
    # Una riga del videocitofono nel registro dice perché, una sola per le due pressioni riconsegnate
    assert plant.rows() == [
        ("apertura_remota", "Relè Citofono · REX · più pressioni", "Relè Citofono"),
        ("citofono", ALREADY_OPEN, "Citofono Cortile"),
    ]
    # Non sono aperture dal vivo: niente avviso di cablaggio da queste
    clock.now += 60
    assert plant.bridge.wired_check() is None


async def test_13_11_without_the_controller_event_the_reconnection_stops_them(db, controller, settings, clock):
    """Stessa sera, con il recupero all'avvio non ancora fatto (o un impianto senza relè cablato): bastano i secondi
    dopo il ricollegamento dell'app al broker."""
    plant = Plant(db, controller, settings, clock, wired=False)
    clock.at("20:02:59")
    plant.bridge.broker_connected()
    plant.mqtt(CALL, "idle", retained=True)
    await plant.unlock("20:03:04")
    await plant.unlock("20:03:09")
    await settle()

    assert controller.calls == []
    assert plant.rows() == [
        (
            "citofono",
            "apertura dal citofono non inoltrata: messaggio arrivato 5 s dopo il ricollegamento dell'app, senza una "
            "chiamata in corso",
            "Citofono Cortile",
        )
    ]


@pytest.mark.parametrize("comeback", ["availability", "relay_off"])
async def test_13_11_with_the_app_still_connected_the_bridge_comeback_stops_them(
    db, controller, settings, clock, comeback
):
    """L'add-on resta collegato a un broker che non cade: conta il ritorno del bridge alle 20:02:57, con la sua
    disponibilità (ha-mqtt-discoverable con manual_availability) o con il relè spento che un bridge ripartito
    ripubblica (lo Switch pubblica solo i cambi)."""
    clock = Clock("19:00:00")
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    plant.mqtt(RELAY_AVAILABILITY, "online", retained=True)
    clock.at("19:30:00")
    plant.mqtt(CALL, "ringing")
    clock.at("19:30:05")
    plant.mqtt(CALL, "idle")
    # 20:01:30 il bridge cade (la sua volontà testamentaria, se l'ha); 20:02:57 torna
    clock.at("20:01:30")
    plant.mqtt(RELAY_AVAILABILITY, "offline")
    clock.at("20:02:57")
    if comeback == "availability":
        plant.mqtt(RELAY_AVAILABILITY, "online")
    else:
        plant.mqtt(RELAY, "OFF")
    await plant.unlock("20:03:04")
    await plant.unlock("20:03:09")
    await settle()

    assert controller.calls == []
    assert plant.rows()[-1] == (
        "citofono",
        "apertura dal citofono non inoltrata: messaggio arrivato 7 s dopo il ritorno del videocitofono, senza una "
        "chiamata in corso",
        "Citofono Cortile",
    )


@pytest.mark.parametrize("wired", [True, False])
async def test_bridge_back_with_online_after_online_stops_the_late_presses(db, controller, settings, wired):
    """Revisione: il bridge che torna con "online" dopo un "online", senza un "offline" visto in mezzo, è un ritorno.
    Succede quando il bridge si chiude in modo ordinato (il broker scarta il testamento) o quando il broker cade con
    lui e con la persistenza tiene l'ultimo "online". L'Apri vero è di 69 s prima: oltre il minuto in cui conta
    l'apertura del controller, quindi resta solo il ritorno del bridge."""
    clock = Clock("19:00:00")
    plant = Plant(db, controller, settings, clock, wired=wired)
    # L'app è collegata da tempo: disponibilità conservata "online", stati a riposo
    plant.bridge.broker_connected()
    for topic in (CALL_AVAILABILITY, RELAY_AVAILABILITY):
        plant.mqtt(topic, "online", retained=True)
    plant.mqtt(CALL, "idle", retained=True)
    plant.mqtt(RELAY, "OFF", retained=True)
    clock.at("19:30:00")
    plant.mqtt(CALL, "ringing")
    clock.at("19:30:05")
    plant.mqtt(CALL, "idle")
    # 20:01:30 il bridge si ferma senza "offline". 20:01:55 Apri dal monitor: con il relè cablato apre il controller
    clock.at("20:01:30")
    if wired:
        plant.press("20:01:55")
    # 20:02:57 il bridge riparte e pubblica la disponibilità; 20:03:04 e 20:03:09 riconsegna le pressioni vecchie
    clock.at("20:02:57")
    for topic in (CALL_AVAILABILITY, RELAY_AVAILABILITY):
        plant.mqtt(topic, "online")
    await plant.unlock("20:03:04")
    await plant.unlock("20:03:09")
    await settle()

    assert controller.calls == []
    assert plant.rows()[-1] == (
        "citofono",
        "apertura dal citofono non inoltrata: messaggio arrivato 7 s dopo il ritorno del videocitofono, senza una "
        "chiamata in corso",
        "Citofono Cortile",
    )
    # Passati i 30 s, un Apri senza chiamata passa di nuovo
    controller.door = "Locked"
    plant.monitor.door_mode = "Locked"
    await plant.unlock("20:03:40", unlock_type="CENTER_PLATFORM")
    assert controller.calls == [FORWARDED]


async def test_idle_republished_by_the_call_state_polling_does_not_hold_back_an_open(db, controller, settings):
    """Revisione: il bridge con call_state_poll rilegge callStatus a intervalli e ripubblica "idle" ogni volta
    (Sensor.set_state con force_update). Un "idle" uguale dopo un minuto di silenzio non è un ritorno del bridge: l'Apri
    dalla portineria con Hik-Connect, senza chiamata, 10 s dopo si inoltra."""
    clock = Clock("08:00:00")
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    plant.mqtt(CALL_AVAILABILITY, "online", retained=True)
    plant.mqtt(CALL, "idle", retained=True)
    plant.mqtt(RELAY, "OFF", retained=True)
    for _ in range(5):
        clock.now += 90
        plant.mqtt(CALL, "idle")
    clock.now += 10
    await plant.unlock(clock.wall().astimezone(ITALIA).strftime("%H:%M:%S"), unlock_type="CENTER_PLATFORM")
    await settle()

    assert controller.calls == [FORWARDED]
    assert plant.rows() == [("apertura_remota", "", "App Hik-Connect (Videocitofono)")]


async def test_live_open_without_wiring_is_forwarded_at_once(db, controller, settings, clock):
    """Impianto senza relè sul controller: chiamata, risposta, Apri. L'inoltro resta com'era."""
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    clock.at("21:15:00")
    plant.mqtt(CALL, "ringing")
    clock.at("21:15:05")
    plant.mqtt(CALL, "onCall")
    await plant.unlock("21:15:08", control="indoor")
    plant.mqtt(CALL, "idle")

    assert controller.calls == [FORWARDED]
    assert plant.rows() == [
        ("citofono", "ha suonato", "Citofono Cortile"),
        ("citofono", "risposta dopo 5 s", "Citofono Cortile"),
        ("apertura_remota", "8 s dopo la chiamata", "Monitor Interno (Videocitofono)"),
    ]
    # Apri dall'app Hik-Connect senza chiamata, a ricollegamento lontano: inoltrato anche lui
    controller.door = "Locked"
    plant.monitor.door_mode = "Locked"
    await plant.unlock("21:40:00", control="app")
    assert controller.calls == [FORWARDED, FORWARDED]
    # Nessuna apertura del controller insieme: nessun avviso di cablaggio
    clock.now += 60
    assert plant.bridge.wired_check() is None and plant.saved == []


async def test_live_call_right_after_a_reconnection_is_forwarded(db, controller, settings, clock):
    plant = Plant(db, controller, settings, clock, wired=False)
    clock.at("20:02:59")
    plant.bridge.broker_connected()
    # Chiamata arrivata dopo il ricollegamento: chi apre ha risposto adesso, anche nei primi secondi
    clock.at("20:03:01")
    plant.mqtt(CALL, "ringing")
    await plant.unlock("20:03:06", control="app")

    assert controller.calls == [FORWARDED]
    assert plant.rows()[-1] == ("apertura_remota", "5 s dopo la chiamata", "App Hik-Connect (Videocitofono)")


async def test_call_from_before_the_reconnection_is_not_a_live_sign(db, controller, settings):
    clock = Clock("20:02:00")
    plant = Plant(db, controller, settings, clock, wired=False)
    clock.at("20:02:30")
    plant.bridge.broker_connected()
    plant.mqtt(CALL, "ringing")
    # Il broker cade e torna: la chiamata di prima non dice che l'Apri dopo è dal vivo
    clock.at("20:02:59")
    plant.bridge.broker_connected()
    await plant.unlock("20:03:04")
    assert controller.calls == []
    # Una risposta arrivata dopo il ricollegamento sì
    clock.at("20:03:15")
    plant.mqtt(CALL, "onCall")
    await plant.unlock("20:03:18")
    assert controller.calls == [FORWARDED]


async def test_skipped_late_message_does_not_hold_back_a_real_open(db, controller, settings, clock):
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    await plant.unlock("20:03:26")
    assert controller.calls == []
    # Passato il tempo dopo il ricollegamento, l'Apri senza chiamata (ripetuto da chi era al monitor) passa, anche
    # meno di 5 s dopo quello scartato
    await plant.unlock("20:03:29")
    assert controller.calls == [FORWARDED]


async def test_retained_relay_after_a_reconnection_is_still_ignored(db, controller, settings, clock):
    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    plant.mqtt(RELAY_ATTRIBUTES, '{"unlock_type": "HOUSEHOLDER"}', retained=True)
    plant.mqtt(RELAY, "ON", retained=True)
    plant.mqtt(CALL, "ringing", retained=True)
    await settle()

    assert controller.calls == [] and db.list_events() == []


async def test_event_time_in_the_attributes_stops_old_messages(db, controller, settings, clock):
    """Con l'ora dell'evento negli attributi del relè: un messaggio di 22 s prima non si inoltra, anche senza un
    ricollegamento visto. Lo scarto dell'orologio del citofono si misura alle aperture durante una chiamata."""
    clock = Clock("19:00:00")
    plant = Plant(db, controller, settings, clock, wired=False)
    await plant.unlock("20:03:04", time="2026-11-13T20:02:42+01:00")
    assert controller.calls == []
    assert plant.rows()[-1][1] == "apertura dal citofono non inoltrata: messaggio di 22 s prima (ora del videocitofono)"

    # Citofono con l'orologio 2 minuti indietro: alla prima chiamata l'app misura lo scarto, e dopo l'Apri senza
    # chiamata con la stessa ora indietro passa
    behind = timedelta(minutes=2)
    clock.at("21:00:00")
    plant.mqtt(CALL, "ringing")
    await plant.unlock("21:00:06", time=(clock.wall() + timedelta(seconds=6) - behind).isoformat())
    assert controller.calls == [FORWARDED]
    plant.mqtt(CALL, "idle")
    plant.monitor.door_mode = "Locked"
    clock.at("21:30:00")
    await plant.unlock("21:30:00", time=(clock.wall() - behind).isoformat())
    assert controller.calls == [FORWARDED, FORWARDED]
    # Senza fuso l'ora non si sa: non conta
    plant.monitor.door_mode = "Locked"
    await plant.unlock("21:45:00", time="2026-11-13T10:00:00")
    assert controller.calls == [FORWARDED] * 3


async def test_wired_relay_after_the_controller_opens_once_and_warns(db, controller, settings, clock):
    """Relè cablato e inoltro attivo, apertura vera dal vivo: il controller apre dal REX, il relè ON arriva dopo.
    Nessun AccessDoor, e dopo due aperture così l'avviso di cablaggio doppio."""
    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    for start in ("21:15:00", "21:40:00"):
        clock.at(start)
        plant.mqtt(CALL, "ringing")
        clock.now += 6
        plant.press((clock.wall() + timedelta(seconds=0)).astimezone(ITALIA).strftime("%H:%M:%S"))
        clock.now += 1
        await plant.unlock((clock.wall()).astimezone(ITALIA).strftime("%H:%M:%S"))
        plant.mqtt(CALL, "idle")
        # Il controller ha già aperto: niente comando in più
        assert controller.calls == []
        assert plant.rows()[-1] == ("citofono", ALREADY_OPEN, "Citofono Cortile")
        if start == "21:15:00":
            clock.now += 15
            # Una sola apertura così può essere un caso: ancora niente avviso
            assert plant.bridge.wired_check() is None
    clock.now += 15
    wired = plant.bridge.wired_check()
    assert wired == {"since": "2026-11-13T20:40:07+00:00", "input": "Relè Citofono (REX)"}
    assert plant.saved == [wired]

    # La notifica per chi gestisce l'impianto, tra i guasti
    notifications = NotificationService(db)
    items = notifications.collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=False,
        controller_online=True,
        sync={},
        door_names={"porta": "Ingresso Teatro"},
        intercom_wiring=[{"door": "Ingresso Teatro", "door_token": "porta", "name": "Citofono Cortile", **wired}],
        primary_id=1,
    )
    [item] = [i for i in items if i["type"] == "intercom"]
    assert item["title"] == "Videocitofono Cablato sul Controller" and item["severity"] == "crit"
    assert item["text"] == (
        "Citofono Cortile · Il relè apre la porta anche dal controller, dall'ingresso Relè Citofono (REX): l'inoltro "
        'dall\'app non serve e un messaggio in ritardo può riaprirla. In Modifica Videocitofono togli «"Apri" dal '
        "Videocitofono Apre Questa Porta»: resta Solo Registro."
    )
    assert "Solo Registro" in item["text"] and item["link"] == "#impianti/c1/citofoni"
    assert item["door"] == "Ingresso Teatro" and item["fault"] is True
    # Chi gestisce l'impianto la vede; il personale senza Impianti no (non la può correggere)
    assert [i["type"] for i in notifications.listing(items, {"stato", "impianti"})["items"]] == ["intercom"]
    assert notifications.listing(items, {"stato", "registro"}, plain=True)["items"] == []


async def test_wired_relay_before_the_controller_is_forwarded_once_and_warns(db, controller, settings, clock):
    """Relè ON prima dell'apertura del controller: l'inoltro parte (l'evento non c'è ancora), il controller poi apre
    dal REX con la porta già aperta e l'app non manda altro. Le due cose insieme fanno l'avviso."""
    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    for start in ("21:15:00", "21:40:00"):
        clock.at(start)
        plant.mqtt(CALL, "ringing")
        await plant.unlock((clock.wall() + timedelta(seconds=6)).astimezone(ITALIA).strftime("%H:%M:%S"))
        plant.press((clock.wall() - timedelta(seconds=1)).astimezone(ITALIA).strftime("%H:%M:%S"))
        plant.mqtt(CALL, "idle")
        await settle()
        plant.monitor.door_mode = "Locked"
    # Un AccessDoor per apertura, mai due
    assert controller.calls == [FORWARDED, FORWARDED]
    clock.now += 15
    assert plant.bridge.wired_check() == {"since": "2026-11-13T20:40:06+00:00", "input": "Relè Citofono (REX)"}


async def test_wiring_warning_goes_away_with_an_open_the_controller_did_not_make(db, controller, settings, clock):
    saved = {"since": "2026-11-13T20:40:06+00:00", "input": "Relè Citofono (REX)"}
    plant = Plant(db, controller, settings, clock, saved=saved)
    plant.bridge.broker_connected()
    assert plant.bridge.wired_check() == saved
    # Apri dal vivo senza apertura del controller: il relè non è (più) cablato
    clock.at("21:15:00")
    plant.mqtt(CALL, "ringing")
    await plant.unlock("21:15:06")
    assert controller.calls == [FORWARDED]
    clock.now += 15
    assert plant.bridge.wired_check() is None and plant.saved == [None]
    # Con l'inoltro spento il cablaggio non conta
    off = Plant(db, controller, settings, clock, open_door=False, saved=saved)
    assert off.bridge.wired is None and off.bridge.wired_check() is None


async def test_forwarding_off_still_only_logs(db, controller, settings, clock):
    """Solo Registro: come prima, anche con il relè cablato e subito dopo un ricollegamento."""
    plant = Plant(db, controller, settings, clock, open_door=False)
    plant.bridge.broker_connected()
    plant.press("20:03:03")
    await plant.unlock("20:03:04", control="indoor")
    assert controller.calls == []
    assert plant.rows()[-1] == (
        "citofono",
        "apertura da Monitor Interno (inoltro all'A1210 disattivato)",
        "Citofono Cortile",
    )


class FakeMqttClient:
    async def publish(self, topic, payload, qos=0, retain=False):
        pass

    async def subscribe(self, topic, qos=0):
        pass


def test_app_follows_the_bridge_and_closes_the_warning_when_forwarding_stops(controller, tmp_path, monkeypatch):
    """Nell'app: disponibilità del bridge seguita, ricollegamento al broker passato ai videocitofoni, avviso di
    cablaggio che resta dopo una nuova configurazione e si chiude spegnendo l'inoltro."""
    from fastapi.testclient import TestClient

    from app.config import parse_options
    from app.main import SETTING_INTERCOM_WIRED, create_app

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "intercom_name": "Citofono Cortile", "intercom_open_door": True}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "c.db")
    with TestClient(app) as client:
        app.state.notifications.set_levels({"mqtt": "off"}, "test")
        [intercom] = app.state.intercoms
        assert intercom.settings.open_door
        assert set(intercom.subscriptions) <= set(app.state.monitor.extra_subscriptions)
        assert RELAY_AVAILABILITY in intercom.subscriptions and CALL_AVAILABILITY in intercom.subscriptions
        # Collegata al broker (anche di nuovo): i primi messaggi del videocitofono possono essere vecchi
        client.portal.call(app.state.monitor.on_connect, FakeMqttClient())
        assert intercom._back_reason == "il ricollegamento dell'app"

        def warnings():
            items = client.get("/api/notifications").json()["items"]
            return [i for i in items if i["type"] == "intercom"]

        assert warnings() == []
        wired = {"since": "2026-11-13T20:40:06+00:00", "input": "Relè Citofono (REX)"}
        intercom.wired = wired
        intercom._on_wired(wired)
        [warning] = warnings()
        assert warning["title"] == "Videocitofono Cablato sul Controller" and warning["fault"]
        # Una nuova configurazione (nome della porta) non la dimentica
        door = app.state.db.list_doors()[0]
        assert client.put(f"/api/doors/{door['id']}", json={**door, "name": "Ingresso Teatro"}).status_code == 200
        [warning] = warnings()
        assert warning["door"] == "Ingresso Teatro"
        # Inoltro spento (Solo Registro): la notifica si chiude e non torna
        door = app.state.db.list_doors()[0]
        assert client.put(f"/api/doors/{door['id']}", json={**door, "intercom_open_door": False}).status_code == 200
        assert warnings() == []
        assert not app.state.db.get_setting(SETTING_INTERCOM_WIRED)


async def test_new_texts_are_fine_for_staff_without_technical_permissions(db, controller, settings, clock):
    """Le righe del registro e la notifica nuove non nominano Home Assistant, MQTT, broker o add-on: il personale
    dell'accesso diretto le legge come sono (vedi plain.py)."""
    from app.notifications import TYPES
    from app.plain import forbidden

    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    await plant.unlock("20:03:02")
    plant.press("20:03:40")
    await plant.unlock("20:03:41")
    clock.now += 60
    await plant.unlock("20:05:00", time="2026-11-13T20:04:00+01:00")
    texts = [detail for kind, detail, _ in plant.rows() if kind == "citofono"]
    assert len(texts) == 3
    assert not [t for t in texts if forbidden(t)]
    assert not forbidden(TYPES["intercom"][0]) and not forbidden(TYPES["intercom"][1])
    assert not forbidden(TYPES["camera"][1])
