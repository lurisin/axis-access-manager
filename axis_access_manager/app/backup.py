"""Backup dell'app: i Dati Comuni di tutto l'impianto e la configurazione di ogni controller.

Ogni backup è un piccolo file indice in /data/backup con l'elenco delle sue parti: una per i Dati Comuni
(persone, tessere, tipologie, fasce orarie, festività, strutture, utenti dell'app) e una per ogni controller
(porte, lettori, punti di accesso, profili, ingressi, impostazioni del dispositivo).
Ogni parte è un file JSON compresso che ha per nome l'impronta SHA-256 del suo contenuto: una parte che non
è cambiata dal backup precedente non si riscrive, si riusa il file. Così molti backup quasi uguali occupano
poco più di uno, e confrontare due backup vuol dire confrontare le loro parti.

Restano fuori le password degli account del controller (non si possono leggere), la chiave privata del
certificato e il firmware (se ne annota la versione); registro eventi e foto sono nel backup di Home Assistant.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from .db import Database, now_iso
from .programming import DOOR_TIMES, INPUT_ROLES, parse_duration
from .service import ServiceError, card_of_credential, plural
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

FORMAT = 1
BACKUP_DIR = "backup"
PARTS_DIR = "parti"
COMMON = "comuni"
KINDS = {"manuale": "Manuale", "automatico": "Automatico", "prima_ripristino": "Prima del Ripristino"}
ID_PATTERN = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NAME_MAX = 80
# Impostazioni dell'app che fanno parte dei Dati Comuni; le altre (versione, notifiche già viste) cambiano da sole
SETTINGS_KEPT = ("default_group", "default_category", "notifications")
SETTINGS_PREFIXES = ("dashboard_layout:",)
# Ultimo backup non riuscito o incompleto (JSON): diventa un guasto finché un backup non riesce
SETTING_ERROR = "backup_error"

# Sezioni di un confronto: (singolare, plurale, femminile)
SECTIONS = {
    "persone": ("persona", "persone", True),
    "tessere": ("tessera", "tessere", True),
    "tipologie": ("tipologia", "tipologie", True),
    "fasce": ("fascia oraria", "fasce orarie", True),
    "festivita": ("festività", "festività", True),
    "strutture": ("struttura", "strutture", True),
    "utenti": ("utente dell'app", "utenti dell'app", False),
    "impostazioni": ("impostazione dell'app", "impostazioni dell'app", True),
    "collegamento": ("collegamento", "collegamenti", False),
    "dispositivo": ("impostazioni del dispositivo", "impostazioni del dispositivo", True),
    "porte": ("porta", "porte", True),
    "lettori": ("lettore", "lettori", False),
    "punti": ("punto di accesso", "punti di accesso", False),
    "profili": ("profilo di accesso", "profili di accesso", False),
    "ingressi": ("ingresso", "ingressi", False),
}
SECTION_TITLES = {
    "persone": "Persone",
    "tessere": "Tessere",
    "tipologie": "Tipologie",
    "fasce": "Fasce Orarie",
    "festivita": "Festività",
    "strutture": "Strutture e Aree",
    "utenti": "Utenti dell'App",
    "impostazioni": "Impostazioni dell'App",
    "collegamento": "Collegamento",
    "dispositivo": "Dispositivo",
    "porte": "Porte",
    "lettori": "Lettori",
    "punti": "Punti di Accesso",
    "profili": "Profili di Accesso",
    "ingressi": "Ingressi",
}
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
    "holidays": "Chiusa nelle Festività",
    "yearly": "Ogni Anno",
    "areas": "Aree",
    "perms": "Permessi",
    "value": "Valore",
    "host": "Indirizzo",
    "username": "Utente",
    "verify_ssl": "Verifica Certificato",
    "area_id": "Posizione",
    "snapshot_camera": "Telecamera",
    "snapshot_on_granted": "Foto degli Accessi Concessi",
    "ha_buttons": "Pulsanti in Home Assistant",
    "intercom_name": "Citofono",
    "intercom_open_door": "Apertura dal Citofono",
    "intercom_relay": "Relè del Citofono",
    "intercom_mqtt_prefix": "Prefisso MQTT del Citofono",
    "mqtt_topic_prefix": "Prefisso MQTT",
    "model": "Modello",
    "serial": "Numero di Serie",
    "firmware": "Firmware",
    "time_zone": "Fuso Orario",
    "ntp": "Server NTP",
    "mqtt": "MQTT",
    "mqtt_events": "Eventi MQTT",
    "Location": "Posizione",
    "Timeout": "Attesa",
    "configuration": "Configurazione",
    "door": "Porta",
    "readers": "Lettori",
    "direction": "Direzione",
    "Enabled": "Stato",
    "Schedule": "Fascia del Profilo",
    "policies": "Regole",
    "role": "Ruolo",
    "door_token": "Porta",
    "contact": "Contatto",
    "supervised": "Linea Supervisionata",
    "schedule": "Fascia",
    "assigned": "Assegnato a",
    **{field: label for field, (_, label, *_) in DOOR_TIMES.items()},
}
COLOR_NAMES = {
    "blue": "Blu",
    "green": "Verde",
    "orange": "Arancione",
    "red": "Rosso",
    "purple": "Viola",
    "teal": "Turchese",
    "pink": "Rosa",
    "gray": "Grigio",
}


class BackupError(ServiceError):
    """Backup non leggibile, non trovato o operazione non consentita."""


def canonical(data) -> bytes:
    """JSON sempre scritto allo stesso modo: stesso contenuto, stessa impronta."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


