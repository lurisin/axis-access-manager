from datetime import UTC, datetime

from app.controller_state import LOG_TAIL, StateKeeper, contact_configured
from app.events import Event, EventMonitor
from app.vapix import ControllerUnreachable, VapixError


def log_event(rowid, ts, *topic, **values):
    """Evento del registro interno del controller (axlog:FetchEvents)."""
    keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(topic)]
    return {
        "rowid": rowid,
        "UtcTime": ts,
        "KeyValues": keys + [{"Key": k, "Value": v} for k, v in values.items()],
    }


def keeper_for(controller):
    monitor = EventMonitor(prefix="x", door_token="porta")
    return monitor, StateKeeper(monitor)


async def test_full_read_when_the_controller_answers(controller):
    monitor, keeper = keeper_for(controller)
    # Prima del primo controllo non si legge niente
    assert await keeper.tick(controller, ["porta"], None) is False
    assert monitor.reachable is None and monitor.door_modes == {}

    assert await keeper.tick(controller, ["porta"], True) is True
    assert monitor.reachable is True and monitor.state_read_at
    assert monitor.door_modes == {"porta": "Locked"}
    assert monitor.door_physical == {"porta": "Closed"}
    assert monitor.door_conditions["porta"]["allarme"]["state"] == "Normal"
    assert monitor.io_ports == {"0": False, "1": False, "2": False, "3": False}
    # Configurazione della porta letta: nel controller finto la porta non usa il contatto porta
    assert monitor.door_contacts == {"porta": False}
    # Niente di nuovo: nessun cambio
    assert await keeper.tick(controller, ["porta"], True) is False


async def test_controller_not_answering_forgets_the_state(controller):
    monitor, keeper = keeper_for(controller)
    await keeper.tick(controller, ["porta"], True)
    monitor.casing_open = True
    # Il controller non risponde: nessun valore vecchio, né in app né in Home Assistant
    assert await keeper.tick(controller, ["porta"], False) is True
    assert monitor.reachable is False
    assert (monitor.door_modes, monitor.door_physical, monitor.io_ports) == ({}, {}, {})
    assert monitor.door_conditions == {} and monitor.casing_open is None
    assert await keeper.tick(controller, ["porta"], False) is False

    # Torna a rispondere: lettura completa
    controller.door_configs.clear()
    assert await keeper.tick(controller, ["porta"], True) is True
    assert monitor.reachable is True and monitor.door_modes == {"porta": "Locked"}


async def test_state_is_read_at_every_round_without_events(controller):
    # Collaudo 29/10: app collegata al broker, controller no; Stato Porta fermo su «Aperta a Tempo» per ore
    monitor, keeper = keeper_for(controller)
    monitor.connected, monitor.controller_online = True, False
    monitor.door_modes["porta"] = "Accessed"
    await keeper.tick(controller, ["porta"], True)
    assert monitor.door_modes == {"porta": "Locked"}
    controller.door = "Accessed"
    controller.ports[2]["state"] = "closed"
    assert await keeper.tick(controller, ["porta"], True) is True
    assert monitor.door_modes == {"porta": "Accessed"}
    assert monitor.input_active("2") is True


async def test_contact_removed_from_the_door(controller):
    monitor, keeper = keeper_for(controller)
    await keeper.tick(controller, ["porta"], True)
    # Collaudo 30/10: contatto tolto, GetDoorState dà solo DoorMode. Non resta «Chiusa»
    controller.door_physical = None
    await keeper.tick(controller, ["porta"], True)
    assert monitor.door_physical == {}


async def test_event_during_the_read_is_newer(controller):
    monitor, keeper = keeper_for(controller)

    class Slow:
        def __getattr__(self, name):
            return getattr(controller, name)

        async def door_state(self, token):
            state = await controller.door_state(token)
            # Mentre si legge arriva l'apertura via MQTT: la lettura, iniziata prima, non la cancella
            monitor.apply_event(
                Event(
                    "Door", "Door/State/DoorMode/$source/DoorToken/porta", datetime.now(tz=UTC), {"State": "Accessed"}
                )
            )
            return state

    await keeper.tick(Slow(), ["porta"], True)
    assert monitor.door_modes == {"porta": "Accessed"}
    await keeper.tick(controller, ["porta"], True)
    assert monitor.door_modes == {"porta": "Locked"}


