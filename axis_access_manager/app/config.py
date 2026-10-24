"""Configurazione dell'add-on: opzioni utente e broker MQTT."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class Settings:
    a1210_host: str
    a1210_username: str
    a1210_password: str
    a1210_verify_ssl: bool
    door_token: str
    mqtt_topic_prefix: str
    mqtt: MqttSettings | None
    log_level: str = "info"


def parse_options(options: dict) -> Settings:
    """Trasforma il contenuto di options.json in Settings.

    Se mqtt_host è impostato vince sulle credenziali fornite dal Supervisor.
    """
    mqtt = None
    if options.get("mqtt_host"):
        mqtt = MqttSettings(
            host=options["mqtt_host"],
            port=int(options.get("mqtt_port") or 1883),
            username=options.get("mqtt_username") or None,
            password=options.get("mqtt_password") or None,
        )
    return Settings(
        a1210_host=options.get("a1210_host", "").strip(),
        a1210_username=options.get("a1210_username", ""),
        a1210_password=options.get("a1210_password", ""),
        a1210_verify_ssl=bool(options.get("a1210_verify_ssl", False)),
        door_token=options.get("door_token", "").strip(),
        mqtt_topic_prefix=options.get("mqtt_topic_prefix", "").strip().rstrip("/"),
        mqtt=mqtt,
        log_level=options.get("log_level", "info"),
    )


def load_settings(path: Path = OPTIONS_PATH) -> Settings:
    return parse_options(json.loads(path.read_text()))


async def resolve_mqtt(settings: Settings, client: httpx.AsyncClient | None = None) -> MqttSettings | None:
    """Restituisce il broker da usare: quello nelle opzioni o quello del Supervisor."""
    if settings.mqtt:
        return settings.mqtt
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await client.get(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            return None
        data = response.json().get("data", {})
        return MqttSettings(
            host=data["host"],
            port=int(data.get("port", 1883)),
            username=data.get("username"),
            password=data.get("password"),
        )
    except (httpx.HTTPError, KeyError, ValueError):
        return None
    finally:
        if owns_client:
            await client.aclose()
