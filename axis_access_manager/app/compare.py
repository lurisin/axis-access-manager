"""Letture del controller e confronto con lo stato voluto dall'app, in sola lettura.

read_strict legge gli elenchi del controller e per ognuno dice se è letto o no: un elenco non letto non è mai un
elenco vuoto, e per quel tipo il confronto non toglie e non ricrea niente. compare confronta lo stato voluto (vedi
desired.build) con la lettura e dà una riga per oggetto, con l'azione che farebbe l'allineatore e le differenze campo
per campo. Qui nessuna funzione scrive sul controller: si chiamano solo i metodi list_* e le letture del dispositivo.

Il letto si proietta sulle sole chiavi che l'app scrive (FIELDS), perché il controller completa da solo chiavi di
IdPointConfiguration e DoorConfiguration, e si normalizza: date in UTC con la Z, numero di tessera senza maiuscole,
durate in secondi, fasce confrontate per significato, elenchi senza un ordine che conti messi in ordine. Le regole di
normalizzazione vengono dalle prove al banco (T3, T9, T10, T11): finché non ci sono, il confronto può mostrare
differenze false, ma non scrive niente.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from datetime import UTC, datetime

from .hardware import ALWAYS, CONTROLLER_INPUTS, READER_BUSES, RELAYS, is_factory
from .programming import DOOR_TIMES, _own_rex_profile, format_duration, is_rex_profile, parse_duration
from .rules import DAYS, describe_rules, parse_schedule_ical
from .service import card_of_credential, plural
from .vapix import ControllerUnreachable, VapixError

_LOGGER = logging.getLogger(__name__)

# Etichette dei campi negli elenchi delle differenze (backup, ripristino e confronto)
FIELD_LABELS = {
    "name": "Nome",
    "Name": "Nome sul Controller",
    "description": "Descrizione",
    "Description": "Descrizione",
    "company": "Ditta o Reparto",
    "notes": "Note",
    "category_id": "Tipologia",
    "person_id": "Persona",
    "enabled": "Stato",
    "access_profile": "Accesso",
    "valid_from": "Valida dal",
    "valid_to": "Valida fino al",
    "color": "Colore",
    "valid_days": "Durata",
    "rules": "Regole",
    "holidays": "Non Valida nei Giorni Festivi",
    "yearly": "Ogni Anno",
    "areas": "Aree",
    "perms": "Permessi",
    "value": "Valore",
    "host": "Indirizzo",
    "username": "Utente",
    "verify_ssl": "Verifica Certificato HTTPS",
    "area_id": "Struttura e Area",
    "snapshot_camera": "Telecamera",
    "snapshot_on_granted": "Foto Anche sugli Accessi Concessi",
    "ha_buttons": "Pulsanti in Home Assistant",
    "intercom_name": "Nome del Videocitofono",
    "intercom_open_door": '"Apri" dal Videocitofono Apre Questa Porta',
    "intercom_relay": "Relè del Tasto Apri",
    "intercom_mqtt_prefix": "Prefisso MQTT del Videocitofono",
    "mqtt_topic_prefix": "Prefisso MQTT",
    "model": "Modello",
    "serial": "Numero di Serie",
    "firmware": "Firmware",
    "time_zone": "Fuso Orario",
    "ntp": "Server NTP",
    "mqtt": "MQTT",
    "mqtt_events": "Eventi MQTT",
    "Location": "Posizione",
    "Timeout": "Attesa PIN",
    "configuration": "Configurazione",
    "door": "Porta",
    "readers": "Lettori",
    "direction": "Direzione",
    "Enabled": "Stato",
    "Schedule": "Fascia del Profilo",
    "policies": "Regole",
    "role": "Ruolo",
    "door_token": "Porta",
    "contact": "Modalità Ingresso",
    "supervised": "Linea Supervisionata",
    "schedule": "Fascia",
    "assigned": "Assegnato a",
    "managed": "Gestito dal Controller",
    **{field: label for field, (_, label, *_) in DOOR_TIMES.items()},
}
UUID_FIELDS = ("DeviceUUID", "DoorDeviceUUID")
FIELD_NAMES = {
    **FIELD_LABELS,
    "Name": "Nome",
    "ScheduleDefinition": "Orari",
    "AccessPolicy": "Regole",
    "AuthenticationProfile": "Modo di Identificazione",
    "IdPointDevice": "Lettori",
    "Entity": "Porta",
    "Attribute": "Attributi",
    "IoMode": "Modo",
    "IoUser": "Assegnato a",
    "HeartbeatInterval": "Controllo Collegamento",
    "Configuration": "Configurazione",
    "staticServers": "Server",
    "servers": "Server",
    "enabled": "Attivo",
    "source": "Origine",
    "server": "Broker",
    "username": "Utente",
}
# Campi che compaiono solo nel confronto (tessere, utenti, gestiti)
COMPARE_NAMES = {
    **FIELD_NAMES,
    "UserToken": "Utente",
    "IdData": "Numero e PIN",
    "ValidFrom": "Valida dal",
    "ValidTo": "Valida fino al",
    "CredentialAccessProfile": "Accesso",
    "ExceptionScheduleDefinition": "Festività",
    "EntityType": "Tipo di Porta",
    "AccessPoint": "Punti Gestiti",
    "value": "Valore",
}

# Lettura


# Elenchi del controller che l'app legge: chiave → (etichetta negli errori di lettura, metodo del client, se il
# metodo accetta strict). Senza strict il metodo lo è sempre: una risposta senza la chiave dell'elenco è un errore
LISTS = {
    "schedules": ("fasce orarie", "list_schedules", False),
    "doors": ("porte", "list_doors", False),
    "door_configurations": ("configurazione delle porte", "list_door_configurations", True),
    "idpoints": ("lettori", "list_idpoints", False),
    "idpoint_configurations": ("configurazione dei lettori", "list_idpoint_configurations", True),
    "io_assignments": ("ingressi e uscite", "list_io", True),
    "access_points": ("punti di accesso", "list_access_points", True),
    "access_profiles": ("profili di accesso", "list_access_profiles", False),
    "credentials": ("tessere", "list_credentials", False),
    "users": ("utenti", "list_users", True),
    "authentication_profiles": ("modi di identificazione", "list_authentication_profiles", True),
    "access_controllers": ("controllo accessi", "list_access_controllers", False),
}
# Impostazioni del dispositivo: fuso, NTP, broker MQTT ed eventi MQTT, lette con le API /axis-cgi
DEVICE = "device"
DEVICE_READS = (
    ("time_zone", "date_time_info"),
    ("ntp", "ntp_info"),
    ("mqtt", "mqtt_client_config"),
    ("mqtt_events", "mqtt_event_config"),
)
# Elenchi della parte di un controller del backup letti uno per uno, con l'etichetta dei loro errori di lettura
READ_LABELS = {
    key: LISTS[key][0]
    for key in (
        "schedules",
        "doors",
        "door_configurations",
        "idpoints",
        "idpoint_configurations",
        "io_assignments",
        "access_points",
        "access_profiles",
        "credentials",
    )
}
# Ordine delle letture della fotografia del backup
SNAPSHOT_READS = (
    "doors",
    "door_configurations",
    "idpoints",
    "idpoint_configurations",
    "access_points",
    "access_profiles",
    "schedules",
    "io_assignments",
    "credentials",
)


async def _optional(client, method: str):
    """Letture che non tutti i firmware hanno: assenti, non sono un errore del backup."""
    reader = getattr(client, method, None)
    if reader is None:
        return None
    try:
        return await reader()
    except VapixError as err:
        _LOGGER.debug("%s non letto: %s", method, err)
        return None


def _device(info: dict, time_info: dict | None, ntp: dict | None, mqtt: dict | None, events: dict | None) -> dict:
    client = (ntp or {}).get("client") if isinstance((ntp or {}).get("client"), dict) else (ntp or {})
    mqtt_config = dict((mqtt or {}).get("config") or {})
    # La password del broker non si salva (e il controller di solito non la restituisce)
    mqtt_config.pop("password", None)
    for key in ("server", "ssl"):
        if isinstance(mqtt_config.get(key), dict):
            mqtt_config[key] = {k: v for k, v in mqtt_config[key].items() if k != "password"}
    return {
        "model": str(info.get("ProdNbr") or info.get("ProdShortName") or ""),
        "serial": str(info.get("SerialNumber") or ""),
        "firmware": str(info.get("Version") or ""),
        "time_zone": str((time_info or {}).get("timeZone") or (time_info or {}).get("posixTimeZone") or ""),
        "ntp": {
            "enabled": client.get("enabled"),
            "source": client.get("serversSource") or client.get("timeServersSource") or "",
            "servers": client.get("staticServers") or client.get("timeServers") or [],
        }
        if client
        else {},
        "mqtt": mqtt_config,
        "mqtt_events": (events or {}).get("eventPublicationConfig") or events or {},
    }


async def _list(client, key: str, strict: bool) -> list[dict]:
    _, method, flag = LISTS[key]
    call = getattr(client, method)
    result = await (call(strict=True) if strict and flag else call())
    # list_io restituisce gli ingressi e le loro assegnazioni: qui servono le assegnazioni
    return result[1] if key == "io_assignments" else result


async def snapshot_lists(client) -> tuple[dict[str, list[dict]], list[str]]:
    """Gli elenchi della fotografia del backup (backup.controller_snapshot), letti come da sempre: un elenco che non si
    legge resta vuoto e il suo errore va negli errori della parte, con l'etichetta di READ_LABELS."""
    lists: dict[str, list[dict]] = {}
    errors: list[str] = []
    for key in SNAPSHOT_READS:
        try:
            lists[key] = await _list(client, key, strict=False)
        except VapixError as err:
            errors.append(f"{LISTS[key][0]}: {err}")
            lists[key] = []
    return lists, errors


