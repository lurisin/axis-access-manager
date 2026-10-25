"""Entità di Home Assistant pubblicate con MQTT Discovery: porta, contatori, ultimo accesso, pulsanti, eventi."""

from __future__ import annotations

import json
import logging

import aiomqtt

from .config import Settings
from .db import Database, local_day_start
from .events import EventMonitor

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "axis_access_manager"

DOOR_MODES = {
    "Locked": "Bloccata",
    "Accessed": "Aperta a Tempo",
    "Unlocked": "Sbloccata",
    "LockedOpen": "Aperta",
    "Blocked": "Bloccata da Regola",
    "LockedDown": "In Blocco",
    "DoubleLocked": "Doppia Chiusura",
}

# Tipo di evento del log → tipo di evento in Home Assistant
EVENT_TYPES = {
    "concesso": "accesso_concesso",
    "negato": "accesso_negato",
    "lettura": "tessera_letta",
    "sconosciuta": "tessera_sconosciuta",
    "apertura_remota": "apertura_remota",
    "allarme": "allarme_porta",
    "manomissione": "manomissione",
    "citofono": "citofono",
}

COMMANDS = {"apri": False, "apri_lungo": True}


def parse_command(command: str) -> tuple[bool, str] | None:
    """Comando dei pulsanti: "apri" o "apri_lungo", seguito da ":<porta>" per le porte oltre la prima."""
    action, _, door = command.partition(":")
    if action not in COMMANDS:
        return None
    return COMMANDS[action], door


def door_label(mode: str | None) -> str:
    return DOOR_MODES.get(mode, mode) if mode else "Sconosciuto"


