"""Stato del controller letto con le API: porte, contatto porta, ingressi, coperchio, allarmi e manomissioni.

Gli eventi in diretta (oggi MQTT, più avanti il WebSocket) aggiornano lo stato quando cambia. Questa lettura lo
ricostruisce per intero quando il controller torna a rispondere (all'avvio dell'add-on, dopo un'interruzione) e lo
ricontrolla a ogni giro: lo stato non dipende dagli eventi, e un evento perso non lascia un valore sbagliato. Quando il
controller non risponde lo stato si svuota: app e Home Assistant lo mostrano sconosciuto, non l'ultimo valore.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from .events import DOOR_CONDITIONS, EventMonitor, StateSnapshot, event_from_log
from .vapix import ControllerUnreachable, VapixError

_LOGGER = logging.getLogger(__name__)

# Secondi tra due letture: con gli eventi in diretta è una rete di sicurezza, senza è l'unico aggiornamento
POLL_LIVE = 30
POLL_WITHOUT_EVENTS = 10
# Coperchio, manomissione e preallarme non hanno un'API di stato: valgono gli ultimi eventi del registro interno del
# controller, che all'avvio li registra tutti. Alla lettura completa si va indietro a pagine finché non si trovano
# tutti (al più LOG_PAGES pagine), poi a ogni giro bastano gli ultimi: un cambio arriva al giro dopo
LOG_FULL = 1000
LOG_PAGES = 10
LOG_TAIL = 100
# Dal registro solo questi: gli ingressi all'avvio del controller risultano a 0 anche a riposo (si leggono con
# getPorts), modalità e contatto con GetDoorState
LOG_CONDITIONS = {prefix.rstrip("/"): key for prefix, key in DOOR_CONDITIONS.items()}
CASING_TOPIC = "Device/Casing/Open"
READY_TOPIC = "Device/Status/SystemReady"


def contact_configured(configurations: list[dict], token: str) -> bool | None:
    """La porta usa il contatto porta (DoorMonitor); None se la sua configurazione non c'è."""
    for item in configurations:
        if item.get("token") == token:
            values = {c.get("Name"): c.get("Value") for c in item.get("Configuration") or []}
            return values.get("DoorMonitor.Type") not in (None, "", "None")
    return None


def _from_log(snapshot: StateSnapshot, entries: list[dict], doors: list[str]) -> None:
    """Ultimo valore di coperchio e condizioni delle porte nel registro, dal più recente. Le condizioni già lette con
    GetDoorState (l'allarme, con il contatto porta) restano quelle."""
    known: set[tuple] = {(token, key) for token, items in snapshot.conditions.items() for key in items}
    # Dal più recente secondo l'ordine di scrittura: dopo un riavvio l'ora degli eventi può non seguirlo
    for entry in sorted(entries, key=lambda e: e.get("rowid") or 0, reverse=True):
        event = event_from_log(entry)
        if event is None:
            continue
        topic, data = event.topic.rstrip("/"), event.data
        ts = event.timestamp.isoformat(timespec="seconds")
        if topic == CASING_TOPIC and "Open" in data:
            casing = str(data["Open"]) == "1"
            if snapshot.casing is None:
                snapshot.casing = casing
                # Da quando: per il guasto Coperchio del Controller Aperto e la scheda Stato del controller
                snapshot.casing_at = ts
            elif casing != snapshot.casing and not snapshot.casing_before:
                # Il cambio precedente: dice se tra due letture il coperchio è stato chiuso e riaperto
                snapshot.casing_before = ts
            continue
        if topic == READY_TOPIC:
            # Ultimo avvio del controller: in Home Assistant Ultimo Avvio Controller anche dopo un riavvio dell'add-on
            snapshot.system_ready = snapshot.system_ready or ts
            continue
        key = LOG_CONDITIONS.get(topic)
        token = str(data.get("DoorToken") or "")
        if key and token in doors and "State" in data and (token, key) not in known:
            known.add((token, key))
            snapshot.conditions.setdefault(token, {})[key] = str(data["State"])
            # Ora del cambio, non della lettura: in Home Assistant è l'attributo ultimo_cambio
            snapshot.changed_at[(token, key)] = ts


def _log_missing(snapshot: StateSnapshot, doors: list[str]) -> bool:
    """Manca ancora qualcosa che si legge solo dal registro."""
    return snapshot.casing is None or any(
        key not in snapshot.conditions.get(token, {}) for token in doors for key in LOG_CONDITIONS.values()
    )