def lists_for(kinds=None) -> list[str]:
    """Elenchi da leggere per i tipi del confronto (KINDS) o per gli elenchi indicati (LISTS e "device").
    None: tutti."""
    if kinds is None:
        return [*LISTS, DEVICE]
    keys: list[str] = []
    for kind in kinds:
        for key in KIND_LISTS.get(kind, (kind,)):
            if key not in LISTS and key != DEVICE:
                raise ValueError(f"tipo o elenco sconosciuto: {kind}")
            if key not in keys:
                keys.append(key)
    return keys


async def read_strict(client, kinds=None) -> dict:
    """Legge dal controller gli elenchi che servono ai tipi kinds (vedi lists_for), solo con letture.

    Restituisce {"lists": {elenco: oggetti}, "unread": {elenco: motivo}, "serial": numero di serie, "offline":
    motivo}: ogni elenco chiesto è in lists, se si è letto davvero, oppure in unread. Non letto è una risposta senza la
    chiave dell'elenco (la lettura strict del client), un rifiuto o il controller che non risponde: dopo il primo «non
    risponde» gli elenchi rimasti non si chiedono neanche, e restano non letti con lo stesso motivo, che è anche in
    offline (vuoto se il controller ha risposto fino alla fine). Non solleva eccezioni del controller. "device" sono
    le impostazioni del dispositivo: dentro ci sono solo quelle lette."""
    keys = lists_for(kinds)
    reading: dict = {"lists": {}, "unread": {}, "serial": "", "offline": ""}
    try:
        info = await client.device_info()
    except ControllerUnreachable as err:
        reading["unread"] = dict.fromkeys(keys, str(err))
        reading["offline"] = str(err)
        return reading
    except VapixError as err:
        info, info_error = None, str(err)
    else:
        reading["serial"] = str(info.get("SerialNumber") or "")
    offline = ""
    for key in keys:
        if key == DEVICE:
            continue
        if offline:
            reading["unread"][key] = offline
            continue
        try:
            items = await _list(client, key, strict=True)
        except ControllerUnreachable as err:
            offline = str(err)
            reading["unread"][key] = offline
        except VapixError as err:
            reading["unread"][key] = f"{LISTS[key][0]}: {err}"
        else:
            reading["lists"][key] = [i for i in items if isinstance(i, dict)]
    if DEVICE in keys:
        if offline or info is None:
            reading["unread"][DEVICE] = offline or info_error
        else:
            values, read = {}, set()
            for field, method in DEVICE_READS:
                if offline:
                    break
                try:
                    values[method] = await getattr(client, method)()
                    read.add(field)
                except ControllerUnreachable as err:
                    offline = str(err)
                except VapixError as err:
                    _LOGGER.debug("%s non letto: %s", method, err)
            device = _device(info, *(values.get(method) for _, method in DEVICE_READS))
            reading["lists"][DEVICE] = {k: v for k, v in device.items() if k not in dict(DEVICE_READS) or k in read}
    reading["offline"] = offline
    return reading


