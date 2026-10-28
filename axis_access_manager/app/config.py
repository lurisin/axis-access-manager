"""Configurazione dell'add-on: opzioni utente e broker MQTT."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

OPTIONS_PATH = Path("/data/options.json")
SUPERVISOR_MQTT_URL = "http://supervisor/services/mqtt"
# Con s6-overlay il processo principale non eredita le variabili del container:
# il Supervisor token si trova anche qui
S6_ENV_DIR = Path("/run/s6/container_environment")
# Prefissi MQTT: senza caratteri jolly (# +) né spazi, come quelli di controller e citofono in Impianti
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_\-/]+$")
DEFAULT_HA_TOPIC_PREFIX = "axis_access_manager"

_LOGGER = logging.getLogger(__name__)


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
    access_profile: str = ""
    event_retention_days: int = 365
    ha_integration: bool = True
    ha_door_buttons: bool = True
    ha_topic_prefix: str = DEFAULT_HA_TOPIC_PREFIX
    snapshot_camera: str = ""
    snapshot_on_granted: bool = False
    readonly_users: tuple[str, ...] = ()
    intercom_name: str = ""
    intercom_open_door: bool = False
    intercom_relay: int = 1
    intercom_mqtt_prefix: str = "hmd"


def normalize_host(value: str) -> str:
    """Accetta anche "https://172.27.0.1/" o "http://host/percorso" e tiene solo l'host."""
    host = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", value.strip())
    return host.split("/", 1)[0]


def container_env(name: str, env_dir: Path = S6_ENV_DIR) -> str | None:
    """Variabile del container, anche quando s6-overlay non la passa al processo."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return (env_dir / name).read_text().strip() or None
    except OSError:
        return None


def supervisor_token(env_dir: Path = S6_ENV_DIR) -> str | None:
    return container_env("SUPERVISOR_TOKEN", env_dir)


def apply_timezone(env_dir: Path = S6_ENV_DIR) -> str | None:
    """Usa il fuso orario di Home Assistant per giornate, statistiche e file CSV."""
    tz = container_env("TZ", env_dir)
    if tz and os.environ.get("TZ") != tz:
        os.environ["TZ"] = tz
        if hasattr(time, "tzset"):
            time.tzset()
    return tz


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
        a1210_host=normalize_host(options.get("a1210_host", "")),
        a1210_username=options.get("a1210_username", ""),
        a1210_password=options.get("a1210_password", ""),
        a1210_verify_ssl=bool(options.get("a1210_verify_ssl", False)),
        door_token=options.get("door_token", "").strip(),
        mqtt_topic_prefix=options.get("mqtt_topic_prefix", "").strip().rstrip("/"),
        mqtt=mqtt,
        log_level=options.get("log_level", "info"),
        access_profile=options.get("access_profile", "").strip(),
        event_retention_days=int(options.get("event_retention_days") or 365),
        ha_integration=bool(options.get("ha_integration", True)),
        ha_door_buttons=bool(options.get("ha_door_buttons", True)),
        ha_topic_prefix=ha_topic_prefix(options.get("ha_topic_prefix")),
        snapshot_camera=(options.get("snapshot_camera") or "").strip(),
        snapshot_on_granted=bool(options.get("snapshot_on_granted", False)),
        readonly_users=tuple(u.lower() for u in _string_list(options.get("readonly_users"))),
        intercom_name=(options.get("intercom_name") or "").strip(),
        intercom_open_door=bool(options.get("intercom_open_door", False)),
        intercom_relay=int(options.get("intercom_relay") or 1),
        intercom_mqtt_prefix=(options.get("intercom_mqtt_prefix") or "hmd").strip().strip("/"),
    )


def ha_topic_prefix(value) -> str:
    """Prefisso delle entità di Home Assistant; uno non valido (vuoto, con # o +) torna al predefinito.

    Con # o + l'iscrizione al topic dei comandi fallirebbe e si fermerebbe l'ascolto degli eventi.
    """
    prefix = str(value or "").strip().strip("/")
    if TOPIC_PATTERN.match(prefix):
        return prefix
    if prefix:
        _LOGGER.warning(
            "Prefisso MQTT di Home Assistant non valido (%s): usa lettere, numeri, _ - e /. Uso %s",
            prefix,
            DEFAULT_HA_TOPIC_PREFIX,
        )
    return DEFAULT_HA_TOPIC_PREFIX


def _string_list(value) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    return tuple(item.strip() for item in value or [] if str(item).strip())


def load_settings(path: Path = OPTIONS_PATH) -> Settings:
    return parse_options(json.loads(path.read_text()))


async def resolve_mqtt(
    settings: Settings, client: httpx.AsyncClient | None = None, token: str | None = None, quiet: bool = False
) -> MqttSettings | None:
    """Restituisce il broker da usare: quello nelle opzioni o quello del Supervisor.

    quiet: nuovo tentativo dopo l'avvio, ogni minuto finché il broker non c'è; il motivo va nel log solo in debug,
    perché l'avviso all'avvio c'è già e senza Mosquitto riempirebbe il log."""
    if settings.mqtt:
        return settings.mqtt
    level = logging.DEBUG if quiet else logging.WARNING
    token = token or supervisor_token()
    if not token:
        _LOGGER.log(level, "Token del Supervisor non disponibile: imposta mqtt_host nelle opzioni")
        return None
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        response = await client.get(SUPERVISOR_MQTT_URL, headers={"Authorization": f"Bearer {token}"})
        if response.status_code != 200:
            _LOGGER.log(
                level,
                "Il Supervisor non fornisce un broker MQTT (risposta %s): installa l'add-on Mosquitto "
                "oppure imposta mqtt_host nelle opzioni",
                response.status_code,
            )
            return None
        data = response.json().get("data", {})
        return MqttSettings(
            host=data["host"],
            port=int(data.get("port", 1883)),
            username=data.get("username"),
            password=data.get("password"),
        )
    except (httpx.HTTPError, KeyError, ValueError) as err:
        _LOGGER.log(level, "Richiesta del broker MQTT al Supervisor non riuscita: %s", err)
        return None
    finally:
        if owns_client:
            await client.aclose()
