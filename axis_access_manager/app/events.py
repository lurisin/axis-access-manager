"""Ascolto degli eventi che l'A1210 pubblica via MQTT."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiomqtt

from .config import MqttSettings

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 10
# Filtro di iscrizione rifiutato dalla libreria MQTT (per esempio # o + nel prefisso): si riprova senza traceback
INVALID_TOPIC = "topic MQTT non valido"


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
    system_ready: str = ""
    io_ports: dict[str, bool] = field(default_factory=dict)
    # Ingressi I/O cambiati dopo il primo valore ricevuto (dall'avvio dell'app o del controller)
    io_changed: set[str] = field(default_factory=set)
    # Porta aperta o chiusa secondo il contatto porta (DoorPhysicalState: Open, Closed, Fault...), per token
    door_physical: dict[str, str] = field(default_factory=dict)
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
                self.outages += 1
                self.controller_offline_since = datetime.now(tz=UTC).isoformat(timespec="seconds")
            if online:
                self.controller_offline_since = ""
            else:
                # Al riavvio il controller rimanda gli ingressi da capo, e il primo valore non conta come cambio
                self.io_ports.clear()
                self.io_changed.clear()
            self.controller_online = online
            if self.on_status:
                self.on_status()
            return
        event = parse_event(self.prefix, topic, payload)
        if event is None:
            return
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
        self._track_state(event)
        if self.on_event:
            try:
                self.on_event(event, retained)
            except Exception:
                _LOGGER.exception("Errore nella gestione dell'evento %s", event.topic)

    def input_active(self, port: str | None, pushbutton: bool = False) -> bool | None:
        """Ingresso I/O attivo (None se non si sa). Gli AUX collegati come pulsante o relè (inout pu/gnd con un
        utente REX) il controller li manda al contrario degli ingressi classici: al banco 1 a riposo, 0 alla
        pressione (il REX scatta 40-50 ms dopo il passaggio a 0), e 0 dall'avvio fino alla prima pressione."""
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
        if topic.startswith("Device/Casing/Open") and "Open" in data:
            self.casing_open = str(data["Open"]) == "1"
        elif topic.startswith("Device/Status/SystemReady"):
            self.system_ready = ts
        elif topic.startswith("Device/IO/Port") and IO_PORT_MARKER in topic and "state" in data:
            port = topic.rsplit(IO_PORT_MARKER, 1)[1].split("/", 1)[0]
            active = str(data["state"]) == "1"
            if port in self.io_ports and self.io_ports[port] != active:
                self.io_changed.add(port)
            self.io_ports[port] = active
        elif topic.startswith("Door/State/DoorPhysicalState") and "State" in data:
            self.door_physical[door_of_topic(topic) or self.door_token] = str(data["State"])

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
                if self.connected:
                    self.outages += 1
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