def merge_readings(*readings: dict) -> dict:
    """Più letture (una per tipo, in blocchi separati) in una: un elenco letto in una vale più di uno non letto in
    un'altra, e tra due letture vale l'ultima."""
    merged: dict = {"lists": {}, "unread": {}, "serial": ""}
    for reading in readings:
        merged["lists"].update(reading.get("lists") or {})
        merged["serial"] = merged["serial"] or reading.get("serial") or ""
        for key, reason in (reading.get("unread") or {}).items():
            merged["unread"][key] = reason
    for key in merged["lists"]:
        merged["unread"].pop(key, None)
    return merged


# Righe e differenze (anche del ripristino completo)


def _row(key: str, section: str, action: str, label: str, text: str, default: bool = True, **extra) -> dict:
    return {
        "key": key,
        "section": section,
        "action": action,
        "label": label,
        "text": text,
        "changes": [],
        "warnings": [],
        "blocked": "",
        "default": default,
        **extra,
    }


def _named(noun: str, name: str) -> str:
    """ "Porta Scala", ma "Porta B" e non "Porta Porta B" se il nome comincia già così."""
    return name if f"{name} ".lower().startswith(f"{noun} ".lower()) else f"{noun} {name}"


def _norm(value):
    """Forma da confrontare: senza campi vuoti né dispositivo, con le configurazioni Name/Value come dizionario."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in UUID_FIELDS:
                continue
            if key == "Configuration" and isinstance(item, list):
                item = {str(c.get("Name")): str(c.get("Value")) for c in item if isinstance(c, dict)}
            item = _norm(item)
            if item in ("", None, [], {}):
                continue
            out[key] = item
        return out
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _show(value) -> str:
    if value in ("", None, [], {}):
        return "—"
    if isinstance(value, bool):
        return "sì" if value else "no"
    if isinstance(value, str):
        seconds = parse_duration(value) if value.startswith("P") else None
        text = f"{seconds:g} s" if seconds is not None else value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return text if len(text) <= 80 else text[:79] + "…"


def _differences(current: dict | None, saved: dict, labels: dict | None = None) -> list[dict]:
    """Campi diversi, com'è adesso → come tornerà; le voci di configurazione una per riga."""
    labels = FIELD_NAMES if labels is None else labels
    now, then = _norm(current or {}), _norm(saved)
    changes = []
    for key in dict.fromkeys([*then, *now]):
        if key == "token" or now.get(key) == then.get(key):
            continue
        if key == "Configuration":
            old, new = now.get(key) or {}, then.get(key) or {}
            for name in dict.fromkeys([*new, *old]):
                if old.get(name) != new.get(name):
                    changes.append({"label": name, "old": old.get(name) or "—", "new": new.get(name) or "—"})
            continue
        changes.append({"label": labels.get(key, key), "old": _show(now.get(key)), "new": _show(then.get(key))})
    return changes


def _own_points(part: dict) -> list[str] | None:
    """Punti di accesso che il controller gestisce, dal suo AccessController (in una rete di controller ci sono
    anche quelli degli altri: il suo ha il numero di serie nel token). None se non è nel backup."""
    controllers = part.get("access_controllers")
    if controllers is None:
        return None
    serial = str((part.get("device") or {}).get("serial") or "").lower()
    if len(controllers) > 1:
        controllers = [c for c in controllers if serial and serial in str(c.get("token") or "").lower()]
    return [t for c in controllers for t in c.get("AccessPoint") or []]