async def test_cover_tamper_and_warning_from_the_controller_log(controller):
    controller.log_events = [
        # All'avvio il controller registra lo stato di tutto (collaudo 30/10)
        log_event(1, "2026-10-29T21:55:17Z", "Device", "Casing", "Open", Open="1"),
        log_event(2, "2026-10-29T21:55:18Z", "Device", "IO", "Port", port="0", state="0"),
        log_event(3, "2026-10-29T21:55:19Z", "Door", "State", "DoorTamper", DoorToken="porta", State="NotInTamper"),
        log_event(4, "2026-10-29T21:55:19Z", "Door", "State", "DoorWarning", DoorToken="porta", State="Normal"),
        log_event(5, "2026-10-29T21:55:20Z", "Door", "State", "DoorAlarm", DoorToken="porta", State="DoorForcedOpen"),
        log_event(6, "2026-10-29T22:10:00Z", "Device", "Casing", "Open", Open="0"),
        log_event(7, "2026-10-29T22:11:00Z", "Door", "State", "DoorTamper", DoorToken="porta", State="TamperDetected"),
        log_event(8, "2026-10-29T22:11:00Z", "Door", "State", "DoorTamper", DoorToken="altra", State="NotInTamper"),
    ]
    controller.log_events.append(log_event(9, "2026-10-29T21:55:23Z", "Device", "Status", "SystemReady", ready="1"))
    monitor, keeper = keeper_for(controller)
    await keeper.tick(controller, ["porta"], True)
    assert monitor.casing_open is False
    assert monitor.system_ready == "2026-10-29T21:55:23+00:00"
    assert monitor.door_conditions["porta"]["manomissione"]["ts"] == "2026-10-29T22:11:00+00:00"
    conditions = {k: v["state"] for k, v in monitor.door_conditions["porta"].items()}
    # L'allarme vale quello di GetDoorState; il registro solo per quello che l'API non dice
    assert conditions == {"allarme": "Normal", "preallarme": "Normal", "manomissione": "TamperDetected"}
    assert monitor.door_conditions["porta"]["manomissione"]["active"] is True
    # Gli ingressi del registro (a 0 dopo l'avvio anche a riposo) non contano: vale getPorts
    assert monitor.io_ports["0"] is False and "altra" not in monitor.door_conditions

    # Senza contatto porta l'allarme non c'è nella risposta: vale l'ultimo del registro
    controller.door_physical = None
    keeper.request_full()
    await keeper.tick(controller, ["porta"], True)
    assert monitor.door_conditions["porta"]["allarme"]["state"] == "DoorForcedOpen"


async def test_full_read_goes_back_in_the_log_until_the_last_change(controller, monkeypatch):
    monkeypatch.setattr("app.controller_state.LOG_FULL", 3)
    # Avvio del controller molto prima: coperchio e condizioni della porta solo nelle pagine vecchie del registro
    controller.log_events = [
        log_event(1, "2026-10-29T21:55:17Z", "Device", "Casing", "Open", Open="1"),
        log_event(2, "2026-10-29T21:55:19Z", "Door", "State", "DoorTamper", DoorToken="porta", State="NotInTamper"),
        log_event(3, "2026-10-29T21:55:19Z", "Door", "State", "DoorWarning", DoorToken="porta", State="Normal"),
        *[log_event(n, "2026-10-30T10:00:00Z", "IdPoint", "Request", "IdData", Card="c0110a01") for n in range(4, 9)],
        log_event(9, "2026-10-30T11:00:00Z", "Door", "State", "DoorTamper", DoorToken="porta", State="TamperDetected"),
    ]
    monitor, keeper = keeper_for(controller)
    await keeper.tick(controller, ["porta"], True)
    assert monitor.casing_open is True
    conditions = {k: v["state"] for k, v in monitor.door_conditions["porta"].items()}
    assert conditions == {"allarme": "Normal", "preallarme": "Normal", "manomissione": "TamperDetected"}


async def test_only_the_last_events_between_full_reads(controller):
    calls = []

    class Spy:
        def __getattr__(self, name):
            return getattr(controller, name)

        async def latest_events(self, limit, before=None):
            calls.append(limit)
            return await controller.latest_events(limit, before)

        async def list_door_configurations(self):
            calls.append("configurazione")
            return await controller.list_door_configurations()

    monitor, keeper = keeper_for(controller)
    await keeper.tick(Spy(), ["porta"], True)
    await keeper.tick(Spy(), ["porta"], True)
    assert calls[1:] == ["configurazione", LOG_TAIL]
    # Eventi interrotti (broker perso): si rilegge tutto
    monitor.outages += 1
    calls.clear()
    await keeper.tick(Spy(), ["porta"], True)
    assert "configurazione" in calls


