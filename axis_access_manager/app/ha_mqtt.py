"""Entità di Home Assistant pubblicate con MQTT Discovery: porte e loro allarmi, controller, contatori,
ultimo accesso, pulsanti, eventi."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timedelta

import aiomqtt

from .config import DEFAULT_HA_TOPIC_PREFIX, Settings
from .db import Database, local_day_start
from .events import EventMonitor
from .ha import BUTTON_COMMAND
from .programming import CONTROLLER_INPUTS, REX_ROLES

_LOGGER = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"
NODE_ID = "axis_access_manager"
# Identificativo del dispositivo in Home Assistant, salvato al primo avvio: non cambia con il token della porta
DEVICE_ID_SETTING = "ha_device_id"
# Porta che ha gli identificativi delle versioni precedenti (apri_porta, stato_porta...): token della porta sul
# controller, salvato alla prima pubblicazione. Se la porta si toglie, quegli identificativi non passano a un'altra
LEGACY_DOOR_SETTING = "ha_legacy_door"
# Allo spegnimento: tempo massimo per dire a Home Assistant che le entità non sono più disponibili
OFFLINE_TIMEOUT = 2

DOOR_MODES = {
    "Locked": "Bloccata",
    "Accessed": "Aperta a Tempo",
    "Unlocked": "Sbloccata",
    "LockedOpen": "Aperta",
    "Blocked": "Bloccata da Regola",
    "LockedDown": "In Blocco",
    "DoubleLocked": "Doppia Chiusura",
}

# Sensore dell'elettroserratura (classe lock: acceso = sbloccata). Gli altri modi, o nessun dato, danno sconosciuto
UNLOCKED_MODES = {"Accessed", "Unlocked", "LockedOpen"}
LOCKED_MODES = {"Locked", "Blocked", "LockedDown", "DoubleLocked"}

# Contatto porta (DoorPhysicalState): acceso = aperta. Senza contatto porta, o con un guasto, è sconosciuto
DOOR_PHYSICAL = {"Open": True, "Closed": False}

# Tipo di evento del log → tipo di evento in Home Assistant
EVENT_TYPES = {
    "concesso": "accesso_concesso",
    "negato": "accesso_negato",
    "lettura": "tessera_letta",
    "sconosciuta": "tessera_sconosciuta",
    "apertura_remota": "apertura_remota",
    "uscita": "uscita",
    "allarme": "allarme_porta",
    "manomissione": "manomissione",
    "citofono": "citofono",
}

# Sensori acceso/spento di ogni porta: chiave nello stato → (nome, classe, icona)
DOOR_CONDITIONS = {
    "allarme": ("Allarme", "problem", "mdi:alarm-light"),
    "preallarme": ("Preallarme", "problem", "mdi:alert"),
    "manomissione": ("Manomissione", "tamper", "mdi:shield-alert"),
}
# Ingressi I/O del controller: numero della porta → (id, nome, classe), come nel vecchio package
IO_INPUTS = {
    "0": ("ingresso_aux_1", "Ingresso AUX 1", None),
    "1": ("ingresso_aux_2", "Ingresso AUX 2", None),
    "2": ("ingresso_manomissione", "Ingresso Manomissione", "tamper"),
    "3": ("ingresso_allarme", "Ingresso Allarme", "problem"),
}
DOOR_ALARM_TYPES = {
    "Normal": "Normale",
    "DoorForcedOpen": "Porta Forzata",
    "DoorOpenTooLong": "Aperta Troppo a Lungo",
    "NotInTamper": "Normale",
    "TamperDetected": "Manomissione Rilevata",
}

COMMANDS = {"apri": False, "apri_lungo": True}
# Eventi che aggiornano il sensore Ultimo Accesso
ACCESS_KINDS = ("concesso", "negato", "sconosciuta", "lettura", "apertura_remota", "uscita")


def access_payload(row: dict) -> dict:
    """Stato del sensore Ultimo Accesso per un evento del registro."""
    remote = row.get("kind") in ("apertura_remota", "uscita")
    return {
        "persona": row.get("person_name") or ((row.get("source") or "Apertura Remota") if remote else ""),
        "tessera": row.get("card") or "",
        "esito": EVENT_TYPES.get(row.get("kind", ""), ""),
        "dettaglio": row.get("detail") or "",
        "ora": row.get("ts") or "",
    }


def _anonymous_denied(row: dict) -> bool:
    return row.get("kind") == "negato" and not row.get("card") and not row.get("person_name")


def parse_command(command: str) -> tuple[bool, str] | None:
    """Comando dei pulsanti: "apri" o "apri_lungo", seguito da ":<porta>" per le porte oltre la prima."""
    action, _, door = command.partition(":")
    if action not in COMMANDS:
        return None
    return COMMANDS[action], door


def door_label(mode: str | None) -> str:
    return DOOR_MODES.get(mode, mode) if mode else "Sconosciuto"


def lock_state(mode: str | None) -> str:
    return on_off(True if mode in UNLOCKED_MODES else False if mode in LOCKED_MODES else None)


def on_off(value: bool | None) -> str:
    """Stato di un sensore acceso/spento; "None" lo rende sconosciuto in Home Assistant."""
    return "None" if value is None else "ON" if value else "OFF"


def topic_base(prefix: str, controller_prefix: str) -> str:
    """Prefisso dei topic delle entità che non finisce tra quelli letti dal controller.

    Con lo stesso prefisso lo stato delle entità finirebbe su <prefisso>/stato, il topic online/offline del
    controller: il controller sembrerebbe offline e lo stato si ripubblicherebbe senza fine.
    """
    for base in (prefix, DEFAULT_HA_TOPIC_PREFIX):
        if not _overlaps(base, controller_prefix):
            return base
    # Il controller usa proprio il prefisso predefinito
    return f"{DEFAULT_HA_TOPIC_PREFIX}/ha"


def _overlaps(base: str, controller_prefix: str) -> bool:
    if not controller_prefix:
        return False
    filters = (f"{controller_prefix}/stato", f"{controller_prefix}/eventi/#")
    topics = [f"{base}/{name}" for name in ("disponibile", "stato", "ultimo", "evento", "comando")]
    return any(aiomqtt.Topic(topic).matches(f) for topic in topics for f in filters)


def seconds_to_midnight(now: float | None = None) -> float:
    """Secondi alla prossima mezzanotte locale (giusti anche nei giorni del cambio d'ora), più un secondo."""
    now = time.time() if now is None else now
    day = datetime.fromtimestamp(now).date() + timedelta(days=1)
    midnight = time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1))
    return max(midnight - now, 0) + 1