def _plan_managed(saved: dict, current: dict, saved_points: dict) -> dict | None:
    """Punti di accesso da far gestire al controller. Il reset svuota l'elenco: i punti ci sono, il controller legge
    le tessere ma non decide niente. Dai backup che non lo hanno (fino alla 0.21.0) si fanno gestire tutti."""
    wanted = _own_points(saved)
    wanted = [t for t in dict.fromkeys(list(saved_points) if wanted is None else wanted) if t in saved_points]
    linked = [t for c in current.get("access_controllers") or [] for t in c.get("AccessPoint") or []]
    missing = [t for t in wanted if t not in linked]
    if not missing:
        return None
    current_names = {p.get("token"): p.get("Name") or p.get("token") for p in current.get("access_points") or []}

    def shown(tokens: list[str], names: dict) -> str:
        return ", ".join(str(names.get(t) or t) for t in tokens) or "nessuno"

    item = _row(
        "prog-gestiti",
        "controller",
        "ricrea" if not linked else "riporta",
        "Punti di Accesso Gestiti",
        f"Fa gestire al controller {plural(len(missing), 'punto di accesso', 'punti di accesso')}: "
        "senza, le tessere vengono lette ma non aprono",
        _kind="gestiti",
        _token="",
        _points=missing,
    )
    names = {t: p.get("Name") or t for t, p in saved_points.items()}
    item["changes"] = [{"label": "Gestiti", "old": shown(linked, current_names), "new": shown(wanted, names)}]
    return item


def _plan_pair(prefix, token, label, saved_obj, current_obj, saved_config, current_config, add) -> dict | None:
    """Porta o lettore insieme alla sua configurazione: si scrivono in fila, nella stessa riga."""
    config_changes = []
    if saved_config is not None:
        config_changes = (
            _differences(current_config, saved_config)
            if current_config is not None
            else [
                {"label": "Configurazione", "old": "—", "new": f"{len(saved_config.get('Configuration') or [])} voci"}
            ]
        )
    item = add(prefix, token, label, saved_obj, current_obj, force=bool(config_changes), _config=saved_config)
    if item is not None and current_obj is not None:
        item["changes"] = _differences(current_obj, saved_obj) + config_changes
    return item


# Morsetti che non sono ingressi: il bus dei lettori e il relè della serratura
IO_NAMES = {**READER_BUSES, **RELAYS}


def _io_name(name: str) -> str:
    return IO_NAMES.get(name) or CONTROLLER_INPUTS.get(name, (name,))[0]


def _io_users(assignment: dict | None, names: dict) -> str:
    users = (assignment or {}).get("IoUser") or []
    return (
        ", ".join(
            f"{names.get(str(u.get('token')), u.get('token'))} ({u.get('Usage') or u.get('Type')})" for u in users
        )
        or "—"
    )


# Confronto con lo stato voluto

