"""Videocitofono con messaggi in ritardo: il 13/11 sera, dopo il riavvio di Home Assistant, il bridge del videocitofono
ha consegnato in ritardo le pressioni dell'Apri e l'app le ha inoltrate all'A1210: la porta si è aperta 22 s dopo la
richiesta, con nessuno davanti.

All'Eliseo il relè del citofono è collegato all'ingresso REX dell'A1210 («Relè Citofono»), ma lì non apre la porta,
per sicurezza: il controller registra la pressione e la porta la apre solo l'app, con l'inoltro. Orologio finto e
messaggi MQTT finti, passati dal monitor come quelli veri; solo il FakeController, mai una porta vera. Nomi di
fantasia."""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone

import aiomqtt
import pytest

from app.events import Event, EventMonitor
from app.intercom import IntercomBridge, IntercomSettings
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
STALE_APP = (
    "apertura dal citofono non inoltrata: messaggio arrivato 5 s dopo il ricollegamento dell'app, senza una chiamata "
    "in corso"
)


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

    def __init__(self, db, controller, settings, clock: Clock, *, wired=True, open_door=True):
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
        # Controller della porta che non risponde (vedi main._controller_down)
        self.down = False
        self.bridge = IntercomBridge(
            IntercomSettings("Citofono Cortile", open_door=open_door),
            log=lambda **fields: self.service.log_event(**(fields | {"ts": clock.iso()}), door="porta"),
            open_door=lambda source, note: self.service.open_door(False, source, "porta", note),
            clock=clock,
            wall=clock.wall,
            controller_down=lambda: self.down,
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
    # 20:02:42 Apri dal monitor del citofono: il relè, collegato all'ingresso REX, fa registrare la pressione al
    # controller anche con Home Assistant spento (all'Eliseo quell'ingresso non apre: la porta la apre l'app). Due
    # pressioni (5 s) che il registro del controller tiene anche senza nessuno collegato
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

    # Prima l'app mandava un AccessDoor e la serratura restava alimentata dalle 20:03:04 alle 20:03:10, 22 s dopo la
    # richiesta: con nessuno davanti alla porta. Ora le pressioni arrivate subito dopo il ricollegamento non aprono
    assert FORWARDED not in controller.calls and controller.calls == []
    # Una riga del videocitofono nel registro dice perché, una sola per le due pressioni riconsegnate
    assert plant.rows() == [
        ("apertura_remota", "Relè Citofono · REX · più pressioni", "Relè Citofono"),
        ("citofono", STALE_APP, "Citofono Cortile"),
    ]


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
    lui e con la persistenza tiene l'ultimo "online"."""
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
    # 20:01:30 il bridge si ferma senza "offline". 20:01:55 Apri dal monitor: il controller registra la pressione
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


async def test_14_11_presses_kept_during_the_controller_outage_do_not_open_the_door(db, controller, settings):
    """La cronologia vera del 14/11, dal log del titolare. La rete della porta cade per circa due ore: il controller
    non risponde («Sincronizzazione non riuscita: il controller non risponde» ogni minuto fino alle 19:22:46) e il
    videocitofono, sulla stessa rete, non arriva a Home Assistant; il broker e il bridge restano su. Le pressioni
    dell'Apri fatte intanto non aprono (l'ingresso REX è disabilitato) e il videocitofono le tiene. Al ritorno le
    consegna tutte: alle 19:23:54, 19:24:05, 19:24:12 e 19:24:20 l'app le inoltrava e la porta si è aperta quattro
    volte, con nessuno davanti. Il controller risulta di nuovo raggiungibile alle 19:23:58."""
    clock = Clock("17:00:00")
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    plant.mqtt(RELAY_AVAILABILITY, "online", retained=True)
    plant.mqtt(CALL, "idle", retained=True)
    plant.mqtt(RELAY, "OFF", retained=True)
    clock.at("17:20:00")
    plant.down = True
    # La prima arriva quando l'app non ha ancora visto il controller tornare
    await plant.unlock("19:23:54")
    clock.at("19:23:58")
    plant.down = False
    plant.bridge.controller_back()
    for at in ("19:24:05", "19:24:12", "19:24:20"):
        await plant.unlock(at)
    await settle()

    assert controller.calls == []
    texts = [detail for kind, detail, _ in plant.rows() if kind == "citofono"]
    assert texts[:2] == [
        "apertura dal citofono non inoltrata: il controller non risponde, senza una chiamata in corso",
        "apertura dal citofono non inoltrata: messaggio arrivato 7 s dopo il ritorno del controller, senza una "
        "chiamata in corso",
    ]
    assert all("non inoltrata" in text for text in texts)
    # Passati 30 s dall'ultima pressione vecchia, un Apri dalla portineria senza chiamata passa di nuovo
    controller.door = "Locked"
    plant.monitor.door_mode = "Locked"
    await plant.unlock("19:25:30", unlock_type="CENTER_PLATFORM")
    assert controller.calls == [FORWARDED]


async def test_live_call_while_the_controller_does_not_answer_is_still_tried(db, controller, settings, clock):
    """Con una chiamata dal vivo l'Apri si prova sempre, anche con il controller segnato come non raggiungibile: se
    risponde, la porta si apre; se no, l'errore resta nel registro e la richiesta non si ripete dopo."""
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    plant.down = True
    clock.at("21:15:00")
    plant.mqtt(CALL, "ringing")
    await plant.unlock("21:15:06")
    assert controller.calls == [FORWARDED]


async def test_late_presses_that_keep_coming_after_a_reconnection_are_all_held_back(db, controller, settings, clock):
    """Una coda lunga di pressioni vecchie: ognuna fa ripartire i 30 s, anche oltre i 30 s dal ricollegamento.
    Finita la raffica, dopo 30 s di silenzio un Apri senza chiamata passa."""
    plant = Plant(db, controller, settings, clock, wired=False)
    clock.at("20:02:59")
    plant.bridge.broker_connected()
    for at in ("20:03:04", "20:03:25", "20:03:48", "20:04:10"):
        await plant.unlock(at)
    await settle()
    assert controller.calls == []
    assert plant.rows()[-1][1] == (
        "apertura dal citofono non inoltrata: altra pressione arrivata insieme a quelle vecchie, senza una chiamata in "
        "corso"
    )
    controller.door = "Locked"
    plant.monitor.door_mode = "Locked"
    await plant.unlock("20:04:50", unlock_type="CENTER_PLATFORM")
    assert controller.calls == [FORWARDED]


def test_controllers_back_wake_the_intercoms_of_their_doors():
    """Nell'app: i controller che tornano a rispondere tra due controlli avvisano i videocitofoni delle loro porte
    (main._intercoms_after_outage); quelli di un altro controller no."""
    from types import SimpleNamespace

    from app.health import ControllerHealth
    from app.main import _controller_down, _controllers_down, _intercoms_after_outage

    calls = []
    intercoms = [
        SimpleNamespace(door={"door_token": "a", "controller_id": None}, controller_back=lambda: calls.append("a")),
        SimpleNamespace(door={"door_token": "b", "controller_id": 2}, controller_back=lambda: calls.append("b")),
    ]
    states = {
        1: ControllerHealth(1, "Portineria", online=False, failures=3),
        2: ControllerHealth(2, "Cortile", True),
        # Un solo controllo lento non vuol dire controller fermo
        3: ControllerHealth(3, "Magazzino", True, failures=1),
    }
    state = SimpleNamespace(
        installation=SimpleNamespace(primary={"id": 1}), health=SimpleNamespace(states=states), intercoms=intercoms
    )
    assert _controllers_down(state) == {1}
    assert _controller_down(state, intercoms[0].door) and not _controller_down(state, intercoms[1].door)
    before = _controllers_down(state)
    states[1].online, states[1].failures = True, 0
    _intercoms_after_outage(state, before - _controllers_down(state))
    assert calls == ["a"]


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
    """Dopo una pressione vecchia scartata, un altro Apri senza chiamata subito dopo può essere della stessa coda (il
    14/11 arrivavano a 7-11 s l'una dall'altra) e resta fermo; con una chiamata passa sempre, e senza chiamata passa
    dopo 30 s senza pressioni vecchie. Non conta come apertura: non fa partire l'attesa di 5 s."""
    plant = Plant(db, controller, settings, clock, wired=False)
    plant.bridge.broker_connected()
    await plant.unlock("20:03:26")
    assert controller.calls == []
    await plant.unlock("20:03:29")
    assert controller.calls == []
    # Il visitatore suona: chi è al monitor risponde e apre, meno di 5 s dopo la pressione scartata
    plant.mqtt(CALL, "ringing")
    await plant.unlock("20:03:33")
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


async def test_relay_on_a_controller_input_that_does_not_open_never_holds_back_the_open(
    db, controller, settings, clock
):
    """Eliseo (15/11): il relè del videocitofono è collegato all'ingresso Relè Citofono dell'A1210, ma lì non apre la
    porta, per sicurezza (un relè manomesso non deve aprire): la apre solo l'app, con l'inoltro. Il controller
    registra comunque la pressione come apertura remota dal Relè Citofono, spesso prima che arrivi il relè ON del
    videocitofono. Nella 0.28.4 quella riga fermava l'inoltro e la porta restava chiusa: ora un Apri dal vivo si
    inoltra sempre, con la riga del controller prima o dopo."""
    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    for start, controller_first in (("21:15:00", True), ("21:40:00", False), ("22:05:00", True)):
        clock.at(start)
        plant.mqtt(CALL, "ringing")
        clock.now += 6
        if controller_first:
            plant.press(clock.wall().astimezone(ITALIA).strftime("%H:%M:%S"))
            clock.now += 1
        await plant.unlock(clock.wall().astimezone(ITALIA).strftime("%H:%M:%S"))
        if not controller_first:
            plant.press((clock.wall() - timedelta(seconds=1)).astimezone(ITALIA).strftime("%H:%M:%S"))
        plant.mqtt(CALL, "idle")
        await settle()
        controller.door = "Locked"
        plant.monitor.door_mode = "Locked"
    assert controller.calls == [FORWARDED] * 3
    assert [row for row in plant.rows() if "non inoltrata" in row[1]] == []
    # Nessun avviso che chieda di spegnere l'inoltro: all'Eliseo spegnerebbe l'apertura dal monitor
    from app.notifications import TYPES

    assert "intercom" not in TYPES


async def test_relay_on_a_controller_input_that_opens_is_not_sent_twice(db, controller, settings, clock):
    """Dove invece il relè sull'ingresso del controller apre davvero la porta, l'app la trova già aperta a tempo e
    non manda un secondo comando, come prima della 0.28.4."""
    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    clock.at("21:15:00")
    plant.mqtt(CALL, "ringing")
    clock.now += 6
    plant.press(clock.wall().astimezone(ITALIA).strftime("%H:%M:%S"))
    # Il controller ha aperto dal REX: la porta è aperta a tempo
    controller.door = "Accessed"
    plant.monitor.door_mode = "Accessed"
    clock.now += 1
    await plant.unlock(clock.wall().astimezone(ITALIA).strftime("%H:%M:%S"))
    await settle()
    assert FORWARDED not in controller.calls


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


def test_app_follows_the_bridge_and_forgets_the_old_wiring_warning(controller, tmp_path, monkeypatch):
    """Nell'app: disponibilità del bridge seguita e ricollegamento al broker passato ai videocitofoni. L'avviso
    «Videocitofono Cablato sul Controller» della 0.28.4 non c'è più: quello salvato si cancella all'avvio."""
    from fastapi.testclient import TestClient

    from app.config import parse_options
    from app.db import Database
    from app.main import SETTING_INTERCOM_WIRED, create_app

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    path = tmp_path / "c.db"
    old = Database(path)
    old.set_setting(
        SETTING_INTERCOM_WIRED, '{"porta": {"since": "2026-11-15T08:00:00+00:00", "input": "Relè Citofono"}}'
    )
    old.close()
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "intercom_name": "Citofono Cortile", "intercom_open_door": True}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=path)
    with TestClient(app) as client:
        app.state.notifications.set_levels({"mqtt": "off"}, "test")
        [intercom] = app.state.intercoms
        assert intercom.settings.open_door
        assert set(intercom.subscriptions) <= set(app.state.monitor.extra_subscriptions)
        assert RELAY_AVAILABILITY in intercom.subscriptions and CALL_AVAILABILITY in intercom.subscriptions
        # Collegata al broker (anche di nuovo): i primi messaggi del videocitofono possono essere vecchi
        client.portal.call(app.state.monitor.on_connect, FakeMqttClient())
        assert intercom._back_reason == "il ricollegamento dell'app"
        assert not app.state.db.get_setting(SETTING_INTERCOM_WIRED)
        items = client.get("/api/notifications").json()["items"]
        assert not [i for i in items if "Cablato" in i["title"]]


