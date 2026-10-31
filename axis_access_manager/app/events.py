"""Ascolto degli eventi che l'A1210 pubblica via MQTT."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiomqtt

from .config import MqttSettings

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 10
# Filtro di iscrizione rifiutato dalla libreria MQTT (per esempio # o + nel prefisso): si riprova senza traceback
INVALID_TOPIC = "topic MQTT non valido"


@dataclass
class StateSnapshot:
    """Stato del controller letto con le API, nello stesso formato degli eventi (vedi controller_state)."""

    at: str
    # Porte lette: per ognuna modalità (DoorMode) e contatto (DoorPhysicalState). Senza contatto porta il controller
    # non manda DoorPhysicalState: per una porta letta, un contatto che manca è sconosciuto
    doors: set[str] = field(default_factory=set)
    modes: dict[str, str] = field(default_factory=dict)
    physical: dict[str, str] = field(default_factory=dict)
    # Allarme, preallarme e manomissione per porta (chiave → stato, come DOOR_CONDITIONS), con l'ora del cambio
    # quando la si sa (dal registro del controller; se no vale l'ora della lettura)
    conditions: dict[str, dict[str, str]] = field(default_factory=dict)
    changed_at: dict[tuple[str, str], str] = field(default_factory=dict)
    # Ingressi I/O per numero di porta, attivi o no come in Device/IO/Port
    io: dict[str, bool] = field(default_factory=dict)
    # Coperchio aperto; None se non letto. casing_at: ora dell'ultimo cambio (dal registro), vuota se non si sa;
    # casing_before: ora del cambio precedente, al valore opposto (chiuso prima di riaprirlo), vuota se non c'è
    casing: bool | None = None
    casing_at: str = ""
    casing_before: str = ""
    # Porte con il contatto porta configurato; None se la configurazione non è stata letta
    contacts: dict[str, bool] | None = None
    # Ultimo avvio del controller (ISO, dal registro); vuoto se non trovato
    system_ready: str = ""


@dataclass(frozen=True)
class Event:
    category: str  # primo livello del topic ONVIF: AccessControl, Door, IdPoint, Device...
    topic: str  # topic senza prefisso, es. IdPoint/Request/IdData/...
    timestamp: datetime
    data: dict


def parse_event(prefix: str, topic: str, payload: bytes) -> Event | None:
    """Interpreta un messaggio pubblicato su <prefisso>/eventi/...

    Restituisce None per i topic che non sono eventi o per payload non validi.
    """
    base = f"{prefix}/eventi/"
    if not topic.startswith(base):
        return None
    path = topic[len(base) :]
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    timestamp = _timestamp(body.get("timestamp"))
    message = body.get("message")
    data: dict = {}
    # Le sorgenti (porta, punto di accesso, numero dell'I/O) l'A1210 le mette nel topic e non nei dati
    data.update(topic_sources(path))
    for part in ("source", "key", "data"):
        value = message.get(part) if isinstance(message, dict) else None
        if isinstance(value, dict):
            data.update(value)
    return Event(category=path.split("/", 1)[0], topic=path, timestamp=timestamp, data=data)


def _timestamp(millis) -> datetime:
    """Ora del messaggio (millisecondi); se manca o è fuori scala (nanosecondi, NaN, 1e300) quella di adesso."""
    if isinstance(millis, int | float) and not isinstance(millis, bool):
        try:
            return datetime.fromtimestamp(millis / 1000, tz=UTC)
        except (ValueError, OverflowError, OSError):
            _LOGGER.debug("Ora del messaggio non valida: %s", millis)
    return datetime.now(tz=UTC)


def mqtt_error_text(err: Exception) -> str:
    """Motivo per cui il broker non è collegato, in italiano; il testo della libreria resta nel log."""
    text = str(err).lower()
    if isinstance(err, ValueError) and any(s in text for s in ("topic", "filter", "wildcard")):
        return INVALID_TOPIC
    if any(s in text for s in ("user name or password", "username or password", "not authori", "code:134", "code:135")):
        return "utente o password del broker non validi"
    if any(s in text for s in ("refused", "unreachable", "unavailable", "timed out", "timeout", "errno")):
        return "broker non raggiungibile"
    if "disconnect" in text or "lost" in text:
        return "collegamento con il broker interrotto"
    return "collegamento con il broker non riuscito"


def topic_sources(topic: str) -> dict[str, str]:
    """Coppie nome/valore dopo $source nel topic: …/AccessGranted/Anonymous/$source/AccessPointToken/<token>."""
    marker = "/$source/"
    if marker not in topic:
        return {}
    parts = topic.split(marker, 1)[1].strip("/").split("/")
    return {parts[i]: parts[i + 1] for i in range(0, len(parts) - 1, 2) if parts[i]}


def event_from_log(entry: dict) -> Event | None:
    """Evento del registro interno del controller nello stesso formato di quelli MQTT.

    Il topic è spezzato in topic0..topic3; sorgente e dati (DoorToken, Card, AccessPointToken...) sono le altre chiavi.
    """
    values = {str(kv.get("Key")): kv.get("Value") for kv in entry.get("KeyValues") or [] if kv.get("Key")}
    levels = [str(values[f"topic{i}"]) for i in range(8) if values.get(f"topic{i}")]
    if not levels:
        return None
    try:
        timestamp = datetime.fromisoformat(str(entry.get("UtcTime") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    # In UTC come quelli MQTT: il registro li ordina per ora confrontando il testo
    timestamp = timestamp.replace(tzinfo=UTC) if timestamp.tzinfo is None else timestamp.astimezone(UTC)
    data = {k: v for k, v in values.items() if not k.startswith("topic")}
    # Barra finale come nei topic MQTT, che proseguono con le sorgenti (…/DoorToken/<porta>)
    return Event(category=levels[0], topic="/".join(levels) + "/", timestamp=timestamp, data=data)


def event_from_ws(notification: dict) -> Event | None:
    """Evento del WebSocket del controller (events:notify) nello stesso formato di quelli MQTT.

    Il topic ha i namespace (tns1:Door/State/DoorMode, tns1:IdPoint/tnsaxis:Request/IdData) e le sorgenti
    nel messaggio: si tolgono i namespace e le sorgenti si mettono nel topic dopo $source, come li pubblica l'A1210
    su MQTT (…/DoorMode/$source/DoorToken/<porta>, …/IO/Port/$source/port/0).
    """
    if not isinstance(notification, dict) or not isinstance(notification.get("topic"), str):
        return None
    levels = [level.rsplit(":", 1)[-1] for level in notification["topic"].split("/") if level]
    if not levels:
        return None
    message = notification.get("message") if isinstance(notification.get("message"), dict) else {}
    source = message.get("source") if isinstance(message.get("source"), dict) else {}
    path = "/".join(levels)
    if source:
        # Un valore con la barra spezzerebbe le coppie del topic: nei dati resta quello vero
        path += "/$source/" + "/".join(f"{k}/{str(v).replace('/', '_')}" for k, v in source.items())
    data: dict = {}
    for part in ("source", "key", "data"):
        value = message.get(part)
        if isinstance(value, dict):
            data.update(value)
    return Event(category=levels[0], topic=path, timestamp=_timestamp(notification.get("timestamp")), data=data)


# Dati che distinguono due eventi con lo stesso topic e la stessa ora (vedi EventMonitor.apply_event): sorgenti e
# valore. Non tutti i dati: un campo in più su un canale farebbe sembrare diversi due eventi uguali
EVENT_IDENTITY = ("DoorToken", "AccessPointToken", "IdPointToken", "port", "CredentialToken", "Card", "State", "state")
EVENT_IDENTITY += ("Open", "Active")
# Eventi recenti ricordati per riconoscere quelli arrivati due volte (WebSocket e MQTT)
RECENT_EVENTS = 2048


def event_key(event: Event) -> tuple:
    """Impronta di un evento del controller, uguale via WebSocket e via MQTT: topic senza le sorgenti, ora al
    millisecondo e sorgenti. Due eventi diversi non hanno lo stesso topic, la stessa sorgente e lo stesso istante."""
    base = event.topic.split("/$source/", 1)[0].rstrip("/")
    return base, event.timestamp, tuple((k, str(event.data[k])) for k in EVENT_IDENTITY if k in event.data)


def _later(value: str, than: str) -> bool:
    """Ora ISO successiva a un'altra; False se una delle due manca o non si legge."""
    try:
        return bool(value and than) and datetime.fromisoformat(value) > datetime.fromisoformat(than)
    except ValueError:
        return False


