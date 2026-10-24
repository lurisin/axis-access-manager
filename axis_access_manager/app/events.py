"""Ascolto degli eventi che l'A1210 pubblica via MQTT."""

from __future__ import annotations

import asyncio
import json
import logging
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

    def handle(self, topic: str, payload: bytes) -> None:
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

    async def run(self, settings: MqttSettings) -> None:
        """Resta collegato al broker, riconnettendosi in caso di errore."""
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=settings.host,
                    port=settings.port,
                    username=settings.username,
                    password=settings.password,
                ) as client:
                    await client.subscribe(f"{self.prefix}/stato", qos=1)
                    await client.subscribe(f"{self.prefix}/eventi/#", qos=1)
                    self.connected = True
                    self.last_error = ""
                    _LOGGER.info("Collegato al broker MQTT %s:%s", settings.host, settings.port)
                    async for message in client.messages:
                        payload = message.payload
                        if not isinstance(payload, bytes):
                            payload = str(payload).encode()
                        self.handle(message.topic.value, payload)
            except aiomqtt.MqttError as err:
                self.connected = False
                self.last_error = str(err)
                _LOGGER.warning("MQTT non disponibile: %s; riprovo tra %s s", err, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