class HaBridge:
    def __init__(
        self, settings: Settings, monitor: EventMonitor, db: Database, doors: list[dict] | None = None
    ) -> None:
        self.settings = settings
        self.monitor = monitor
        self.db = db
        # Porte configurate nell'app: la prima mantiene gli identificativi delle versioni precedenti
        self.doors = (
            doors
            if doors is not None
            else [{"id": 0, "door_token": settings.door_token, "name": "Porta", "ha_buttons": settings.ha_door_buttons}]
        )
        base = settings.ha_topic_prefix
        self.availability_topic = f"{base}/disponibile"
        self.state_topic = f"{base}/stato"
        self.last_topic = f"{base}/ultimo"
        self.event_topic = f"{base}/evento"
        self.command_topic = f"{base}/comando"
        self.last_access: dict = {}

    @property
    def enabled(self) -> bool:
        return self.settings.ha_integration

    @property
    def has_buttons(self) -> bool:
        return any(d.get("ha_buttons") for d in self.doors)

    def will(self) -> aiomqtt.Will | None:
        if not self.enabled:
            return None
        return aiomqtt.Will(self.availability_topic, "offline", qos=1, retain=True)

    def _device(self) -> dict:
        return {
            "identifiers": [f"{NODE_ID}_{self.settings.door_token or 'porta'}"],
            "name": "Controllo Accessi",
            "manufacturer": "Axis Access Manager",
            "model": "Axis A1210",
        }

    def _entities(self) -> list[tuple[str, str, dict]]:
        """(componente, id oggetto, configurazione) di ogni entità."""
        common = {
            "availability_topic": self.availability_topic,
            "device": self._device(),
        }
        state = {"state_topic": self.state_topic}
        entities = [
            (
                "sensor",
                "stato_porta",
                {**state, "name": "Stato Porta", "icon": "mdi:door", "value_template": "{{ value_json.porta }}"},
            ),
            (
                "sensor",
                "ultimo_accesso",
                {
                    "name": "Ultimo Accesso",
                    "icon": "mdi:card-account-details",
                    "state_topic": self.last_topic,
                    "value_template": "{{ value_json.persona or value_json.tessera or 'nessuno' }}",
                    "json_attributes_topic": self.last_topic,
                },
            ),
            (
                "sensor",
                "accessi_oggi",
                {
                    **state,
                    "name": "Accessi Oggi",
                    "icon": "mdi:login",
                    "value_template": "{{ value_json.accessi_oggi }}",
                },
            ),
            (
                "sensor",
                "negati_oggi",
                {
                    **state,
                    "name": "Accessi Negati Oggi",
                    "icon": "mdi:account-cancel",
                    "value_template": "{{ value_json.negati_oggi }}",
                },
            ),
            (
                "sensor",
                "tessere_da_registrare",
                {
                    **state,
                    "name": "Tessere da Registrare",
                    "icon": "mdi:card-plus",
                    "value_template": "{{ value_json.da_registrare }}",
                },
            ),
            (
                "sensor",
                "persone_registrate",
                {
                    **state,
                    "name": "Persone Registrate",
                    "icon": "mdi:account-group",
                    "value_template": "{{ value_json.persone }}",
                },
            ),
            (
                "event",
                "eventi",
                {
                    "name": "Eventi Accessi",
                    "icon": "mdi:shield-account",
                    "state_topic": self.event_topic,
                    "event_types": sorted(set(EVENT_TYPES.values())),
                },
            ),
        ]
        for index, door in enumerate(self.doors):
            # Prima porta: nomi e identificativi di sempre; le altre hanno il loro nome e id
            suffix, label, extra = (
                ("", "Porta", "") if index == 0 else (f"_{door['id']}", door["name"], f":{door['door_token']}")
            )
            if index:
                entities.append(
                    (
                        "sensor",
                        f"stato_porta{suffix}",
                        {
                            **state,
                            "name": f"Stato {label}",
                            "icon": "mdi:door",
                            "value_template": f"{{{{ value_json.porte['{door['door_token']}'] }}}}",
                        },
                    )
                )
            if not door.get("ha_buttons"):
                continue
            entities += [
                (
                    "button",
                    f"apri_porta{suffix}",
                    {
                        "name": f"Apri {label}",
                        "icon": "mdi:door-open",
                        "command_topic": self.command_topic,
                        "payload_press": f"apri{extra}",
                    },
                ),
                (
                    "button",
                    f"apri_porta_a_lungo{suffix}",
                    {
                        "name": f"Apri {label} a Lungo",
                        "icon": "mdi:door-open",
                        "command_topic": self.command_topic,
                        "payload_press": f"apri_lungo{extra}",
                    },
                ),
            ]
        result = []
        for component, object_id, config in entities:
            config = {**common, **config, "unique_id": f"{NODE_ID}_{object_id}"}
            result.append((component, object_id, config))
        return result

    def _all_object_ids(self) -> list[tuple[str, str]]:
        ids = [(c, o) for c, o, _ in self._entities()]
        for extra in (("button", "apri_porta"), ("button", "apri_porta_a_lungo")):
            if extra not in ids:
                ids.append(extra)
        return ids

    async def on_connect(self, client: aiomqtt.Client) -> None:
        """Pubblica (o rimuove) le entità a ogni connessione al broker."""
        if not self.enabled:
            for component, object_id in self._all_object_ids():
                await client.publish(self._config_topic(component, object_id), b"", qos=1, retain=True)
            return
        configured = {(c, o) for c, o, _ in self._entities()}
        for component, object_id in self._all_object_ids():
            if (component, object_id) not in configured:
                await client.publish(self._config_topic(component, object_id), b"", qos=1, retain=True)
        for component, object_id, config in self._entities():
            await client.publish(self._config_topic(component, object_id), json.dumps(config), qos=1, retain=True)
        await client.publish(self.availability_topic, "online", qos=1, retain=True)
        await self._publish_state(client)
        await client.publish(self.last_topic, json.dumps(self._last_payload()), qos=1, retain=True)
        _LOGGER.info("Entità pubblicate in Home Assistant (MQTT Discovery)")

    @staticmethod
    def _config_topic(component: str, object_id: str) -> str:
        return f"{DISCOVERY_PREFIX}/{component}/{NODE_ID}/{object_id}/config"

    def state(self) -> dict:
        counts = self.db.count_events(local_day_start())
        mode = self.monitor.door_mode
        return {
            "porta": door_label(mode),
            "modalita": mode,
            "porte": {d["door_token"]: door_label(self.monitor.door_modes.get(d["door_token"])) for d in self.doors},
            "accessi_oggi": counts.get("concesso", 0) + counts.get("apertura_remota", 0),
            "negati_oggi": counts.get("negato", 0) + counts.get("sconosciuta", 0),
            "da_registrare": len(self.db.list_unknown_cards()),
            "persone": len(self.db.all_persons()),
        }

    async def _publish_state(self, client: aiomqtt.Client) -> None:
        await client.publish(self.state_topic, json.dumps(self.state()), qos=1, retain=True)

    async def publish_state(self) -> None:
        if self.enabled:
            await self.monitor.publish(self.state_topic, json.dumps(self.state()), retain=True)

    def _last_payload(self) -> dict:
        return self.last_access or {"persona": "", "tessera": "", "esito": "", "ora": ""}

    async def publish_event(self, row: dict) -> None:
        if not self.enabled:
            return
        event_type = EVENT_TYPES.get(row.get("kind", ""))
        if event_type is None:
            return
        payload = {
            "event_type": event_type,
            "persona": row.get("person_name") or "",
            "tessera": row.get("card") or "",
            "dettaglio": row.get("detail") or "",
            "origine": row.get("source") or "",
            "ora": row.get("ts") or "",
        }
        await self.monitor.publish(self.event_topic, json.dumps(payload))
        if row.get("kind") in ("concesso", "negato", "sconosciuta", "lettura", "apertura_remota"):
            self.last_access = {
                "persona": payload["persona"] or (payload["origine"] if row.get("kind") == "apertura_remota" else ""),
                "tessera": payload["tessera"],
                "esito": event_type,
                "dettaglio": payload["dettaglio"],
                "ora": payload["ora"],
            }
            await self.monitor.publish(self.last_topic, json.dumps(self.last_access), retain=True)