class HaBridge:
    def __init__(
        self, settings: Settings, monitor: EventMonitor, db: Database, doors: list[dict] | None = None
    ) -> None:
        self.settings = settings
        self.monitor = monitor
        self.db = db
        # Porte configurate nell'app: una tiene gli identificativi delle versioni precedenti (legacy_door)
        self.doors = (
            doors
            if doors is not None
            else [{"id": 0, "door_token": settings.door_token, "name": "Porta", "ha_buttons": settings.ha_door_buttons}]
        )
        base = topic_base(settings.ha_topic_prefix, monitor.prefix)
        if base != settings.ha_topic_prefix:
            _LOGGER.warning(
                "Il prefisso MQTT di Home Assistant (%s) si sovrappone a quello del controller (%s): uso %s",
                settings.ha_topic_prefix,
                monitor.prefix,
                base,
            )
        self.topic_prefix = base
        self.availability_topic = f"{base}/disponibile"
        self.state_topic = f"{base}/stato"
        self.last_topic = f"{base}/ultimo"
        self.event_topic = f"{base}/evento"
        self.command_topic = f"{base}/comando"
        # Dal registro, così dopo un riavvio il sensore non torna a "Nessuno"
        last = [r for r in db.list_events(kind=",".join(ACCESS_KINDS), limit=20) if not _anonymous_denied(r)]
        self.last_access: dict = access_payload(last[0]) if last else {}
        # Configurazioni conservate dal broker, ricevute a ogni connessione: si svuotano quelle di entità tolte
        monitor.extra_subscriptions[f"{DISCOVERY_PREFIX}/+/{NODE_ID}/+/config"] = self._on_discovery_config
        self._tasks: set[asyncio.Task] = set()
        self._midnight: asyncio.Task | None = None

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

    def _device_id(self) -> str:
        """Fino alla 0.21 era legato al token della prima porta: con un token nuovo (porta cambiata in Impianti,
        token rifiutato da un controller sostituito) Home Assistant creava un secondo dispositivo. Ora si salva
        la prima volta, con il valore di prima, e non cambia più."""
        device_id = self.db.get_setting(DEVICE_ID_SETTING)
        if not device_id:
            device_id = f"{NODE_ID}_{self.settings.door_token or 'porta'}"
            self.db.set_setting(DEVICE_ID_SETTING, device_id)
        return device_id

    def legacy_door(self) -> str:
        """Token della porta con gli identificativi di sempre ("" se nessuna porta dell'app lo ha più).

        Fino alla 0.21 li aveva la prima porta dell'elenco: con un controller tolto e aggiunto di nuovo, con le porte
        in un altro ordine, il pulsante Apri Porta di Home Assistant apriva un'altra porta."""
        saved = self.db.get_setting(LEGACY_DOOR_SETTING)
        if not saved and self.doors and self.doors[0]["door_token"]:
            saved = self.doors[0]["door_token"]
            self.db.set_setting(LEGACY_DOOR_SETTING, saved)
        return saved if any(d["door_token"] == saved for d in self.doors) else ""

    def _device(self) -> dict:
        return {
            "identifiers": [self._device_id()],
            "name": "Controllo Accessi",
            "manufacturer": "Axis Access Manager",
            "model": "Axis A1210",
        }

    def _input_names(self) -> dict[str, str]:
        """Nome scelto nell'app per gli ingressi che danno gli eventi Device/IO/Port, per numero di porta."""
        ports = {io: port for io, (_, port) in CONTROLLER_INPUTS.items() if port is not None}
        return {ports[row["io"]]: row["name"] for row in self.db.all_controller_inputs() if row["io"] in ports}

    def _pushbutton_ports(self) -> set[str]:
        """Porte I/O collegate come pulsante o relè del citofono: il loro stato arriva al contrario."""
        ports = {io: port for io, (_, port) in CONTROLLER_INPUTS.items() if port is not None}
        return {
            ports[row["io"]]
            for row in self.db.all_controller_inputs()
            if row["io"] in ports and row["role"] in REX_ROLES
        }

    def _entities(self) -> list[tuple[str, str, dict]]:
        """(componente, id oggetto, configurazione) di ogni entità."""
        common = {
            "availability_topic": self.availability_topic,
            "device": self._device(),
        }
        state = {"state_topic": self.state_topic}
        names = self._input_names()
        entities = [
            (
                "sensor",
                "ultimo_accesso",
                {
                    "name": "Ultimo Accesso",
                    "icon": "mdi:card-account-details",
                    "state_topic": self.last_topic,
                    "value_template": "{{ value_json.persona or value_json.tessera or 'Nessuno' }}",
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
                "binary_sensor",
                "controller_online",
                {
                    **state,
                    "name": "Controller Online",
                    "device_class": "connectivity",
                    "entity_category": "diagnostic",
                    "value_template": "{{ value_json.controller.online }}",
                },
            ),
            (
                "binary_sensor",
                "coperchio_controller",
                {
                    **state,
                    "name": "Coperchio Controller Aperto",
                    "device_class": "tamper",
                    "value_template": "{{ value_json.controller.coperchio }}",
                },
            ),
            (
                "sensor",
                "ultimo_avvio_controller",
                {
                    **state,
                    "name": "Ultimo Avvio Controller",
                    "device_class": "timestamp",
                    "entity_category": "diagnostic",
                    "value_template": "{{ value_json.controller.avvio }}",
                },
            ),
            *[
                (
                    "binary_sensor",
                    object_id,
                    {
                        **state,
                        "name": names.get(port) or name,
                        **({"device_class": device_class} if device_class else {}),
                        "icon": "mdi:electric-switch",
                        "value_template": f"{{{{ value_json.ingressi['{port}'] }}}}",
                    },
                )
                for port, (object_id, name, device_class) in IO_INPUTS.items()
            ],
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
        legacy = self.legacy_door()
        for door in self.doors:
            # Porta con gli identificativi di sempre: nomi e id delle versioni precedenti. Le altre hanno il loro
            # nome e il loro id, che non passa mai a un'altra porta. Il comando senza porta va alla prima porta.
            if legacy and door["door_token"] == legacy:
                suffix, label = "", "Porta"
                extra = "" if door["door_token"] == self.settings.door_token else f":{door['door_token']}"
            else:
                suffix, label, extra = f"_{door['id']}", door["name"], f":{door['door_token']}"
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
            # Bloccata (spento) o sbloccata (acceso): nelle dashboard prende il lucchetto
            entities.append(
                (
                    "binary_sensor",
                    f"elettroserratura_porta{suffix}",
                    {
                        **state,
                        "name": f"Elettroserratura {label}",
                        "device_class": "lock",
                        "value_template": f"{{{{ value_json.serrature['{door['door_token']}'] }}}}",
                    },
                )
            )
            # Aperta o chiusa secondo il contatto porta: la serratura può essere bloccata con la porta aperta
            entities.append(
                (
                    "binary_sensor",
                    f"contatto_porta{suffix}",
                    {
                        **state,
                        "name": f"Contatto {label}",
                        "device_class": "door",
                        "value_template": f"{{{{ value_json.contatti['{door['door_token']}'] }}}}",
                    },
                )
            )
            for key, (name, device_class, icon) in DOOR_CONDITIONS.items():
                path = f"value_json.condizioni['{door['door_token']}'].{key}"
                entities.append(
                    (
                        "binary_sensor",
                        f"{key}_porta{suffix}",
                        {
                            **state,
                            "name": f"{name} {label}",
                            "device_class": device_class,
                            "icon": icon,
                            "value_template": f"{{{{ {path}.stato }}}}",
                            "json_attributes_topic": self.state_topic,
                            "json_attributes_template": f"{{{{ {path} | tojson }}}}",
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
                        **self._command_attribute(f"apri{extra}"),
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
                        **self._command_attribute(f"apri_lungo{extra}"),
                    },
                ),
            ]
        result = []
        for component, object_id, config in entities:
            config = {**common, **config, "unique_id": f"{NODE_ID}_{object_id}"}
            result.append((component, object_id, config))
        return result

    def _command_attribute(self, command: str) -> dict:
        """Il comando del pulsante come attributo in Home Assistant: dice chi ha premuto proprio quel pulsante."""
        attributes = json.dumps({BUTTON_COMMAND: command})
        return {
            "json_attributes_topic": self.state_topic,
            "json_attributes_template": f"{{{{ {attributes} | tojson }}}}",
        }

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

    def _on_discovery_config(self, topic: str, payload: bytes, retained: bool = False) -> None:
        """Configurazione conservata dal broker, ricevuta alla connessione. Se l'entità non c'è più (porta tolta
        dall'app, pulsanti spenti, integrazione disattivata) si svuota, così sparisce anche da Home Assistant."""
        parts = topic.split("/")
        if not retained or not payload or len(parts) != 5:
            return
        current = {(c, o) for c, o, _ in self._entities()} if self.enabled else set()
        if (parts[1], parts[3]) in current:
            return
        _LOGGER.info("Entità tolta da Home Assistant: %s", parts[3])
        task = asyncio.get_running_loop().create_task(self.monitor.publish(topic, b"", retain=True))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start(self) -> None:
        """Avvia la ripubblicazione dello stato a mezzanotte; si ferma con stop()."""
        if self.enabled and self._midnight is None:
            self._midnight = asyncio.get_running_loop().create_task(self._midnight_loop())

    async def stop(self) -> None:
        """Da chiamare prima di chiudere la connessione al broker (spegnimento o nuova configurazione).

        Con una chiusura ordinata il broker scarta il messaggio di addio: "offline" si pubblica qui, altrimenti in
        Home Assistant le entità resterebbero disponibili con l'ultimo stato.
        """
        if self._midnight is not None:
            self._midnight.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._midnight
            self._midnight = None
        if not self.enabled:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self.monitor.publish(self.availability_topic, "offline", retain=True), OFFLINE_TIMEOUT
            )

    async def _midnight_loop(self) -> None:
        """Accessi Oggi e Accessi Negati Oggi tornano a zero a mezzanotte, anche senza eventi né sincronizzazioni."""
        while True:
            await asyncio.sleep(seconds_to_midnight())
            try:
                await self.publish_state()
            except Exception:
                _LOGGER.exception("Stato di mezzanotte non pubblicato in Home Assistant")

    async def publish_input_names(self) -> None:
        """Ripubblica solo le entità degli ingressi, con il nome scelto nell'app: le altre restano com'erano."""
        if not self.enabled:
            return
        ids = {object_id for object_id, _, _ in IO_INPUTS.values()}
        for component, object_id, config in self._entities():
            if object_id in ids:
                await self.monitor.publish(self._config_topic(component, object_id), json.dumps(config), retain=True)

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
            "serrature": {
                d["door_token"]: lock_state(self.monitor.door_modes.get(d["door_token"])) for d in self.doors
            },
            "contatti": {
                d["door_token"]: on_off(DOOR_PHYSICAL.get(self.monitor.door_physical.get(d["door_token"], "")))
                for d in self.doors
            },
            "accessi_oggi": counts.get("concesso", 0) + counts.get("apertura_remota", 0) + counts.get("uscita", 0),
            # Una tessera sconosciuta dà anche il diniego del controller: si conta quello
            "negati_oggi": counts.get("negato", 0),
            "da_registrare": len(self.db.list_unknown_cards()),
            "persone": len(self.db.all_persons()),
            "controller": {
                "online": on_off(self.monitor.controller_online),
                "coperchio": on_off(self.monitor.casing_open),
                "avvio": self.monitor.system_ready or "None",
            },
            "ingressi": {
                port: on_off(self.monitor.input_active(port, port in self._pushbutton_ports())) for port in IO_INPUTS
            },
            "condizioni": {d["door_token"]: self._conditions(d["door_token"]) for d in self.doors},
        }

    def _conditions(self, token: str) -> dict:
        """Allarme, preallarme e manomissione di una porta, con il tipo e l'ora dell'ultimo cambio."""
        seen = self.monitor.door_conditions.get(token, {})
        result = {}
        for key in DOOR_CONDITIONS:
            item = seen.get(key)
            result[key] = (
                {
                    "stato": on_off(item["active"]),
                    "tipo": DOOR_ALARM_TYPES.get(item["state"], item["state"]),
                    "ultimo_cambio": item["ts"],
                }
                if item
                else {"stato": "None", "tipo": "", "ultimo_cambio": ""}
            )
        return result

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
        # Un negato senza tessera né persona (pulsante o relè che il controller non fa aprire) non è un accesso
        if row.get("kind") in ACCESS_KINDS and not _anonymous_denied(row):
            self.last_access = access_payload(row)
            await self.monitor.publish(self.last_topic, json.dumps(self.last_access), retain=True)