# Tipi di oggetto, nell'ordine in cui li scrive l'allineatore (passo 3 e 4)
KINDS = (
    "dispositivo",
    "fascia",
    "porta",
    "config_porta",
    "lettore",
    "config_lettore",
    "morsetto",
    "punto",
    "gestiti",
    "pulsanti",
    "tipologia",
    "utente",
    "tessera",
)
# Elenchi letti che servono a ogni tipo
KIND_LISTS = {
    "dispositivo": (DEVICE,),
    "fascia": ("schedules",),
    "porta": ("doors",),
    "config_porta": ("door_configurations",),
    "lettore": ("idpoints",),
    "config_lettore": ("idpoint_configurations",),
    "morsetto": ("io_assignments",),
    "punto": ("access_points",),
    "gestiti": ("access_controllers",),
    "pulsanti": ("access_profiles",),
    "tipologia": ("access_profiles",),
    "utente": ("users",),
    "tessera": ("credentials",),
    "modo": ("authentication_profiles",),
}
PROFILE_FIELDS = (
    "Name",
    "Description",
    "Enabled",
    "ValidFrom",
    "ValidTo",
    "AuthenticationProfile",
    "Attribute",
    "Schedule",
    "AccessPolicy",
)
# Chiavi che l'app scrive, per tipo: solo queste si confrontano e si firmano. Gli utenti senza la descrizione, che
# l'app ha scritto in modi diversi nel tempo; le tessere senza Status, che il controller può lasciare com'era con
# Enable/DisableCredential (T4): conta Enabled
FIELDS = {
    "dispositivo": ("value",),
    "fascia": ("Name", "Description", "ScheduleDefinition", "ExceptionScheduleDefinition", "Attribute"),
    "porta": ("Name", "Description", *DOOR_TIMES),
    "config_porta": ("Configuration",),
    "lettore": ("Name", "Description", "Timeout"),
    "config_lettore": ("Configuration",),
    "morsetto": ("IoMode", "IoUser"),
    "punto": (
        "Name",
        "Description",
        "Entity",
        "EntityType",
        "Enabled",
        "IdPointDevice",
        "AuthenticationProfile",
        "Attribute",
    ),
    "gestiti": ("AccessPoint",),
    "pulsanti": PROFILE_FIELDS,
    "tipologia": PROFILE_FIELDS,
    "utente": ("Name", "Attribute"),
    "tessera": (
        "UserToken",
        "Description",
        "Enabled",
        "IdData",
        "Attribute",
        "AuthenticationProfile",
        "ValidFrom",
        "ValidTo",
        "CredentialAccessProfile",
    ),
}
DURATION_FIELDS = frozenset({*DOOR_TIMES, "Timeout"})
DATE_FIELDS = frozenset({"ValidFrom", "ValidTo"})
# Elenchi il cui ordine non conta: si confrontano in ordine
SORTED_LISTS = {
    "AccessPolicy": lambda p: str(p.get("AccessPoint")) if isinstance(p, dict) else str(p),
    "Attribute": lambda a: str(a.get("Name")) if isinstance(a, dict) else str(a),
    "IdData": lambda i: str(i.get("Name")) if isinstance(i, dict) else str(i),
    "IoUser": lambda u: (str(u.get("token")), str(u.get("Usage"))) if isinstance(u, dict) else (str(u), ""),
    "IdPointDevice": lambda d: str(d.get("IdPoint")) if isinstance(d, dict) else str(d),
    "CredentialAccessProfile": lambda c: str(c.get("AccessProfile")) if isinstance(c, dict) else str(c),
    "Schedule": str,
    "AuthenticationProfile": str,
    "AuthorizationProfile": str,
    "AccessPoint": str,
}
# Voci della configurazione della porta che fanno sblocchi programmati: devono essere vuote
DOOR_SCHEDULE_PREFIX = "DoorSchedule"
ACTIONS = {
    "prende": "Prende in carico com'è sul controller",
    "riporta": "Riporta com'è nell'app",
    "ricrea": "Ricrea: sul controller non c'è",
    "togli": "Togli: nell'app non c'è",
    "uguale": "Uguale nell'app e sul controller",
    "non_letto": "Non letto: per questo tipo niente si toglie, si ricrea o si conferma",
}
KIND_NOUNS = {
    "dispositivo": "",
    "fascia": "Fascia Oraria",
    "porta": "Porta",
    "config_porta": "Configurazione della Porta",
    "lettore": "Lettore",
    "config_lettore": "Configurazione del Lettore",
    "morsetto": "Morsetto",
    "punto": "Punto di Accesso",
    "gestiti": "Punti di Accesso Gestiti",
    "pulsanti": "Profilo dei Pulsanti",
    "tipologia": "Profilo di Accesso",
    "utente": "Utente",
    "tessera": "Tessera",
    "sblocco": "Sblocco Programmato",
}
KIND_TITLES = {
    "dispositivo": "Impostazioni del Controller",
    "fascia": "Fasce Orarie",
    "porta": "Porte",
    "config_porta": "Configurazione delle Porte",
    "lettore": "Lettori",
    "config_lettore": "Configurazione dei Lettori",
    "morsetto": "Ingressi e Uscite",
    "punto": "Punti di Accesso",
    "gestiti": "Punti di Accesso Gestiti",
    "pulsanti": "Profilo dei Pulsanti",
    "tipologia": "Profili di Accesso",
    "utente": "Utenti",
    "tessera": "Tessere",
}
DEVICE_ITEMS = {
    "fuso": ("time_zone", "Fuso Orario"),
    "ntp": ("ntp", "Server NTP"),
    "mqtt": ("mqtt", "Collegamento MQTT"),
    "mqtt_eventi": ("mqtt_events", "Eventi MQTT"),
}
# Sorprese: oggetti che l'app non si aspetta e che vanno guardati prima della presa in carico
SURPRISE_OUTSIDE = "creata fuori dall'app (per esempio da uno script)"
SURPRISE_USER_OUTSIDE = "creato fuori dall'app (per esempio da uno script)"
SURPRISE_EXTERNAL = "profilo esterno: non è di una tipologia dell'app"
SURPRISE_REX = "profilo dei pulsanti fatto a mano"
SURPRISE_ALWAYS = "la fascia di fabbrica Sempre non vale più sempre"
SURPRISE_SCHEDULE = "sblocco programmato della porta (DoorSchedule)"
SURPRISE_DUPLICATE = "doppione per numero di tessera"


