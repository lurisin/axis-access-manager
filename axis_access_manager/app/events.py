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
    millis = body.get("timestamp")
    timestamp = (
        datetime.fromtimestamp(millis / 1000, tz=UTC) if isinstance(millis, int | float) else datetime.now(tz=UTC)
    )
    data = body.get("message", {}).get("data", {})
    return Event(category=path.split("/", 1)[0], topic=path, timestamp=timestamp, data=data)


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
    last_error: str = ""
    categories: dict[str, CategoryStats] = field(default_factory=dict)
    last_card: dict | None = None
    door_token: str = ""
    door_mode: str | None = None
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
    _tasks: set = field(default_factory=set, repr=False)

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
            self.controller_online = payload.decode(errors="replace") == "online"
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
        if (
            event.topic.startswith("Door/State/DoorMode/")
            and self.door_token
            and event.topic.endswith(f"/DoorToken/{self.door_token}")
            and "State" in event.data
        ):
            self.door_mode = event.data["State"]
        if self.on_event:
            try:
                self.on_event(event, retained)
            except Exception:
                _LOGGER.exception("Errore nella gestione dell'evento %s", event.topic)

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
                        payload = message.payload
                        if not isinstance(payload, bytes):
                            payload = str(payload).encode()
                        topic = message.topic.value
                        if self.command_topic and topic == self.command_topic:
                            if not message.retain:
                                self._command(payload)
                            continue
                        if self._dispatch_extra(message.topic, payload, bool(message.retain)):
                            continue
                        self.handle(topic, payload, bool(message.retain))
            except aiomqtt.MqttError as err:
                self.client = None
                self.connected = False
                self.last_error = str(err)
                _LOGGER.warning("MQTT non disponibile: %s; riprovo tra %s s", err, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