def test_in_the_app_the_controller_row_of_the_relay_does_not_stop_a_live_open(controller, tmp_path, monkeypatch):
    """Lo stesso nell'app vera, collegata come in main._wire: la riga del controller per la pressione del relè (Relè
    Citofono, non apre) arriva prima del relè ON, e l'Apri dal vivo durante la chiamata si inoltra lo stesso. Nella
    0.28.4 main passava al videocitofono le aperture del controller, e qui la porta restava chiusa."""
    from fastapi.testclient import TestClient

    from app.config import parse_options
    from app.main import create_app

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "intercom_name": "Citofono Cortile", "intercom_open_door": True}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "c.db")
    with TestClient(app) as client:
        db, service, monitor = app.state.db, app.state.service, app.state.monitor
        fields = {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0}
        controller_id = db.save_controller(None, fields)
        db.save_controller_input(
            controller_id,
            "REX",
            {"name": "Relè Citofono", "role": "intercom", "door_token": "porta", "access_point_token": REX_POINT},
        )
        service.access_point_doors[REX_POINT] = "porta"
        controller.door = "Locked"
        monitor.door_mode = "Locked"
        controller.calls.clear()

        async def live_open():
            monitor._dispatch_extra(aiomqtt.Topic(CALL), b"ringing", False)
            service.record_event(
                Event(
                    "AccessControl",
                    "AccessControl/AccessGranted/Anonymous/",
                    service.controller_now(),
                    {"AccessPointToken": REX_POINT},
                )
            )
            monitor._dispatch_extra(aiomqtt.Topic(RELAY_ATTRIBUTES), b'{"unlock_type": "HOUSEHOLDER"}', False)
            monitor._dispatch_extra(aiomqtt.Topic(RELAY), b"ON", False)
            await asyncio.sleep(0.2)

        client.portal.call(live_open)
        assert FORWARDED in controller.calls
        rows = [(e["kind"], e["source"]) for e in db.list_events()]
        assert ("apertura_remota", "Relè Citofono") in rows
        assert not [e for e in db.list_events() if "non inoltrata" in (e["detail"] or "")]


async def test_new_texts_are_fine_for_staff_without_technical_permissions(db, controller, settings, clock):
    """Le righe del registro e la notifica nuove non nominano Home Assistant, MQTT, broker o add-on: il personale
    dell'accesso diretto le legge come sono (vedi plain.py)."""
    from app.notifications import TYPES
    from app.plain import forbidden

    plant = Plant(db, controller, settings, clock)
    plant.bridge.broker_connected()
    await plant.unlock("20:03:02")
    clock.now += 60
    await plant.unlock("20:05:00", time="2026-11-13T20:04:00+01:00")
    texts = [detail for kind, detail, _ in plant.rows() if kind == "citofono"]
    assert len(texts) == 2
    assert not [t for t in texts if forbidden(t)]
    assert not forbidden(TYPES["camera"][1])