async def _read_log(client, snapshot: StateSnapshot, doors: list[str], full: bool) -> None:
    entries = await client.latest_events(LOG_FULL if full else LOG_TAIL)
    _from_log(snapshot, entries, doors)
    pages = 1
    while full and len(entries) == LOG_FULL and pages < LOG_PAGES and _log_missing(snapshot, doors):
        oldest = min((e.get("rowid") or 0 for e in entries), default=0)
        entries = await client.latest_events(LOG_FULL, before=oldest) if oldest else []
        # Solo se va davvero indietro: con un firmware che intende RowID in un altro modo ci si ferma
        if not entries or min(e.get("rowid") or 0 for e in entries) >= oldest:
            break
        _from_log(snapshot, entries, doors)
        pages += 1


async def read_state(client, doors: list[str], full: bool) -> StateSnapshot:
    """Stato letto dal controller. Le porte sono obbligatorie (VapixError se il controller non risponde); ingressi,
    registro e configurazione delle porte se non si leggono restano come sono."""
    snapshot = StateSnapshot(at=datetime.now(tz=UTC).isoformat(timespec="seconds"))
    for token in doors:
        snapshot.doors.add(token)
        try:
            door = await client.door_state(token)
        except ControllerUnreachable:
            raise
        except VapixError as err:
            # Porta che il controller non conosce più (tolta o rinominata fuori dall'app): sconosciuta, le altre sì
            _LOGGER.debug("Stato della porta %s non letto: %s", token, err)
            continue
        if door.get("DoorMode"):
            snapshot.modes[token] = str(door["DoorMode"])
        # Senza contatto porta il controller non manda né DoorPhysicalState né Alarm
        if door.get("DoorPhysicalState"):
            snapshot.physical[token] = str(door["DoorPhysicalState"])
        if door.get("Alarm"):
            snapshot.conditions.setdefault(token, {})["allarme"] = str(door["Alarm"])
    try:
        ports = await client.io_ports()
    except VapixError as err:
        _LOGGER.debug("Ingressi del controller non letti: %s", err)
    else:
        for item in ports:
            port, value, normal = str(item.get("port") or ""), item.get("state"), item.get("normalState")
            # Attivo quando non è nello stato normale, come in Device/IO/Port. Le uscite non sono ingressi
            if port and item.get("direction", "input") == "input" and {value, normal} <= {"open", "closed"}:
                snapshot.io[port] = value != normal
    try:
        await _read_log(client, snapshot, doors, full)
    except VapixError as err:
        _LOGGER.debug("Registro del controller non letto: %s", err)
    if full:
        try:
            configurations = await client.list_door_configurations()
        except VapixError as err:
            _LOGGER.debug("Configurazione delle porte non letta: %s", err)
        else:
            contacts = {token: contact_configured(configurations, token) for token in doors}
            snapshot.contacts = {token: value for token, value in contacts.items() if value is not None}
    return snapshot


class StateKeeper:
    """Tiene lo stato del monitor allineato al controller: sconosciuto finché il controller non risponde, letto per
    intero quando torna a rispondere (o dopo un'interruzione degli eventi), poi ricontrollato a ogni giro."""

    def __init__(self, monitor: EventMonitor, wake: asyncio.Event | None = None) -> None:
        self.monitor = monitor
        self.wake = wake or asyncio.Event()
        self.full_pending = True
        self.outages = monitor.outages

    def request_full(self) -> None:
        """Lettura completa al prossimo giro, subito: configurazione delle porte cambiata, controller tornato."""
        self.full_pending = True
        self.wake.set()

    async def tick(self, client, doors: list[str], responding: bool | None) -> bool:
        """Un giro. responding: il controller risponde (True), non risponde (False), non si sa ancora (None: primo
        controllo non fatto, o un solo controllo fallito). True se lo stato è cambiato."""
        monitor = self.monitor
        if responding is None:
            return False
        if not responding:
            self.full_pending = True
            if monitor.reachable is False:
                return False
            monitor.reachable = False
            monitor.forget_state()
            return True
        if monitor.outages != self.outages:
            # Eventi interrotti (broker perso, controller scollegato dal broker): quelli persi si rileggono tutti
            self.outages = monitor.outages
            self.full_pending = True
        full, self.full_pending = self.full_pending, False
        since = monitor.seq
        try:
            snapshot = await read_state(client, doors, full)
        except VapixError as err:
            # Se non risponde più lo dirà il controllo periodico: fino ad allora lo stato resta quello letto
            _LOGGER.warning("Stato del controller non letto: %s", err)
            self.full_pending = self.full_pending or full
            return False
        changed = monitor.apply_snapshot(snapshot, since)
        if monitor.reachable is not True:
            monitor.reachable = True
            changed = True
        if full:
            _LOGGER.info("Stato del controller letto per intero")
        return changed