def _utc(value: str) -> str:
    """Data e ora in UTC con la Z, come le scrive l'app; il testo com'è se non si legge."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value) -> str:
    """Testo di un calendario senza differenze di righe e spazi."""
    lines = str(value or "").replace("\r\n ", "").replace("\r\n", "\n").split("\n")
    return "\n".join(line.strip() for line in lines if line.strip())


def schedule_meaning(text: str | None) -> str:
    """Orari di un calendario Axis per significato: «lun-ven 08:00–18:00». Due calendari scritti in modo diverso con
    gli stessi orari danno lo stesso testo. Un calendario che l'editor non sa leggere resta com'è."""
    if not text:
        return ""
    rules = parse_schedule_ical(text)
    if rules is None:
        return _text(text)
    per_day: dict[str, list[list[str]]] = {}
    for rule in rules:
        for day in rule["days"]:
            per_day.setdefault(day, []).append([rule["start"], rule["end"]])
    by_interval: dict[tuple[str, str], list[str]] = {}
    for day in DAYS:
        merged: list[list[str]] = []
        for start, end in sorted(per_day.get(day, [])):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        for start, end in merged:
            by_interval.setdefault((start, end), []).append(day)
    canonical = [
        {"days": days, "start": start, "end": end}
        for (start, end), days in sorted(by_interval.items(), key=lambda i: (DAYS.index(i[1][0]), i[0]))
    ]
    return describe_rules(canonical)


def exceptions_meaning(text: str | None) -> str:
    """Giornate escluse da una fascia (festività) per significato: «25/12/2026 ogni anno; 06/04/2026»."""
    if not text:
        return ""
    days = set()
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", str(text).replace("\r\n ", ""), re.S):
        props: dict[str, str] = {}
        for line in block.strip().splitlines():
            key, _, value = line.strip().partition(":")
            props[key.split(";")[0].upper()] = value.strip()
        start = props.get("DTSTART", "")
        if not re.fullmatch(r"\d{8}", start):
            return _text(text)
        rrule = props.get("RRULE", "")
        yearly = "FREQ=YEARLY" in rrule.upper()
        if rrule and not yearly:
            return _text(text)
        days.add(f"{start[6:8]}/{start[4:6]}/{start[:4]}" + (" ogni anno" if yearly else ""))
    return "; ".join(sorted(days)) if days else _text(text)


def _canon(key: str, value):
    """Valore nella forma del confronto: date in UTC, durate in secondi, fasce per significato, numero di tessera
    senza maiuscole, elenchi senza ordine messi in ordine, nessuna password."""
    if key in DATE_FIELDS and isinstance(value, str):
        return _utc(value)
    if key in DURATION_FIELDS and isinstance(value, str):
        seconds = parse_duration(value)
        return value if seconds is None else format_duration(seconds)
    if key == "ScheduleDefinition":
        return schedule_meaning(value)
    if key == "ExceptionScheduleDefinition":
        return exceptions_meaning(value)
    if key == "Configuration" and isinstance(value, list):
        items = {str(c.get("Name")): str(c.get("Value")) for c in value if isinstance(c, dict)}
        # Sensore Porta spento: la voce "None" e la voce che manca dicono la stessa cosa (T3, T14)
        return {k: v for k, v in items.items() if not (k.startswith("DoorMonitor.") and v == "None")}
    if isinstance(value, dict):
        return {k: _canon(k, v) for k, v in value.items() if k != "password"}
    if isinstance(value, list):
        items = []
        for item in value:
            if key == "IdData" and isinstance(item, dict) and item.get("Name") == "Card":
                item = {**item, "Value": str(item.get("Value") or "").lower()}
            if key == "IoUser" and isinstance(item, dict) and item.get("MultiIo") is False:
                item = {k: v for k, v in item.items() if k != "MultiIo"}
            items.append(_canon(key, item))
        if key in SORTED_LISTS:
            items.sort(key=SORTED_LISTS[key])
            if key == "AccessPoint":
                items = list(dict.fromkeys(items))
        return items
    return value


def project(kind: str, read: dict, wanted: dict | None = None) -> dict:
    """L'oggetto letto con le sole chiavi che l'app scrive per quel tipo; nelle configurazioni solo le voci che ha
    l'oggetto voluto (le altre le completa il controller da solo)."""
    out = {key: copy.deepcopy(read[key]) for key in FIELDS[kind] if key in read}
    if isinstance(out.get("Configuration"), list) and wanted is not None:
        names = {str(c.get("Name")) for c in wanted.get("Configuration") or [] if isinstance(c, dict)}
        out["Configuration"] = [c for c in out["Configuration"] if isinstance(c, dict) and str(c.get("Name")) in names]
    return out


def normalize(kind: str, payload: dict) -> dict:
    """Forma confrontabile e firmabile di un oggetto (voluto, o letto e già proiettato): solo le chiavi di FIELDS,
    nessun campo vuoto né dispositivo, Enabled che manca vale acceso (come lo legge il controller)."""
    data = {key: _canon(key, copy.deepcopy(payload[key])) for key in FIELDS[kind] if key in payload}
    if "Enabled" in FIELDS[kind] and data.get("Enabled") is None:
        data["Enabled"] = True
    return _norm(data)


def _by_token(items, field: str = "token") -> dict[str, dict]:
    return {str(i.get(field)): i for i in items or [] if isinstance(i, dict) and i.get(field)}


def _own_controller(controllers: list[dict], serial: str) -> dict | None:
    """Il controllo accessi di questo controller: l'unico, o quello con il numero di serie nel token."""
    if len(controllers) == 1:
        return controllers[0]
    mine = [c for c in controllers if serial and serial.lower() in str(c.get("token") or "").lower()]
    return mine[0] if len(mine) == 1 else None


def _schedule_altered(schedule: dict) -> bool:
    """Sempre deve valere tutti i giorni 00:00–24:00, anche nei festivi. Il calendario vuoto è quello di serie del
    controller finto (T12 dirà quello dell'A1210)."""
    if schedule.get("ExceptionScheduleDefinition"):
        return True
    definition = schedule.get("ScheduleDefinition")
    return bool(definition) and schedule_meaning(definition) != "tutti i giorni 00:00–24:00"


def _door_schedule(config: dict) -> list[tuple[str, str]]:
    """Voci DoorSchedule* non vuote di una configurazione della porta, fuori o dentro Configuration."""
    found = [
        (k, _show(v))
        for k, v in config.items()
        if k.startswith(DOOR_SCHEDULE_PREFIX) and _norm(v) not in ("", None, [], {})
    ]
    for item in config.get("Configuration") or []:
        name = str(item.get("Name") or "") if isinstance(item, dict) else ""
        value = item.get("Value") if isinstance(item, dict) else None
        if name.startswith(DOOR_SCHEDULE_PREFIX) and value not in ("", None, "None"):
            found.append((name, str(value)))
    return found