async def test_pushbutton_read_from_the_controller_is_at_rest(controller):
    monitor, keeper = keeper_for(controller)
    # Collaudo 30/10: AUX 1 come Pulsante Uscita (pu/gnd), a riposo closed con normalState open. Gli eventi lo danno
    # a 1 a riposo, 0 alla pressione: la lettura dà lo stesso valore, ed è affidabile da subito
    controller.ports[0]["state"] = "closed"
    await keeper.tick(controller, ["porta"], True)
    assert monitor.io_ports["0"] is True
    assert monitor.input_active("0", pushbutton=True) is False
    assert monitor.input_active("0") is True


async def test_door_unknown_to_the_controller_does_not_stop_the_others(controller):
    monitor, keeper = keeper_for(controller)
    monitor.door_modes["vecchia"] = "Locked"

    class Partly:
        def __getattr__(self, name):
            return getattr(controller, name)

        async def door_state(self, token):
            if token == "vecchia":
                raise VapixError("il controller ha rifiutato la richiesta")
            return await controller.door_state(token)

    assert await keeper.tick(Partly(), ["vecchia", "porta"], True) is True
    assert monitor.door_modes == {"porta": "Locked"}


async def test_read_failure_keeps_the_full_read_pending(controller):
    monitor, keeper = keeper_for(controller)

    class Down:
        def __getattr__(self, name):
            return getattr(controller, name)

        async def door_state(self, token):
            raise ControllerUnreachable("il controller non risponde")

    assert await keeper.tick(Down(), ["porta"], True) is False
    assert keeper.full_pending is True and monitor.reachable is None


def test_contact_configured():
    configs = [
        {"token": "a", "Configuration": [{"Name": "DoorMonitor.Type", "Value": "Standard"}]},
        {"token": "b", "Configuration": [{"Name": "DoorMonitor.Type", "Value": "None"}]},
        {"token": "c", "Configuration": []},
    ]
    assert [contact_configured(configs, t) for t in "abcd"] == [True, False, False, None]


async def test_casing_open_since_from_the_log_and_the_events(controller):
    controller.log_events = [
        log_event(1, "2026-10-29T21:55:17Z", "Device", "Casing", "Open", Open="1"),
        log_event(2, "2026-10-30T10:00:00Z", "IdPoint", "Request", "IdData", Card="c0110a01"),
    ]
    monitor, keeper = keeper_for(controller)
    # Collaudo 30/10: aperto dall'avvio del controller, si sa da quando anche dopo un riavvio dell'add-on
    await keeper.tick(controller, ["porta"], True)
    assert monitor.casing_open is True and monitor.casing_since == "2026-10-29T21:55:17+00:00"
    # Lo stesso valore conservato dal broker non sposta l'ora; la chiusura sì
    casing = "Device/Casing/Open"
    later = datetime(2026, 10, 30, 22, 0, tzinfo=UTC)
    monitor.apply_event(Event("Device", casing, later, {"Open": "1"}))
    assert monitor.casing_since == "2026-10-29T21:55:17+00:00"
    monitor.apply_event(Event("Device", casing, later, {"Open": "0"}))
    assert monitor.casing_open is False and monitor.casing_since == "2026-10-30T22:00:00+00:00"
    # Controller che non risponde: né lo stato né l'ora
    await keeper.tick(controller, ["porta"], False)
    assert monitor.casing_open is None and monitor.casing_since == ""


async def test_casing_closed_and_opened_again_between_two_reads(controller):
    casing = ("Device", "Casing", "Open")
    controller.log_events = [log_event(1, "2026-10-29T21:55:17Z", *casing, Open="1")]
    monitor, keeper = keeper_for(controller)
    await keeper.tick(controller, ["porta"], True)
    # Il controller si riavvia: registra di nuovo il coperchio aperto, ma non è un'altra apertura
    controller.log_events.append(log_event(2, "2026-10-30T08:00:00Z", *casing, Open="1"))
    await keeper.tick(controller, ["porta"], True)
    assert monitor.casing_since == "2026-10-29T21:55:17+00:00"
    # Senza eventi in diretta, chiuso e riaperto tra due letture: è un'apertura nuova, con la sua ora
    controller.log_events += [
        log_event(3, "2026-10-30T09:00:00Z", *casing, Open="0"),
        log_event(4, "2026-10-30T09:00:05Z", *casing, Open="1"),
    ]
    assert await keeper.tick(controller, ["porta"], True) is True
    assert monitor.casing_open is True and monitor.casing_since == "2026-10-30T09:00:05+00:00"
    # Alla lettura dopo resta quella
    await keeper.tick(controller, ["porta"], True)
    assert monitor.casing_since == "2026-10-30T09:00:05+00:00"