def door_of_topic(topic: str) -> str:
    """Token della porta nei topic come Door/State/DoorMode/.../DoorToken/<token>."""
    marker = "/DoorToken/"
    return topic.rsplit(marker, 1)[1].split("/", 1)[0] if marker in topic else ""


# Condizioni della porta tenute come stato: topic → chiave; attive se lo stato non è "Normal",
# la manomissione solo con "TamperDetected" (come nel vecchio package di Home Assistant)
DOOR_CONDITIONS = {
    "Door/State/DoorAlarm/": "allarme",
    "Door/State/DoorWarning/": "preallarme",
    "Door/State/DoorTamper/": "manomissione",
}
IO_PORT_MARKER = "/port/"


@dataclass
class CategoryStats:
    count: int = 0
    last_topic: str = ""
    last_seen: datetime | None = None


@dataclass
class EventMonitor:
    """Tiene lo stato della connessione MQTT e degli eventi visti dall'avvio."""

    prefix: str
    connected: bool = False
    controller_online: bool | None = None
    # Da quando l'app sa il controller scollegato dal broker (ISO, UTC); vuoto se collegato
    controller_offline_since: str = ""
    # Da quando l'app non è collegata al broker (ISO, UTC); vuoto se collegata
    disconnected_since: str = ""
    last_error: str = ""
    categories: dict[str, CategoryStats] = field(default_factory=dict)
    last_card: dict | None = None
    door_token: str = ""
    # Modalità di ogni porta del controller, per token
    door_modes: dict[str, str] = field(default_factory=dict)
    # Allarme, preallarme e manomissione di ogni porta: token → chiave → {"active", "state", "ts"}
    door_conditions: dict[str, dict[str, dict]] = field(default_factory=dict)
    # Diagnostica del controller: coperchio aperto, ultimo avvio, ingressi I/O (numero → attivo)
    casing_open: bool | None = None
    # Da quando il coperchio è aperto o chiuso (ISO, ora del controller); vuoto se non si sa
    casing_since: str = ""
    system_ready: str = ""
    io_ports: dict[str, bool] = field(default_factory=dict)
    # Ingressi I/O cambiati dopo il primo valore ricevuto (dall'avvio dell'app o del controller)
    io_changed: set[str] = field(default_factory=set)
    # Porta aperta o chiusa secondo il contatto porta (DoorPhysicalState: Open, Closed, Fault...), per token
    door_physical: dict[str, str] = field(default_factory=dict)
    # Porte con il contatto porta configurato sul controller (DoorMonitor), per token: senza, il contatto non si sa
    door_contacts: dict[str, bool] = field(default_factory=dict)
    # Il controller risponde all'app (controllo periodico delle API): None finché non si sa. Quando non risponde lo
    # stato qui sopra si svuota, e app e Home Assistant lo mostrano sconosciuto invece dell'ultimo valore
    reachable: bool | None = None
    # Ultima lettura completa o periodica dello stato con le API (ISO, UTC)
    state_read_at: str = ""
    # Cambi di stato arrivati dagli eventi: numero dell'ultimo cambio di ogni valore. Una lettura con le API
    # iniziata prima non lo sovrascrive (vedi apply_snapshot)
    seq: int = 0
    changed: dict[tuple, int] = field(default_factory=dict)
    # Eventi in diretta dal WebSocket del controller (vedi live_events): con il WebSocket collegato, broker e
    # controller scollegati da MQTT non fanno perdere eventi
    ws_live: bool = False
    # Impronte degli ultimi eventi (event_key), dalla più vecchia: lo stesso evento via WebSocket e MQTT conta una volta
    recent: OrderedDict = field(default_factory=OrderedDict)
    # Chiamata quando il controller va online o offline
    on_status: Callable[[], None] | None = None
    # Chiamata per ogni evento; retained=True per i messaggi conservati dal broker
    on_event: Callable[[Event, bool], None] | None = None
    # Integrazione con Home Assistant: chiamata a ogni connessione, comandi ricevuti e messaggio di addio
    on_connect: Callable[[aiomqtt.Client], Awaitable[None]] | None = None
    command_topic: str = ""
    on_command: Callable[[str], Awaitable[None]] | None = None
    will: aiomqtt.Will | None = None
    client: aiomqtt.Client | None = field(default=None, repr=False)
    # Altri topic da seguire (anche con # e +): filtro → funzione(topic, payload, conservato dal broker)
    extra_subscriptions: dict[str, Callable[[str, bytes, bool], None]] = field(default_factory=dict)
    # Interruzioni (broker perso o controller offline): dopo ognuna si recuperano gli eventi persi
    outages: int = 0
    _tasks: set = field(default_factory=set, repr=False)

    @property
    def door_mode(self) -> str | None:
        """Modalità della porta principale."""
        return self.door_modes.get(self.door_token)

    @door_mode.setter
    def door_mode(self, value: str | None) -> None:
        if value is None:
            self.door_modes.pop(self.door_token, None)
        else:
            self.door_modes[self.door_token] = value

    async def publish(self, topic: str, payload: str | bytes, retain: bool = False) -> bool:
        """Pubblica se collegato; restituisce False se il broker non è disponibile."""
        client = self.client
        if client is None or not self.connected:
            return False
        try:
            await client.publish(topic, payload, qos=1, retain=retain)
        except aiomqtt.MqttError as err:
            _LOGGER.warning("Pubblicazione su %s non riuscita: %s", topic, err)
            return False
        return True

    def _dispatch_extra(self, topic: aiomqtt.Topic, payload: bytes, retained: bool = False) -> bool:
        handled = False
        for topic_filter, handler in self.extra_subscriptions.items():
            if topic.matches(topic_filter):
                handled = True
                try:
                    handler(topic.value, payload, retained)
                except Exception:
                    _LOGGER.exception("Errore nella gestione del messaggio %s", topic.value)
        return handled

    def _command(self, payload: bytes) -> None:
        if self.on_command is None:
            return
        command = payload.decode(errors="replace").strip()
        task = asyncio.get_running_loop().create_task(self.on_command(command))
        # Tiene un riferimento finché il comando non è finito
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def handle(self, topic: str, payload: bytes, retained: bool = False) -> None:
        if topic == f"{self.prefix}/stato":
            online = payload.decode(errors="replace") == "online"
            if not online and self.controller_online is not False:
                # Con il WebSocket collegato gli eventi non si perdono: niente da recuperare
                if not self.ws_live:
                    self.outages += 1
                self.controller_offline_since = datetime.now(tz=UTC).isoformat(timespec="seconds")
            if online:
                self.controller_offline_since = ""
            elif not self.ws_live:
                # Al riavvio il controller rimanda gli ingressi da capo, e il primo valore non conta come cambio.
                # Con il WebSocket collegato il controller non si è riavviato: è solo scollegato dal broker
                self.io_ports.clear()
                self.io_changed.clear()
                self.recent.clear()
            self.controller_online = online
            if self.on_status:
                self.on_status()
            return
        event = parse_event(self.prefix, topic, payload)
        if event is not None:
            self.apply_event(event, retained)

    def apply_event(self, event: Event, retained: bool = False) -> bool:
        """Evento del controller arrivato in diretta, dal WebSocket o da MQTT. Lo stesso evento arrivato da tutti e
        due (o ripetuto dal broker) si applica una volta sola: False se era già arrivato."""
        key = event_key(event)
        if key in self.recent:
            self.recent.move_to_end(key)
            return False
        self.recent[key] = None
        while len(self.recent) > RECENT_EVENTS:
            self.recent.popitem(last=False)
        stats = self.categories.setdefault(event.category, CategoryStats())
        stats.count += 1
        stats.last_topic = event.topic
        stats.last_seen = event.timestamp
        if event.topic.startswith("IdPoint/Request/IdData") and "Card" in event.data:
            self.last_card = {"card": event.data["Card"], "read_at": event.timestamp}
        if event.topic.startswith("Door/State/DoorMode/") and "State" in event.data:
            token = door_of_topic(event.topic)
            if token:
                self.door_modes[token] = event.data["State"]
                self._mark(("mode", token))
        self._track_state(event)
        if self.on_event:
            try:
                self.on_event(event, retained)
            except Exception:
                _LOGGER.exception("Errore nella gestione dell'evento %s", event.topic)
        return True

    def input_active(self, port: str | None, pushbutton: bool = False) -> bool | None:
        """Ingresso I/O attivo (None se non si sa). Gli AUX collegati come pulsante o relè (inout pu/gnd con un
        utente REX) il controller li manda al contrario degli ingressi classici: al banco 1 a riposo, 0 alla
        pressione (il REX scatta 40-50 ms dopo il passaggio a 0), e 0 dall'avvio fino alla prima pressione. La
        lettura con le API (getPorts: closed con normalState open, cioè 1) è giusta da subito e vale come un cambio."""
        value = self.io_ports.get(port) if port is not None else None
        if value is None or not pushbutton:
            return value
        return port in self.io_changed and not value

    def _track_state(self, event: Event) -> None:
        """Stati che Home Assistant mostra come sensori acceso/spento."""
        data, topic = event.data, event.topic
        ts = event.timestamp.isoformat(timespec="seconds")
        for prefix, key in DOOR_CONDITIONS.items():
            if topic.startswith(prefix) and "State" in data:
                state = str(data["State"])
                active = state == "TamperDetected" if key == "manomissione" else state != "Normal"
                token = door_of_topic(topic) or self.door_token
                self.door_conditions.setdefault(token, {})[key] = {"active": active, "state": state, "ts": ts}
                self._mark(("condition", token, key))
        if topic.startswith("Device/Casing/Open") and "Open" in data:
            casing = str(data["Open"]) == "1"
            # Un messaggio uguale (conservato dal broker, ripetuto) non sposta l'ora dell'apertura
            if casing != self.casing_open or not self.casing_since:
                self.casing_since = ts
            self.casing_open = casing
            self._mark(("casing",))
        elif topic.startswith("Device/Status/SystemReady"):
            self.system_ready = ts
        elif topic.startswith("Device/IO/Port") and IO_PORT_MARKER in topic and "state" in data:
            port = topic.rsplit(IO_PORT_MARKER, 1)[1].split("/", 1)[0]
            active = str(data["state"]) == "1"
            if port in self.io_ports and self.io_ports[port] != active:
                self.io_changed.add(port)
            self.io_ports[port] = active
            self._mark(("io", port))
        elif topic.startswith("Door/State/DoorPhysicalState") and "State" in data:
            token = door_of_topic(topic) or self.door_token
            self.door_physical[token] = str(data["State"])
            self._mark(("physical", token))

    def set_door_mode(self, token: str, mode: str) -> None:
        """Modalità della porta saputa dall'app (letta prima di aprire, o appena aperta): vale come un evento."""
        self.door_modes[token] = mode
        self._mark(("mode", token))

    def _mark(self, key: tuple) -> None:
        self.seq += 1
        self.changed[key] = self.seq

    def forget_state(self) -> None:
        """Controller che non risponde: porte, contatti, allarmi, ingressi e coperchio tornano sconosciuti."""
        self.door_modes.clear()
        self.door_physical.clear()
        self.door_conditions.clear()
        self.casing_open = None
        self.casing_since = ""
        self.io_ports.clear()
        self.io_changed.clear()
        # Al ritorno il controller rimanda lo stato con le ore di prima: non sono doppioni da scartare
        self.recent.clear()

    def apply_snapshot(self, snapshot: StateSnapshot, since: int) -> bool:
        """Stato letto con le API (vedi controller_state). Un valore arrivato da un evento dopo l'inizio della
        lettura (numero since) è più recente e resta. True se qualcosa è cambiato."""
        changed = False

        def fresh(key: tuple) -> bool:
            return self.changed.get(key, 0) <= since

        def put(target: dict, key, value, mark: tuple) -> None:
            nonlocal changed
            if fresh(mark) and target.get(key) != value:
                target[key] = value
                changed = True

        for token in snapshot.doors:
            # Un valore che la lettura non dà è sconosciuto, non l'ultimo: contatto porta tolto (niente
            # DoorPhysicalState), porta che il controller non conosce più
            for target, value, mark in (
                (self.door_modes, snapshot.modes.get(token), ("mode", token)),
                (self.door_physical, snapshot.physical.get(token), ("physical", token)),
            ):
                if value:
                    put(target, token, value, mark)
                elif fresh(mark) and token in target:
                    del target[token]
                    changed = True
        for token, conditions in snapshot.conditions.items():
            for key, state in conditions.items():
                current = self.door_conditions.get(token, {}).get(key)
                if not fresh(("condition", token, key)) or (current and current["state"] == state):
                    continue
                active = state == "TamperDetected" if key == "manomissione" else state != "Normal"
                ts = snapshot.changed_at.get((token, key), snapshot.at)
                self.door_conditions.setdefault(token, {})[key] = {"active": active, "state": state, "ts": ts}
                changed = True
        for port, active in snapshot.io.items():
            if fresh(("io", port)):
                put(self.io_ports, port, active, ("io", port))
                # Letto dal controller, il valore è affidabile anche per pulsanti e relè: dopo l'avvio del controller
                # gli eventi li danno a 0 fino alla prima pressione (vedi input_active)
                self.io_changed.add(port)
        if snapshot.casing is not None and fresh(("casing",)):
            # Chiuso e riaperto tra due letture, senza eventi in diretta: il valore è lo stesso ma è un'altra apertura.
            # Il controller che si riavvia registra di nuovo il coperchio con lo stesso valore: quello non conta
            again = _later(snapshot.casing_before, self.casing_since)
            if self.casing_open != snapshot.casing or again or (snapshot.casing_at and not self.casing_since):
                self.casing_since = snapshot.casing_at or snapshot.at
                changed = True
            self.casing_open = snapshot.casing
        if snapshot.system_ready > self.system_ready:
            self.system_ready = snapshot.system_ready
            changed = True
        if snapshot.contacts is not None and self.door_contacts != snapshot.contacts:
            self.door_contacts = dict(snapshot.contacts)
            changed = True
        self.state_read_at = snapshot.at
        return changed

    async def run(self, settings: MqttSettings) -> None:
        """Resta collegato al broker, riconnettendosi in caso di errore."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.host,
                    port=settings.port,
                    username=settings.username,
                    password=settings.password,
                    will=self.will,
                ) as client:
                    await client.subscribe(f"{self.prefix}/stato", qos=1)
                    await client.subscribe(f"{self.prefix}/eventi/#", qos=1)
                    if self.command_topic:
                        await client.subscribe(self.command_topic, qos=1)
                    for topic_filter in self.extra_subscriptions:
                        await client.subscribe(topic_filter, qos=1)
                    self.client = client
                    self.connected = True
                    self.disconnected_since = ""
                    self.last_error = ""
                    _LOGGER.info("Collegato al broker MQTT %s:%s", settings.host, settings.port)
                    if self.on_connect:
                        try:
                            await self.on_connect(client)
                        except aiomqtt.MqttError:
                            raise
                        except Exception:
                            _LOGGER.exception("Errore nella pubblicazione delle entità per Home Assistant")
                    async for message in client.messages:
                        try:
                            self._message(message)
                        except Exception:
                            # Un messaggio strano non deve fermare l'ascolto: eventi, pulsanti di HA e citofono
                            _LOGGER.exception("Messaggio MQTT su %s non elaborato", message.topic.value)
            except Exception as err:  # noqa: BLE001
                # Errori del broker, ma anche imprevisti: si risulta scollegati e si riprova, così l'interruzione
                # si vede nell'app e dopo si recuperano gli eventi persi
                if self.connected and not self.ws_live:
                    # Senza il WebSocket gli eventi persi si recuperano quando il broker torna
                    self.outages += 1
                if self.connected or not self.disconnected_since:
                    self.disconnected_since = datetime.now(tz=UTC).isoformat(timespec="seconds")
                self.client = None
                self.connected = False
                self.last_error = mqtt_error_text(err)
                if isinstance(err, aiomqtt.MqttError) or self.last_error == INVALID_TOPIC:
                    _LOGGER.warning("MQTT non disponibile: %s; riprovo tra %s s", err, RECONNECT_DELAY)
                else:
                    _LOGGER.exception("Ascolto MQTT interrotto; riprovo tra %s s", RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    def _message(self, message: aiomqtt.Message) -> None:
        payload = message.payload
        if not isinstance(payload, bytes):
            payload = str(payload).encode()
        topic = message.topic.value
        if self.command_topic and topic == self.command_topic:
            if not message.retain:
                self._command(payload)
            return
        if self._dispatch_extra(message.topic, payload, bool(message.retain)):
            return
        self.handle(topic, payload, bool(message.retain))