def _read_objects(kind: str, lists: dict, objects: dict, serial: str) -> dict[str, dict] | None:
    """Oggetti letti di un tipo, per chiave; None se non si possono dire (controllo accessi non riconosciuto)."""
    if kind == "dispositivo":
        device = lists.get(DEVICE) or {}
        return {key: {"value": device[field]} for key, (field, _) in DEVICE_ITEMS.items() if field in device}
    if kind in ("tipologia", "pulsanti"):
        profiles = _by_token(lists.get("access_profiles"))
        rex = {
            t
            for t, p in profiles.items()
            if t in (objects.get("pulsanti") or {}) or (t not in (objects.get("tipologia") or {}) and is_rex_profile(p))
        }
        return {t: p for t, p in profiles.items() if (t in rex) == (kind == "pulsanti")}
    if kind == "morsetto":
        wanted = objects.get("morsetto") or {}
        return {
            k: a for k, a in _by_token(lists.get("io_assignments"), "IoName").items() if a.get("IoUser") or k in wanted
        }
    if kind == "gestiti":
        own = _own_controller(lists.get("access_controllers") or [], serial)
        return None if own is None else {"gestiti": {**own, "AccessPoint": list(own.get("AccessPoint") or [])}}
    key = {
        "fascia": "schedules",
        "porta": "doors",
        "config_porta": "door_configurations",
        "lettore": "idpoints",
        "config_lettore": "idpoint_configurations",
        "punto": "access_points",
        "utente": "users",
        "tessera": "credentials",
    }[kind]
    return _by_token(lists.get(key))


def _names(lists: dict, objects: dict) -> dict[str, str]:
    """Nomi da mostrare per token: dal voluto, altrimenti dal letto."""
    names: dict[str, str] = {}
    for key in ("doors", "idpoints", "access_points", "access_profiles", "schedules", "users"):
        for item in lists.get(key) or []:
            if isinstance(item, dict) and item.get("token") and item.get("Name"):
                names[str(item["token"])] = str(item["Name"])
    for kind in ("porta", "lettore", "punto", "tipologia", "pulsanti", "fascia", "utente"):
        for token, payload in (objects.get(kind) or {}).items():
            if payload.get("Name"):
                names[token] = str(payload["Name"])
    return names


def _label(kind: str, key: str, wanted: dict | None, current: dict | None, names: dict) -> str:
    if kind == "dispositivo":
        return DEVICE_ITEMS[key][1]
    if kind == "gestiti":
        return KIND_NOUNS[kind]
    if kind == "morsetto":
        return f"Morsetto {_io_name(key)}"
    if kind == "tessera":
        item = wanted or current or {}
        number = card_of_credential(item) or card_of_credential(current or {})
        holder = item.get("Description") or (current or {}).get("Description") or ""
        return f"Tessera {number or key}" + (f" ({holder})" if holder else "")
    name = names.get(key) or (wanted or {}).get("Name") or (current or {}).get("Name") or key
    if kind in ("fascia", "tipologia", "pulsanti") and key == ALWAYS and not (wanted or {}).get("Name"):
        name = "Sempre"
    return _named(KIND_NOUNS[kind], str(name))


def _times_over(door: dict) -> list[str]:
    """Tempi della porta letta fuori dai limiti della serratura (DOOR_TIMES: apertura 30 s, estesa 60 s)."""
    found = []
    for field, (_, label, low, high) in DOOR_TIMES.items():
        seconds = parse_duration(door.get(field))
        if seconds is not None and not low <= seconds <= high:
            found.append(f"{label}: {seconds:g} s, fuori dai limiti della serratura ({low}-{high} s)")
    return found


def _duplicates(credentials: dict[str, dict], cards: dict[str, dict]) -> dict[str, str]:
    """Tessere lette con lo stesso numero di un'altra letta, o di una tessera dell'app con un altro token."""
    by_number: dict[str, list[str]] = {}
    for token, credential in credentials.items():
        number = card_of_credential(credential).lower()
        if number:
            by_number.setdefault(number, []).append(token)
    app_numbers = {card_of_credential(c).lower(): t for t, c in cards.items() if card_of_credential(c)}
    found = {}
    for number, tokens in by_number.items():
        mine = app_numbers.get(number)
        if len(tokens) > 1 or (mine and mine not in tokens):
            total = len({*tokens, *([mine] if mine else [])})
            for token in tokens:
                found[token] = f"{SURPRISE_DUPLICATE} {number}: {total} tessere"
    return found