class BackupStore:
    """File dei backup: indici in root, parti compresse in root/parti."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.parts = self.root / PARTS_DIR

    def write_part(self, data: dict) -> dict:
        raw = canonical(data)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.parts / f"{digest}.json.gz"
        if not path.exists():
            self.parts.mkdir(parents=True, exist_ok=True)
            partial = path.with_name(path.name + ".parziale")
            with open(partial, "wb") as handle, gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as gz:
                gz.write(raw)
            os.replace(partial, path)
        return {"hash": digest, "size": path.stat().st_size}

    def read_part(self, digest: str) -> dict:
        if not HASH_PATTERN.match(digest or ""):
            raise BackupError("parte del backup non valida")
        try:
            raw = gzip.decompress((self.parts / f"{digest}.json.gz").read_bytes())
        except (OSError, EOFError, gzip.BadGzipFile) as err:
            raise BackupError("file del backup mancante o rovinato") from err
        if hashlib.sha256(raw).hexdigest() != digest:
            raise BackupError("file del backup rovinato: il contenuto non corrisponde alla sua impronta")
        return json.loads(raw)

    def _path(self, backup_id: str) -> Path:
        if not ID_PATTERN.match(backup_id or ""):
            raise BackupError("backup non trovato")
        return self.root / f"{backup_id}.json"

    def new_id(self) -> str:
        base = datetime.now().strftime("%Y%m%d-%H%M%S")
        candidate, n = base, 1
        while (self.root / f"{candidate}.json").exists():
            n += 1
            candidate = f"{base}-{n}"
        return candidate

    def save(self, manifest: dict) -> None:
        path = self._path(manifest["id"])
        self.root.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".parziale")
        partial.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
        os.replace(partial, path)

    def list(self) -> list[dict]:
        """Backup dal più recente; un indice illeggibile si salta (e si segnala nel log)."""
        if not self.root.exists():
            return []
        items = []
        for path in self.root.glob("*.json"):
            try:
                manifest = json.loads(path.read_text())
            except (OSError, ValueError):
                _LOGGER.warning("Indice di backup illeggibile: %s", path.name)
                continue
            if isinstance(manifest, dict) and manifest.get("id") == path.stem:
                items.append(manifest)
        return sorted(items, key=lambda m: (m.get("created_at") or "", m["id"]), reverse=True)

    def get(self, backup_id: str) -> dict:
        path = self._path(backup_id)
        try:
            return json.loads(path.read_text())
        except FileNotFoundError as err:
            raise BackupError("backup non trovato") from err
        except (OSError, ValueError) as err:
            raise BackupError("indice del backup rovinato") from err

    def delete(self, backup_id: str) -> None:
        self._path(backup_id).unlink(missing_ok=True)
        self.collect_garbage()

    def collect_garbage(self) -> int:
        """Toglie le parti che nessun backup usa più."""
        if not self.parts.exists():
            return 0
        used = {part.get("hash") for m in self.list() for part in (m.get("parts") or {}).values()}
        removed = 0
        for path in self.parts.glob("*.json.gz"):
            if path.name.split(".")[0] not in used:
                path.unlink(missing_ok=True)
                removed += 1
        for path in self.parts.glob("*.parziale"):
            path.unlink(missing_ok=True)
        return removed

    def usage(self) -> int:
        """Spazio occupato da tutti i backup, in byte."""
        if not self.root.exists():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())


# Fotografia dello stato attuale


def _by(items: list[dict], key: str = "token") -> list[dict]:
    return sorted(items, key=lambda item: str(item.get(key, "")))


def common_snapshot(db: Database, schedules: list[dict]) -> dict:
    """Dati Comuni, senza quello che cambia da solo (ultimo accesso, token assegnati dal controller):
    un backup senza modifiche ha la stessa impronta del precedente.
    schedules: fasce orarie del controller (i nomi stanno lì, le regole nell'app)."""
    schedule_rules = db.schedule_rules()
    category_rules = db.all_category_rules()
    persons, cards = [], []
    for person in db.list_persons():
        persons.append(
            {
                "id": person["id"],
                "created_at": person["created_at"],
                "name": person["name"],
                "company": person["company"] or "",
                "notes": person["notes"] or "",
                "category_id": person["category_id"] or 0,
            }
        )
        for card in person["cards"]:
            cards.append(
                {
                    "number": card["number"],
                    "person_id": person["id"],
                    "enabled": bool(card["enabled"]),
                    "access_profile": card["access_profile"] or "",
                    "valid_from": card["valid_from"] or "",
                    "valid_to": card["valid_to"] or "",
                }
            )
    settings = {
        row["key"]: row["value"]
        for row in db.all_settings()
        if row["key"] in SETTINGS_KEPT or row["key"].startswith(SETTINGS_PREFIXES)
    }
    return {
        "format": FORMAT,
        "persons": _by(persons, "id"),
        "cards": _by(cards, "number"),
        "categories": [
            {
                "id": c["id"],
                "name": c["name"],
                "color": c["color"],
                "valid_days": c["valid_days"],
                "description": c["description"],
                "group_token": c["group_token"],
                "rules": category_rules.get(c["id"], []),
            }
            for c in sorted(db.list_categories(), key=lambda c: c["id"])
        ],
        "schedules": _by(
            [
                {
                    "token": s.get("token", ""),
                    "name": s.get("Name") or "",
                    "description": s.get("Description") or "",
                    **(schedule_rules.get(s.get("token")) or {}),
                }
                for s in schedules
            ]
        ),
        "holidays": [{"day": h["day"], "name": h["name"], "yearly": h["yearly"]} for h in db.list_holidays()],
        "sites": [
            {
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "areas": [{"id": a["id"], "name": a["name"]} for a in s["areas"]],
            }
            for s in sorted(db.list_sites(), key=lambda s: s["id"])
        ],
        "app_users": [
            {"username": u["username"], "name": u["name"], "perms": u["perms"]}
            for u in sorted(db.list_app_users(), key=lambda u: u["username"])
        ],
        "hidden": sorted(db.all_hidden(), key=lambda h: (h["kind"], h["token"])),
        "settings": settings,
    }


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


def _credential_ref(credential: dict) -> dict:
    """Quale tessera c'è sul controller, come riferimento ai Dati Comuni (non una copia della persona)."""
    return {
        "token": credential.get("token", ""),
        "user": credential.get("UserToken", ""),
        "card": card_of_credential(credential),
        "enabled": credential.get("Enabled", True) is not False,
        "valid_from": credential.get("ValidFrom") or "",
        "valid_to": credential.get("ValidTo") or "",
        "profiles": [p.get("AccessProfile") for p in credential.get("CredentialAccessProfile") or []],
    }


async def controller_snapshot(db: Database, row: dict, client) -> dict:
    """Configurazione di un controller come la legge l'app, più le impostazioni dell'app su quel controller.
    Se il controller non risponde solleva VapixError; una parte non leggibile finisce negli errori."""
    errors: list[str] = []

    async def read(label: str, call, default):
        try:
            return await call()
        except VapixError as err:
            errors.append(f"{label}: {err}")
            return default

    info = await client.device_info()
    doors = await read("porte", client.list_doors, [])
    door_configs = await read("configurazione delle porte", client.list_door_configurations, [])
    idpoints = await read("lettori", client.list_idpoints, [])
    idpoint_configs = await read("configurazione dei lettori", client.list_idpoint_configurations, [])
    points = await read("punti di accesso", client.list_access_points, [])
    profiles = await read("profili di accesso", client.list_access_profiles, [])
    schedules = await read("fasce orarie", client.list_schedules, [])
    _, io_assigned = await read("ingressi e uscite", client.list_io, ([], []))
    credentials = await read("tessere", client.list_credentials, [])
    device = _device(
        info,
        await _optional(client, "date_time_info"),
        await _optional(client, "ntp_info"),
        await _optional(client, "mqtt_client_config"),
        await _optional(client, "mqtt_event_config"),
    )
    return {
        "format": FORMAT,
        "controller": {
            k: row.get(k)
            for k in (
                "id",
                "name",
                "description",
                "host",
                "username",
                "verify_ssl",
                "area_id",
                "snapshot_camera",
                "mqtt_topic_prefix",
            )
        },
        "device": device,
        "doors": _by(doors),
        "door_configurations": _by(door_configs),
        "idpoints": _by(idpoints),
        "idpoint_configurations": _by(idpoint_configs),
        "access_points": _by(points),
        "access_profiles": _by(profiles),
        "schedules": _by(schedules),
        "io_assignments": _by(io_assigned, "IoName"),
        "credentials": _by([_credential_ref(c) for c in credentials], "card"),
        "app_doors": _by([{"id": d["id"], **{k: d[k] for k in Database.DOOR_FIELDS}} for d in row["doors"]], "id"),
        "app_inputs": _by(
            [{k: v for k, v in r.items() if k != "controller_id"} for r in db.controller_inputs(row["id"]).values()],
            "io",
        ),
        "errors": errors,
    }


def part_counts(key: str, part: dict) -> dict:
    if key == COMMON:
        return {
            "persone": len(part.get("persons", [])),
            "tessere": len(part.get("cards", [])),
            "tipologie": len(part.get("categories", [])),
            "fasce": len(part.get("schedules", [])),
        }
    # Pulsanti e relè sugli ingressi sono punti di identificazione come i lettori: si contano a parte
    wired = {r.get("idpoint_token") for r in part.get("app_inputs", [])} - {"", None}
    return {
        "porte": len(part.get("doors", [])),
        "lettori": sum(1 for p in part.get("idpoints", []) if p.get("token") not in wired),
        "ingressi": sum(1 for r in part.get("app_inputs", []) if r.get("role")),
        "tessere": len(part.get("credentials", [])),
    }


# Confronto tra due fotografie: oggetti aggiunti, tolti e modificati, campo per campo


def _text(value) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "sì" if value else "no"
    if isinstance(value, list):
        return ", ".join(_text(v) for v in value) or "—"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _when(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value or "—")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%d/%m/%Y %H:%M")


def _seconds(value) -> str:
    seconds = parse_duration(value)
    return f"{seconds} s" if seconds is not None else _text(value)


def _item(label: str, fields: dict, show: dict | None = None) -> dict:
    """Oggetto confrontabile: i campi si confrontano grezzi e si mostrano con show (se c'è)."""
    return {"label": label, "fields": fields, "show": show or {}}


def _place_names(common: dict) -> dict[int, str]:
    return {a["id"]: f"{s['name']} › {a['name']}" for s in common.get("sites", []) for a in s.get("areas", [])}


def context_of(parts: dict[str, dict]) -> dict:
    """Nomi da mostrare nei confronti: porte, aree, fasce e profili di tutta la fotografia."""
    common = parts.get(COMMON) or {}
    doors, profile_names = {}, {}
    for key, part in parts.items():
        if key == COMMON:
            continue
        for door in part.get("app_doors", []):
            doors[door["id"]] = door["name"]
            doors[door["door_token"]] = door["name"]
        for profile in part.get("access_profiles", []):
            profile_names[profile.get("token")] = profile.get("Name") or profile.get("token")
    for category in common.get("categories", []):
        if category.get("group_token"):
            profile_names[category["group_token"]] = category["name"]
    return {
        "doors": doors,
        "areas": _place_names(common),
        "sites": {s["id"]: s["name"] for s in common.get("sites", [])},
        "schedules": {s["token"]: s["name"] or s["token"] for s in common.get("schedules", [])},
        "profiles": profile_names,
        "categories": {c["id"]: c["name"] for c in common.get("categories", [])},
        "persons": {p["id"]: p["name"] for p in common.get("persons", [])},
    }


def rules_text(rules: list[dict], ctx: dict) -> str:
    names = {"door": ctx["doors"], "area": ctx["areas"], "site": ctx["sites"]}
    parts = []
    for rule in rules:
        place = (
            names.get(rule.get("target"), {}).get(rule.get("target_id"))
            or f"{rule.get('target')} {rule.get('target_id')}"
        )
        parts.append(f"{place}: {ctx['schedules'].get(rule.get('schedule'), rule.get('schedule'))}")
    return "; ".join(parts) or "—"


def _schedule_rules_text(rules: list[dict]) -> str:
    texts = []
    for rule in rules or []:
        days = ",".join(rule.get("days") or [])
        texts.append(f"{days} {rule.get('start', '')}-{rule.get('end', '')}".strip())
    return "; ".join(texts) or "—"


def common_view(part: dict, ctx: dict) -> dict[str, dict[str, dict]]:
    categories = ctx["categories"]
    view: dict[str, dict[str, dict]] = {key: {} for key in SECTIONS}
    for p in part.get("persons", []):
        category = p["category_id"]
        view["persone"][f"{p['id']}:{p['created_at']}"] = _item(
            p["name"],
            {k: p[k] for k in ("name", "company", "notes", "category_id")},
            {"category_id": categories.get(category, f"tipologia n. {category}") if category else "nessuna"},
        )
    for c in part.get("cards", []):
        view["tessere"][c["number"]] = _item(
            c["number"],
            {k: c[k] for k in ("person_id", "enabled", "access_profile", "valid_from", "valid_to")},
            {
                "person_id": ctx["persons"].get(c["person_id"], f"persona n. {c['person_id']}"),
                "enabled": "attiva" if c["enabled"] else "sospesa",
                "access_profile": ctx["profiles"].get(c["access_profile"], c["access_profile"]),
                "valid_from": _when(c["valid_from"]) if c["valid_from"] else "—",
                "valid_to": _when(c["valid_to"]) if c["valid_to"] else "—",
            },
        )
    for c in part.get("categories", []):
        view["tipologie"][str(c["id"])] = _item(
            c["name"],
            {k: c[k] for k in ("name", "color", "valid_days", "description", "rules")},
            {
                "color": COLOR_NAMES.get(c["color"], c["color"]),
                "valid_days": f"{c['valid_days']} giorni" if c["valid_days"] else "illimitata",
                "rules": rules_text(c["rules"], ctx),
            },
        )
    for s in part.get("schedules", []):
        view["fasce"][s["token"]] = _item(
            s["name"] or s["token"],
            {
                "name": s["name"],
                "description": s["description"],
                "rules": s.get("rules"),
                "holidays": s.get("holidays"),
            },
            {"rules": _schedule_rules_text(s.get("rules"))},
        )
    for h in part.get("holidays", []):
        view["festivita"][h["day"]] = _item(
            f"{h['name'] or 'Festività'} ({h['day']})", {"name": h["name"], "yearly": h["yearly"]}
        )
    for s in part.get("sites", []):
        view["strutture"][str(s["id"])] = _item(
            s["name"],
            {"name": s["name"], "description": s["description"], "areas": [a["name"] for a in s["areas"]]},
        )
    for u in part.get("app_users", []):
        view["utenti"][u["username"]] = _item(u["name"] or u["username"], {"name": u["name"], "perms": u["perms"]})
    for key, value in (part.get("settings") or {}).items():
        label = {
            "default_group": "Gruppo predefinito",
            "default_category": "Tipologia predefinita",
            "notifications": "Notifiche",
        }.get(key) or (f"Cruscotto di {key.split(':', 1)[1]}" if key.startswith("dashboard_layout:") else key)
        show = {}
        if key == "default_category":
            show["value"] = categories.get(int(value or 0), value)
        elif key.startswith("dashboard_layout:") or key == "notifications":
            show["value"] = "personalizzato"
        view["impostazioni"][key] = _item(label, {"value": value}, show)
    return view


def _attribute(item: dict, name: str) -> str:
    return next((str(a.get("Value") or "") for a in item.get("Attribute") or [] if a.get("Name") == name), "")


def _configuration_text(configs: list[dict], token: str) -> dict:
    item = next((c for c in configs if c.get("token") == token), None)
    return {str(c.get("Name")): str(c.get("Value")) for c in (item or {}).get("Configuration") or []}


def controller_view(part: dict, ctx: dict) -> dict[str, dict[str, dict]]:
    view: dict[str, dict[str, dict]] = {key: {} for key in SECTIONS}
    controller = part.get("controller") or {}
    view["collegamento"]["collegamento"] = _item(
        "Collegamento del controller",
        {k: controller.get(k) for k in ("name", "description", "host", "username", "verify_ssl", "area_id")},
        {"area_id": ctx["areas"].get(controller.get("area_id"), "—")},
    )
    device = part.get("device") or {}
    view["dispositivo"]["dispositivo"] = _item(
        "Impostazioni del dispositivo",
        {k: device.get(k) for k in ("model", "serial", "firmware", "time_zone", "ntp", "mqtt", "mqtt_events")},
        {
            "ntp": _text((device.get("ntp") or {}).get("servers")),
            "mqtt": _text((device.get("mqtt") or {}).get("server") or device.get("mqtt")),
            "mqtt_events": "configurati" if device.get("mqtt_events") else "—",
        },
    )
    app_doors = {d["door_token"]: d for d in part.get("app_doors", [])}
    for door in part.get("doors", []):
        token = door.get("token", "")
        app = app_doors.get(token) or {}
        fields = {"Name": door.get("Name") or "", "Description": door.get("Description") or ""}
        show = {}
        for field in DOOR_TIMES:
            if field in door:
                fields[field] = door[field]
                show[field] = _seconds(door[field])
        for key in Database.DOOR_FIELDS:
            if key != "door_token" and app:
                fields[key] = app.get(key)
        if app:
            show["area_id"] = ctx["areas"].get(app.get("area_id"), "come il controller")
        view["porte"][token] = _item(app.get("name") or door.get("Name") or token, fields, show)
    configs = part.get("idpoint_configurations", [])
    owned = {r.get("idpoint_token") for r in part.get("app_inputs", [])} - {""}
    for reader in part.get("idpoints", []):
        token = reader.get("token", "")
        if token in owned:
            continue
        view["lettori"][token] = _item(
            reader.get("Name") or token,
            {
                "Name": reader.get("Name") or "",
                "Description": reader.get("Description") or "",
                "Timeout": reader.get("Timeout") or "",
                "configuration": _configuration_text(configs, token),
            },
            {"Timeout": _seconds(reader.get("Timeout"))},
        )
    owned_points = {r.get("access_point_token") for r in part.get("app_inputs", [])} - {""}
    readers = {r.get("token"): r.get("Name") or r.get("token") for r in part.get("idpoints", [])}
    for point in part.get("access_points", []):
        token = point.get("token", "")
        if token in owned_points:
            continue
        linked = [d.get("IdPoint") for d in point.get("IdPointDevice") or [] if d.get("IdPoint")]
        view["punti"][token] = _item(
            point.get("Name") or token,
            {
                "Name": point.get("Name") or "",
                "door": point.get("Entity") or "",
                "readers": linked,
                "direction": _attribute(point, "Direction"),
                "Enabled": point.get("Enabled", True) is not False,
            },
            {
                "door": ctx["doors"].get(point.get("Entity"), point.get("Entity") or "—"),
                "readers": ", ".join(readers.get(r, r) for r in linked) or "—",
                "direction": {"in": "Entrata", "out": "Uscita"}.get(_attribute(point, "Direction"), "—"),
                "Enabled": "attivo" if point.get("Enabled", True) is not False else "disattivato",
            },
        )
    points = {p.get("token"): p for p in part.get("access_points", [])}
    for profile in part.get("access_profiles", []):
        token = profile.get("token", "")
        policies = [
            {"point": p.get("AccessPoint"), "schedules": p.get("Schedule") or []}
            for p in profile.get("AccessPolicy") or []
        ]
        policy_text = "; ".join(
            f"{(points.get(p['point']) or {}).get('Name') or p['point']}: "
            f"{', '.join(ctx['schedules'].get(s, s) for s in p['schedules'])}"
            for p in policies
        )
        view["profili"][token] = _item(
            profile.get("Name") or token,
            {
                "Name": profile.get("Name") or "",
                "Description": profile.get("Description") or "",
                "Schedule": profile.get("Schedule") or [],
                "policies": policies,
            },
            {
                "Schedule": ", ".join(ctx["schedules"].get(s, s) for s in profile.get("Schedule") or []) or "—",
                "policies": policy_text or "—",
            },
        )
    assigned = {a.get("IoName"): a for a in part.get("io_assignments", [])}
    inputs = {r["io"]: r for r in part.get("app_inputs", [])}
    for io in sorted(set(inputs) | {k for k, a in assigned.items() if a.get("IoUser")}):
        mine = inputs.get(io) or {}
        users = [f"{u.get('Type')} {u.get('token')}" for u in (assigned.get(io) or {}).get("IoUser") or []]
        view["ingressi"][io] = _item(
            mine.get("name") or io,
            {
                "name": mine.get("name") or "",
                "role": mine.get("role") or "",
                "door_token": mine.get("door_token") or "",
                "contact": mine.get("contact") or "",
                "supervised": bool(mine.get("supervised")),
                "schedule": mine.get("schedule") or "",
                "assigned": users,
            },
            {
                "role": INPUT_ROLES.get(mine.get("role") or "", mine.get("role") or "—"),
                "door_token": ctx["doors"].get(mine.get("door_token"), mine.get("door_token") or "—"),
                "contact": {"no": "normalmente aperto", "nc": "normalmente chiuso"}.get(mine.get("contact"), "—"),
                "schedule": ctx["schedules"].get(mine.get("schedule"), mine.get("schedule") or "—"),
            },
        )
    return view


def view_of(key: str, part: dict | None, ctx: dict) -> dict:
    if not part:
        return {k: {} for k in SECTIONS}
    return common_view(part, ctx) if key == COMMON else controller_view(part, ctx)


def _shown(item: dict, field: str) -> str:
    if field in item["show"]:
        return _text(item["show"][field])
    value = item["fields"].get(field)
    if field in ("valid_from", "valid_to") and value:
        return _when(value)
    return _text(value)


def diff_views(old: dict, new: dict) -> list[dict]:
    """Sezioni con differenze: aggiunti, tolti e modificati (con i campi cambiati)."""
    sections = []
    for key in SECTIONS:
        before, after = old.get(key, {}), new.get(key, {})
        added = [{"key": k, "label": after[k]["label"]} for k in after if k not in before]
        removed = [{"key": k, "label": before[k]["label"]} for k in before if k not in after]
        changed = []
        for k in after:
            if k not in before:
                continue
            a, b = before[k], after[k]
            names = list(dict.fromkeys([*b["fields"], *a["fields"]]))
            fields = [
                {"field": f, "label": FIELD_LABELS.get(f, f), "old": _shown(a, f), "new": _shown(b, f)}
                for f in names
                if a["fields"].get(f) != b["fields"].get(f)
            ]
            if fields:
                changed.append({"key": k, "label": b["label"], "fields": fields})
        if added or removed or changed:
            sort = lambda items: sorted(items, key=lambda i: str(i["label"]).lower())  # noqa: E731
            sections.append(
                {
                    "key": key,
                    "title": SECTION_TITLES[key],
                    "added": sort(added),
                    "removed": sort(removed),
                    "changed": sort(changed),
                }
            )
    return sections


def part_title(key: str, part: dict | None) -> str:
    if key == COMMON:
        return "Dati Comuni"
    return ((part or {}).get("controller") or {}).get("name") or f"Controller {key.split(':', 1)[-1]}"


def compare_parts(old: dict[str, dict], new: dict[str, dict], only: str | None = None) -> list[dict]:
    """Differenze tra due fotografie, parte per parte (Dati Comuni e ogni controller)."""
    old_ctx, new_ctx = context_of(old), context_of(new)
    keys = [k for k in dict.fromkeys([*new, *old]) if only is None or k == only]
    groups = []
    for key in sorted(keys, key=lambda k: (k != COMMON, k)):
        sections = diff_views(view_of(key, old.get(key), old_ctx), view_of(key, new.get(key), new_ctx))
        note = ""
        if key not in old:
            note = "non c'era nel backup più vecchio"
        elif key not in new:
            note = "non c'è nel backup più recente"
        if sections or note:
            groups.append(
                {
                    "part": key,
                    "title": part_title(key, new.get(key) or old.get(key)),
                    "note": note,
                    "sections": sections,
                }
            )
    return groups


def _named(noun: str, label: str) -> str:
    """ "Porta Scala Teatro", ma "Porta B" e non "Porta Porta B" se il nome comincia già così."""
    return label if label.lower().split(" ")[0] == noun.lower() else f"{noun.capitalize()} {label}"


def summary_text(groups: list[dict], limit: int = 6) -> str:
    """Riassunto di un confronto: "+3 tessere, Porta Scala Teatro modificata"."""
    phrases = []
    for group in groups:
        for section in group["sections"]:
            one, many, female = SECTIONS[section["key"]]
            if section["added"]:
                phrases.append(f"+{plural(len(section['added']), one, many)}")
            if section["removed"]:
                phrases.append(f"−{plural(len(section['removed']), one, many)}")
            changed = section["changed"]
            if section["key"] in ("collegamento", "dispositivo") and changed:
                phrases.append(f"{one.capitalize()} di {group['title']} modificat{'e' if female else 'o'}")
            elif 0 < len(changed) <= 2:
                phrases += [f"{_named(one, c['label'])} modificat{'a' if female else 'o'}" for c in changed]
            elif changed:
                phrases.append(f"{len(changed)} {many} modificat{'e' if female else 'i'}")
    if not phrases:
        return "Nessuna modifica"
    return ", ".join(phrases[:limit]) + ("…" if len(phrases) > limit else "")


def clean_name(value: str | None) -> str:
    name = " ".join(str(value or "").split())
    if len(name) > NAME_MAX:
        raise BackupError(f"nome del backup troppo lungo (massimo {NAME_MAX} caratteri)")
    return name


def new_manifest(store: BackupStore, kind: str, name: str, user: str, app_version: str) -> dict:
    return {
        "format": FORMAT,
        "id": store.new_id(),
        "created_at": now_iso(),
        "kind": kind,
        "name": name,
        "user": user,
        "locked": False,
        "app_version": app_version,
        "parts": {},
        "errors": [],
        "summary": "",
        "previous": "",
    }


class BackupService:
    """Crea, elenca, confronta, blocca ed elimina i backup. Il ripristino è in restore.py."""

    def __init__(self, db: Database, root: Path | str, app_version: str) -> None:
        self.db = db
        self.store = BackupStore(root)
        self.app_version = app_version
        # Un backup alla volta: la pulizia delle parti non deve togliere quelle di un backup in corso
        self.lock = asyncio.Lock()

    def _parts_of(self, manifest: dict) -> dict[str, dict]:
        return {key: self.store.read_part(info["hash"]) for key, info in (manifest.get("parts") or {}).items()}

    def latest(self) -> dict | None:
        items = self.store.list()
        return items[0] if items else None

    async def snapshot(self, clients: dict[int, object]) -> tuple[dict[str, dict], dict[str, str]]:
        """Fotografia dello stato attuale ed errori per parte. clients: controller → client con cui leggerlo.
        Un controller che non risponde non ha la sua parte; le fasce orarie dei Dati Comuni si leggono dal primo
        controller che risponde, altrimenti si tengono quelle dell'ultimo backup."""
        parts: dict[str, dict] = {}
        errors: dict[str, str] = {}
        schedules = None
        for row in self.db.list_controllers():
            client = clients.get(row["id"])
            if not row["host"] or client is None:
                continue
            key = f"controller:{row['id']}"
            try:
                part = await controller_snapshot(self.db, row, client)
            except VapixError as err:
                errors[key] = f"{row['name']}: {err}"
                continue
            parts[key] = part
            if part["errors"]:
                errors[key] = f"{row['name']}: {'; '.join(part['errors'])}"
            if schedules is None and not any(e.startswith("fasce orarie") for e in part["errors"]):
                schedules = part["schedules"]
        if schedules is None:
            schedules = self._last_schedules()
        parts[COMMON] = common_snapshot(self.db, schedules)
        return parts, errors

    def _last_schedules(self) -> list[dict]:
        latest = self.latest()
        if not latest or COMMON not in latest.get("parts", {}):
            return []
        try:
            common = self.store.read_part(latest["parts"][COMMON]["hash"])
        except BackupError:
            return []
        return [{"token": s["token"], "Name": s["name"], "Description": s["description"]} for s in common["schedules"]]

    async def create(
        self, kind: str, name: str, user: str, clients: dict[int, object], skip_unchanged: bool = False
    ) -> dict | None:
        """Nuovo backup. Con skip_unchanged non si crea se è tutto uguale all'ultimo (restituisce None).
        Un controller che non risponde tiene la parte dell'ultimo backup, segnata: il backup è incompleto."""
        if kind not in KINDS:
            raise BackupError("tipo di backup sconosciuto")
        name = clean_name(name)
        async with self.lock:
            try:
                parts, errors = await self.snapshot(clients)
                last = self.latest()
                try:
                    last_parts = self._parts_of(last) if last else {}
                except BackupError as err:
                    _LOGGER.warning("Ultimo backup non leggibile, confronto saltato: %s", err)
                    last, last_parts = None, {}
                manifest = new_manifest(self.store, kind, name, user, self.app_version)
                for key, part in parts.items():
                    manifest["parts"][key] = {
                        **self.store.write_part(part),
                        "title": part_title(key, part),
                        "counts": part_counts(key, part),
                    }
                for key in errors:
                    if key not in parts and last and key in last["parts"]:
                        manifest["parts"][key] = {**last["parts"][key], "stale": last["id"]}
                        parts[key] = last_parts[key]
                manifest["errors"] = list(errors.values())
                hashes = {k: v["hash"] for k, v in manifest["parts"].items()}
                if skip_unchanged and last and hashes == {k: v["hash"] for k, v in last["parts"].items()}:
                    return None
                if last:
                    groups = compare_parts(last_parts, parts)
                    manifest["summary"] = summary_text(groups)
                    manifest["previous"] = last["id"]
                    for key, info in manifest["parts"].items():
                        info["summary"] = summary_text([g for g in groups if g["part"] == key])
                else:
                    manifest["summary"] = "Primo backup"
                self.store.save(manifest)
            except OSError as err:
                self._note_error(f"backup non salvato: {err}")
                raise BackupError(f"backup non salvato: {err.strerror or err}") from err
        if manifest["errors"]:
            self._note_error("; ".join(manifest["errors"]), manifest["id"])
        else:
            self.db.set_setting(SETTING_ERROR, "")
        label = manifest["name"] or KINDS[kind]
        self.db.add_audit(user, "backup creato", f"{label}: {manifest['summary']}")
        return manifest

    def _note_error(self, error: str, backup_id: str = "") -> None:
        self.db.set_setting(SETTING_ERROR, json.dumps({"at": now_iso(), "error": error, "id": backup_id}))

    def last_error(self) -> dict | None:
        try:
            value = json.loads(self.db.get_setting(SETTING_ERROR) or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def listing(self) -> dict:
        items = self.store.list()
        return {
            "backups": [{**m, "kind_label": KINDS.get(m.get("kind"), m.get("kind"))} for m in items],
            "size": self.store.usage(),
            "error": self.last_error(),
        }

    def get(self, backup_id: str) -> dict:
        return self.store.get(backup_id)

    def load(self, backup_id: str) -> tuple[dict, dict[str, dict]]:
        manifest = self.store.get(backup_id)
        return manifest, self._parts_of(manifest)

    async def set_locked(self, backup_id: str, locked: bool, user: str) -> dict:
        async with self.lock:
            manifest = self.store.get(backup_id)
            if bool(manifest.get("locked")) != locked:
                manifest["locked"] = locked
                self.store.save(manifest)
                label = manifest.get("name") or _when(manifest["created_at"])
                self.db.add_audit(user, "backup bloccato" if locked else "backup sbloccato", label)
        return manifest

    async def delete(self, backup_id: str, user: str) -> None:
        async with self.lock:
            manifest = self.store.get(backup_id)
            if manifest.get("locked"):
                raise BackupError("il backup è bloccato: sbloccalo prima di eliminarlo")
            self.store.delete(backup_id)
        self.db.add_audit(user, "backup eliminato", manifest.get("name") or _when(manifest["created_at"]))

    def previous_of(self, backup_id: str) -> dict | None:
        items = self.store.list()
        ids = [m["id"] for m in items]
        if backup_id not in ids:
            raise BackupError("backup non trovato")
        index = ids.index(backup_id)
        return items[index + 1] if index + 1 < len(items) else None

    async def compare(self, backup_id: str, other: str, clients: dict[int, object], part: str | None = None) -> dict:
        """Differenze tra un backup e il precedente, lo stato attuale o un altro backup: sempre dal più vecchio
        al più recente."""
        manifest, parts = self.load(backup_id)
        if other == "attuale":
            current, _ = await self.snapshot(clients)
            older, newer = (manifest, parts), ({"id": "attuale", "created_at": now_iso()}, current)
        else:
            other_manifest = self.previous_of(backup_id) if other == "precedente" else self.store.get(other)
            if other_manifest is None:
                return {"older": None, "newer": _brief(manifest), "groups": [], "first": True}
            pair = [(manifest, parts), (other_manifest, self._parts_of(other_manifest))]
            pair.sort(key=lambda p: (p[0]["created_at"], p[0]["id"]))
            older, newer = pair
        return {
            "older": _brief(older[0]),
            "newer": _brief(newer[0]),
            "groups": compare_parts(older[1], newer[1], part),
        }


def _brief(manifest: dict) -> dict:
    return {
        "id": manifest["id"],
        "created_at": manifest.get("created_at"),
        "name": manifest.get("name", ""),
        "kind": manifest.get("kind", ""),
        "kind_label": KINDS.get(manifest.get("kind"), "Stato Attuale" if manifest["id"] == "attuale" else ""),
    }