def compare(voluto: dict, letto: dict, notes: list[dict] | None = None) -> dict:
    """Confronta lo stato voluto (desired.build) con la lettura del controller (read_strict).

    Restituisce {"rows": [...], "unread": [...]}. rows: una riga per oggetto dei tipi del voluto, nell'ordine di KINDS,
    con action tra "prende" (lo prende in carico com'è), "riporta" (sul controller è diverso: lo riporta com'è
    nell'app), "ricrea" (manca sul controller), "togli" (c'è solo sul controller), "uguale"; changes: le differenze
    campo per campo, old com'è sul controller e new com'è nell'app; surprises: perché va guardato (vuoto se niente).
    Gli oggetti di fabbrica (hardware.is_factory) non sono mai tra le righe "togli". unread: una riga "non_letto" per
    ogni tipo con un elenco non letto, e per quel tipo nessuna riga di oggetti.
    notes: le note della presa in carico; un oggetto preso in carico e uguale dopo la presa è "prende", e le sue
    sorprese e i suoi avvisi passano nella riga."""
    lists = letto.get("lists") or {}
    unread_lists = letto.get("unread") or {}
    serial = str(letto.get("serial") or "")
    objects = voluto.get("objects") or {}
    pending = set(voluto.get("pending") or ())
    known_users = set((voluto.get("known") or {}).get("utente") or ())
    noted = {(n.get("kind"), n.get("key")): n for n in notes or () if n.get("key")}
    names = _names(lists, objects)
    rows: list[dict] = []
    unread: list[dict] = []
    # Solo i tipi per cui il voluto è stato calcolato: per gli altri ogni oggetto letto risulterebbe da togliere
    for kind in voluto.get("kinds", ()):
        missing = [key for key in KIND_LISTS[kind] if key not in lists]
        current = None if missing else _read_objects(kind, lists, objects, serial)
        if current is None:
            reasons = [unread_lists.get(key) or "elenco non chiesto" for key in missing] or [
                "non si riconosce il controllo accessi di questo controller"
            ]
            item = _row(f"{kind}:", kind, "non_letto", KIND_TITLES[kind], ACTIONS["non_letto"], False, kind=kind)
            item.update(token="", surprises=[], blocked="; ".join(dict.fromkeys(reasons)))
            unread.append(item)
            continue
        wanted = objects.get(kind) or {}
        extra = _surprises(kind, current, objects, known_users)
        for key in dict.fromkeys([*wanted, *current]):
            want, have = wanted.get(key), current.get(key)
            if want is None and kind in ("fascia", "morsetto") and is_factory(kind, key, serial):
                if kind == "fascia" and key == ALWAYS and have is not None and _schedule_altered(have):
                    item = _make(kind, key, "riporta", want, have, names)
                    item["text"] = "Riporta Sempre com'è di fabbrica: tutti i giorni 00:00–24:00, anche nei festivi"
                    item["surprises"].append(SURPRISE_ALWAYS)
                    rows.append(item)
                continue
            if kind in pending:
                action = "prende"
            elif want is None:
                action = "togli"
            elif have is None:
                action = "ricrea"
            else:
                action = "uguale"
            item = _make(kind, key, action, want, have, names)
            if action == "uguale":
                item["changes"] = _differences(
                    _without_pin(normalize(kind, project(kind, have, want))),
                    _without_pin(normalize(kind, want)),
                    COMPARE_NAMES,
                )
                if item["changes"]:
                    item.update(action="riporta", text=ACTIONS["riporta"])
                elif (kind, key) in noted:
                    item.update(action="prende", text=ACTIONS["prende"])
            note = noted.get((kind, key))
            if note:
                if note.get("surprise"):
                    item["surprises"].append(note["surprise"])
                item["warnings"] += note.get("warnings") or []
            item["surprises"] += extra.get(key, [])
            rows.append(item)
        if kind == "config_porta":
            for key, config in current.items():
                found = _door_schedule(config)
                if found:
                    item = _make("sblocco", key, "togli", None, config, names)
                    item["label"] = _named(KIND_NOUNS["sblocco"], names.get(key) or key)
                    item["text"] = "Togli: l'app non programma mai sblocchi della porta"
                    item["changes"] = [{"label": name, "old": value, "new": "—"} for name, value in found]
                    item["surprises"].append(SURPRISE_SCHEDULE)
                    rows.append(item)
    return {"rows": rows, "unread": unread}


def _without_pin(data: dict) -> dict:
    """Senza il valore del PIN e degli altri dati della tessera che non sono il numero: le differenze si mostrano nella
    pagina del controller e nel log. L'app non scrive PIN, quindi un PIN sul controller resta una differenza."""
    if not isinstance(data.get("IdData"), list):
        return data
    items = [
        {**item, "Value": "••••"} if isinstance(item, dict) and item.get("Name") != "Card" else item
        for item in data["IdData"]
    ]
    return {**data, "IdData": items}


def _make(kind: str, key: str, action: str, wanted: dict | None, current: dict | None, names: dict) -> dict:
    item = _row(f"{kind}:{key}", kind, action, _label(kind, key, wanted, current, names), ACTIONS[action], False)
    item.update(kind=kind, token=key, surprises=[])
    return item


def _surprises(kind: str, current: dict, objects: dict, known_users: set[str]) -> dict[str, list[str]]:
    """Sorprese degli oggetti letti di un tipo, per chiave."""
    wanted = objects.get(kind) or {}
    found: dict[str, list[str]] = {}

    def add(key: str, text: str) -> None:
        found.setdefault(key, []).append(text)

    if kind == "tessera":
        for token in current:
            if token not in wanted:
                add(token, SURPRISE_OUTSIDE)
        for token, text in _duplicates(current, wanted).items():
            add(token, text)
    elif kind == "utente":
        for token in current:
            if token not in wanted and token not in known_users:
                add(token, SURPRISE_USER_OUTSIDE)
    elif kind == "tipologia":
        for token in current:
            if token not in wanted:
                add(token, SURPRISE_EXTERNAL)
    elif kind == "pulsanti":
        for token, profile in current.items():
            if token not in wanted and _own_rex_profile([profile]) is None:
                add(token, SURPRISE_REX)
    elif kind == "porta":
        for token, door in current.items():
            for text in _times_over(door):
                add(token, text)
    return found


def counts(result: dict) -> dict:
    """Quante righe per azione e per tipo, quante sorprese e quali tipi non letti (per i contatori e il log)."""
    actions: dict[str, int] = {}
    kinds: dict[str, dict[str, int]] = {}
    for row in result.get("rows") or []:
        actions[row["action"]] = actions.get(row["action"], 0) + 1
        per_kind = kinds.setdefault(row["kind"], {})
        per_kind[row["action"]] = per_kind.get(row["action"], 0) + 1
    return {
        "actions": actions,
        "kinds": kinds,
        "surprises": sum(1 for row in result.get("rows") or [] if row["surprises"]),
        "unread": [row["kind"] for row in result.get("unread") or []],
    }
