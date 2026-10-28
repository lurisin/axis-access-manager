"""Impianti configurati nell'app: controller (indirizzo e credenziali) e porte con citofono e telecamera."""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import re

import httpx

from .config import Settings, normalize_host
from .db import Database
from .service import ServiceError, normalize_name

# Per ora l'add-on gestisce un controller; il database è già pronto per averne di più
MAX_CONTROLLERS = 1
DEFAULT_SITE = "Struttura Principale"
DEFAULT_AREA = "Generale"
TOPIC_PATTERN = re.compile(r"^[A-Za-z0-9_\-/]+$")
# Nome o IPv4, oppure IPv6 tra parentesi quadre, con la porta facoltativa
HOST_PATTERN = re.compile(r"^(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:.]+\])(?::(\d{1,5}))?$")
# Entità telecamera di Home Assistant: l'app la chiede a Home Assistant nell'indirizzo, niente altro ci deve stare
CAMERA_PATTERN = re.compile(r"^camera\.[a-z0-9_]+$")
CAMERA_ERROR = "la telecamera deve essere un'entità camera.* di Home Assistant"
# Spostamento dei controller nelle aree (versioni prima della 0.7) già fatto: non si ripete a ogni avvio
SETTING_SITES_MIGRATED = "sites_migrated"


def _flag(value) -> bool:
    return bool(value) and value not in ("0", "false", "False")


def check_host(value: str) -> str:
    """Indirizzo del controller come lo userà il client: un IPv6 scritto senza parentesi le riceve.
    Un indirizzo che il client non saprebbe usare è un errore qui, non al riavvio dell'app."""
    host = normalize_host(value or "")
    if not host:
        raise ServiceError("manca l'indirizzo del controller")
    with contextlib.suppress(ValueError):
        ipaddress.IPv6Address(host)
        host = f"[{host}]"
    match = HOST_PATTERN.match(host)
    port = int(match.group(1)) if match and match.group(1) else None
    try:
        httpx.URL(f"https://{host}")
    except httpx.InvalidURL:
        match = None
    if len(host) > 253 or not match or (port is not None and not 1 <= port <= 65535):
        raise ServiceError("indirizzo del controller non valido: scrivi il nome o l'IP, con la porta se serve")
    return host


def same_controller(host: str, username: str, existing: dict) -> bool:
    """Stesso indirizzo (senza maiuscole) e stesso utente del controller salvato: solo lì va la sua password."""
    try:
        saved = check_host(existing["host"] or "")
    except ServiceError:
        # Salvato da una versione precedente, prima del controllo dell'indirizzo
        saved = existing["host"] or ""
    return (host.lower(), username.strip()) == (saved.lower(), (existing["username"] or "").strip())


def valid_camera(camera: str) -> bool:
    return bool(CAMERA_PATTERN.match(camera))


class InstallationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # Lettura

    def controllers(self, redact: bool = True) -> list[dict]:
        result = self.db.list_controllers()
        if redact:
            for controller in result:
                controller["has_password"] = bool(controller.pop("password"))
        return result

    @property
    def primary(self) -> dict | None:
        """Controller in uso (il primo). Una telecamera salvata che non è un'entità camera non si usa."""
        controllers = self.db.list_controllers()
        if not controllers:
            return None
        controller = controllers[0]
        for item in (controller, *controller["doors"]):
            if not valid_camera(item.get("snapshot_camera") or ""):
                item["snapshot_camera"] = ""
        return controller

    def doors(self) -> list[dict]:
        primary = self.primary
        return primary["doors"] if primary else []

    def door_by_token(self, token: str) -> dict | None:
        return next((d for d in self.doors() if d["door_token"] == token), None)

    def door_area(self, door: dict, controllers: dict[int, dict] | None = None) -> int:
        """Area della porta: quella del suo controller, salvo un'area scelta apposta per la porta."""
        if door["area_id"]:
            return door["area_id"]
        controller = (controllers or {}).get(door["controller_id"]) or self.db.get_controller(door["controller_id"])
        return controller["area_id"] if controller else 0

    def locations(self) -> list[dict]:
        """Strutture con le loro aree, i controller e le porte di ogni area; il resto in fondo."""
        controllers = {c["id"]: c for c in self.db.list_controllers()}
        doors = self.db.list_doors()
        door_areas = {d["id"]: self.door_area(d, controllers) for d in doors}
        sites = self.db.list_sites()
        known = set()
        for site in sites:
            for area in site["areas"]:
                area["doors"] = [d["id"] for d in doors if door_areas[d["id"]] == area["id"]]
                area["controllers"] = [c["id"] for c in controllers.values() if c["area_id"] == area["id"]]
                known.add(area["id"])
        loose_doors = [d["id"] for d in doors if door_areas[d["id"]] not in known]
        loose_controllers = [c["id"] for c in controllers.values() if c["area_id"] not in known]
        if not loose_doors and not loose_controllers:
            return sites
        loose_area = {
            "id": 0,
            "site_id": 0,
            "name": "Senza Area",
            "doors": loose_doors,
            "controllers": loose_controllers,
        }
        return sites + [{"id": 0, "name": "Senza Struttura", "description": "", "areas": [loose_area]}]

    def _places(self, key: str) -> dict[int, dict]:
        places = {}
        for site in self.locations():
            for area in site["areas"]:
                for item_id in area[key]:
                    places[item_id] = {
                        "site": site["name"],
                        "area": area["name"],
                        "site_id": site["id"],
                        "area_id": area["id"],
                    }
        return places

    def door_places(self) -> dict[int, dict]:
        """Per ogni porta: struttura e area, per le etichette dell'interfaccia."""
        return self._places("doors")

    def controller_places(self) -> dict[int, dict]:
        return self._places("controllers")

    def ensure_default_site(self) -> bool:
        """Al primo avvio con questa versione, per i dati delle versioni prima della 0.7: ogni controller va in
        un'area, e le porte la ereditano. Poi non si ripete: un controller lasciato Senza Area resta dov'è.

        Prima della 0.6 non c'erano strutture: si crea quella predefinita da rinominare. Dalla 0.6 alla 0.7
        l'area passa dalle porte al loro controller; resta sulla porta solo se diversa da quella del controller.
        """
        if self.db.get_setting(SETTING_SITES_MIGRATED):
            return False
        changed = False
        controllers = self.db.list_controllers()
        if not self.db.list_sites() and any(not c["area_id"] for c in controllers):
            site_id = self.db.save_site(None, DEFAULT_SITE, "")
            area_id = self.db.save_area(None, site_id, DEFAULT_AREA)
            for controller in controllers:
                if not controller["area_id"]:
                    self.db.set_controller_area(controller["id"], area_id)
            self.db.add_audit("sistema", "struttura predefinita creata", f"{DEFAULT_SITE} › {DEFAULT_AREA}")
            changed = True
        for controller in self.db.list_controllers():
            areas = [d["area_id"] for d in controller["doors"] if d["area_id"]]
            if controller["area_id"] or not areas:
                continue
            area_id = max(set(areas), key=areas.count)
            self.db.set_controller_area(controller["id"], area_id)
            for door in controller["doors"]:
                if door["area_id"] == area_id:
                    self.db.set_door_area(door["id"], 0)
            self.db.add_audit(
                "sistema",
                "controller spostato nell'area delle sue porte",
                f"{controller['name']}: {self._place(area_id)}",
            )
            changed = True
        self.db.set_setting(SETTING_SITES_MIGRATED, "1")
        return changed

    def _place(self, area_id: int) -> str:
        area = self.db.get_area(area_id)
        site = self.db.get_site(area["site_id"]) if area else None
        return f"{site['name']} › {area['name']}" if area and site else ""

    def effective_settings(self, base: Settings) -> Settings:
        """Opzioni dell'add-on con controller e prima porta presi dall'app."""
        controller = self.primary
        if controller is None:
            return dataclasses.replace(base, a1210_host="", door_token="", intercom_name="")
        door = controller["doors"][0] if controller["doors"] else None
        values = {
            "a1210_host": controller["host"],
            "a1210_username": controller["username"],
            "a1210_password": controller["password"],
            "a1210_verify_ssl": controller["verify_ssl"],
            "mqtt_topic_prefix": controller["mqtt_topic_prefix"],
            "door_token": door["door_token"] if door else "",
            "snapshot_camera": door["snapshot_camera"] if door else "",
            "snapshot_on_granted": door["snapshot_on_granted"] if door else False,
            "ha_door_buttons": door["ha_buttons"] if door else False,
            "intercom_name": door["intercom_name"] if door else "",
            "intercom_open_door": door["intercom_open_door"] if door else False,
            "intercom_relay": door["intercom_relay"] if door else 1,
            "intercom_mqtt_prefix": door["intercom_mqtt_prefix"] if door else "hmd",
        }
        return dataclasses.replace(base, **values)

    # Prima installazione della 0.5: quello che c'era nelle opzioni passa nell'app

    def migrate_from_options(self, options: Settings) -> bool:
        if self.db.list_controllers() or not options.a1210_host:
            if options.access_profile and not self.db.get_setting("default_group"):
                self.db.set_setting("default_group", options.access_profile)
            return False
        controller_id = self.db.save_controller(
            None,
            {
                "name": "Controller A1210",
                "host": options.a1210_host,
                "username": options.a1210_username,
                "password": options.a1210_password,
                "verify_ssl": int(options.a1210_verify_ssl),
                "mqtt_topic_prefix": options.mqtt_topic_prefix,
            },
        )
        if options.door_token:
            self.db.save_door(
                None,
                controller_id,
                {
                    "door_token": options.door_token,
                    "name": "Porta",
                    "snapshot_camera": options.snapshot_camera if valid_camera(options.snapshot_camera) else "",
                    "snapshot_on_granted": int(options.snapshot_on_granted),
                    "ha_buttons": int(options.ha_door_buttons),
                    "intercom_name": options.intercom_name,
                    "intercom_open_door": int(options.intercom_open_door),
                    "intercom_relay": options.intercom_relay,
                    "intercom_mqtt_prefix": options.intercom_mqtt_prefix or "hmd",
                },
            )
        if options.access_profile and not self.db.get_setting("default_group"):
            self.db.set_setting("default_group", options.access_profile)
        self.db.add_audit("sistema", "impianto importato dalle opzioni dell'add-on", options.a1210_host)
        return True

    # Modifica

    def controller_fields(self, data: dict, existing: dict | None) -> dict:
        name = normalize_name(data.get("name") or "")
        host = check_host(data.get("host") or "")
        username = (data.get("username") or "").strip()
        if not username:
            raise ServiceError("manca il nome utente del controller")
        password = data.get("password") or ""
        if not password:
            if not existing or not existing["password"]:
                raise ServiceError("manca la password del controller")
            # La password salvata va solo all'indirizzo e all'utente salvati, anche dal controllo periodico
            if not same_controller(host, username, existing):
                raise ServiceError("indirizzo o utente cambiati: scrivi di nuovo la password del controller")
            password = existing["password"]
        prefix = (data.get("mqtt_topic_prefix") or "").strip().strip("/")
        if not prefix or not TOPIC_PATTERN.match(prefix):
            raise ServiceError("prefisso MQTT non valido: usa lettere, numeri, _ - e /")
        area_id = data.get("area_id")
        if area_id is None:
            area_id = existing["area_id"] if existing else self._default_area()
        return {
            "name": name,
            "host": host,
            "username": username[:100],
            "password": password,
            "verify_ssl": int(_flag(data.get("verify_ssl"))),
            "mqtt_topic_prefix": prefix,
            "area_id": self._area(area_id),
            "description": (data.get("description") or "").strip()[:200],
        }

    def save_controller(self, controller_id: int | None, data: dict, user: str) -> dict:
        existing = self.db.get_controller(controller_id) if controller_id else None
        if controller_id and existing is None:
            raise ServiceError("controller non trovato")
        if not controller_id and len(self.db.list_controllers()) >= MAX_CONTROLLERS:
            raise ServiceError("per ora l'add-on gestisce un solo controller")
        fields = self.controller_fields(data, existing)
        controller_id = self.db.save_controller(controller_id, fields)
        if existing and existing["name"] and existing["name"] != fields["name"]:
            # Registro e notifiche mostrano il nome attuale, non quello di quando è successo
            self.db.set_controller_event_source(fields["name"], old=existing["name"])
        self.db.add_audit(
            user, "controller modificato" if existing else "controller aggiunto", f"{fields['name']} ({fields['host']})"
        )
        return {"id": controller_id}

    def set_controller_camera(self, controller_id: int, camera: str, user: str) -> dict:
        controller = self.db.get_controller(controller_id)
        if controller is None:
            raise ServiceError("controller non trovato")
        camera = camera.strip()
        if camera and not valid_camera(camera):
            raise ServiceError(CAMERA_ERROR)
        self.db.set_controller_camera(controller_id, camera)
        self.db.add_audit(
            user,
            "telecamera del controller modificata",
            f"{controller['name']}: {camera}" if camera else f"{controller['name']}: nessuna",
        )
        return {"id": controller_id, "snapshot_camera": camera}

    def update_controller_event_names(self) -> int:
        """All'avvio: con un solo controller, gli eventi del coperchio prendono il suo nome attuale."""
        controllers = self.db.list_controllers()
        if len(controllers) != 1 or not controllers[0]["name"]:
            return 0
        return self.db.set_controller_event_source(controllers[0]["name"])

    def delete_controller(self, controller_id: int, user: str) -> None:
        controller = self.db.get_controller(controller_id)
        if controller is None:
            raise ServiceError("controller non trovato")
        self.db.delete_controller(controller_id)
        self.db.add_audit(user, "controller rimosso dall'app", f"{controller['name']} ({controller['host']})")

    # Strutture e aree

    def save_site(self, site_id: int | None, data: dict, user: str) -> dict:
        existing = self.db.get_site(site_id) if site_id else None
        if site_id and existing is None:
            raise ServiceError("struttura non trovata")
        if not (data.get("name") or "").strip():
            raise ServiceError("manca il nome della struttura")
        name = normalize_name(data["name"])
        clash = self.db.site_by_name(name)
        if clash and clash["id"] != site_id:
            raise ServiceError(f"esiste già una struttura chiamata {clash['name']}")
        description = (data.get("description") or "").strip()[:200]
        site_id = self.db.save_site(site_id, name[:64], description)
        self.db.add_audit(user, "struttura modificata" if existing else "struttura aggiunta", name)
        return {"id": site_id}

    def delete_site(self, site_id: int, user: str) -> None:
        site = self.db.get_site(site_id)
        if site is None:
            raise ServiceError("struttura non trovata")
        self.db.delete_site(site_id)
        self.db.add_audit(user, "struttura rimossa", site["name"])

    def save_area(self, area_id: int | None, site_id: int, data: dict, user: str) -> dict:
        existing = self.db.get_area(area_id) if area_id else None
        if area_id and existing is None:
            raise ServiceError("area non trovata")
        site_id = int(data.get("site_id") or site_id or (existing or {}).get("site_id") or 0)
        site = self.db.get_site(site_id)
        if site is None:
            raise ServiceError("struttura non trovata")
        if not (data.get("name") or "").strip():
            raise ServiceError("manca il nome dell'area")
        name = normalize_name(data["name"])
        clash = self.db.area_by_name(site_id, name)
        if clash and clash["id"] != area_id:
            raise ServiceError(f"in {site['name']} esiste già l'area {clash['name']}")
        area_id = self.db.save_area(area_id, site_id, name[:64])
        self.db.add_audit(user, "area modificata" if existing else "area aggiunta", f"{site['name']} › {name}")
        return {"id": area_id}

    def delete_area(self, area_id: int, user: str) -> None:
        area = self.db.get_area(area_id)
        if area is None:
            raise ServiceError("area non trovata")
        self.db.delete_area(area_id)
        self.db.add_audit(user, "area rimossa", area["name"])

    def door_fields(self, data: dict) -> dict:
        token = (data.get("door_token") or "").strip()
        if not token:
            raise ServiceError("scegli la porta del controller")
        intercom = (data.get("intercom_name") or "").strip()
        prefix = (data.get("intercom_mqtt_prefix") or "hmd").strip().strip("/")
        if not TOPIC_PATTERN.match(prefix):
            raise ServiceError("prefisso MQTT del citofono non valido")
        try:
            relay = int(data.get("intercom_relay") or 1)
        except (TypeError, ValueError) as err:
            raise ServiceError("relè del citofono non valido") from err
        if relay not in (1, 2):
            raise ServiceError("il relè del citofono è 1 o 2")
        camera = (data.get("snapshot_camera") or "").strip()
        if camera and not valid_camera(camera):
            raise ServiceError(CAMERA_ERROR)
        return {
            "door_token": token,
            "name": normalize_name(data.get("name") or ""),
            "snapshot_camera": camera,
            "snapshot_on_granted": int(_flag(data.get("snapshot_on_granted"))),
            "ha_buttons": int(_flag(data.get("ha_buttons", True))),
            "intercom_name": intercom[:100],
            "intercom_open_door": int(_flag(data.get("intercom_open_door")) and bool(intercom)),
            "intercom_relay": relay,
            "intercom_mqtt_prefix": prefix,
            "area_id": self._area(data.get("area_id")),
        }

    def _area(self, value) -> int:
        try:
            area_id = int(value or 0)
        except (TypeError, ValueError) as err:
            raise ServiceError("area non valida") from err
        if area_id and self.db.get_area(area_id) is None:
            raise ServiceError("area non trovata")
        return area_id

    def save_door(self, door_id: int | None, controller_id: int, data: dict, user: str) -> dict:
        controller = self.db.get_controller(controller_id)
        if controller is None:
            raise ServiceError("controller non trovato")
        existing = self.db.get_door(door_id) if door_id else None
        if door_id and (existing is None or existing["controller_id"] != controller_id):
            raise ServiceError("porta non trovata")
        if data.get("area_id") is None and existing:
            data = {**data, "area_id": existing["area_id"]}
        # Senza un'area scelta la porta eredita quella del controller
        fields = self.door_fields(data)
        clash = next(
            (d for d in controller["doors"] if d["door_token"] == fields["door_token"] and d["id"] != door_id), None
        )
        if clash:
            raise ServiceError(f"la porta del controller è già configurata come {clash['name']}")
        door_id = self.db.save_door(door_id, controller_id, fields)
        detail = fields["name"] + (f", citofono {fields['intercom_name']}" if fields["intercom_name"] else "")
        self.db.add_audit(user, "porta modificata" if existing else "porta aggiunta", detail)
        return {"id": door_id}

    def _default_area(self) -> int:
        """Area per un controller nuovo senza area scelta: l'unica che c'è, o una nuova struttura predefinita."""
        areas = [a for s in self.db.list_sites() for a in s["areas"]]
        if len(areas) == 1:
            return areas[0]["id"]
        if areas:
            return 0
        site = self.db.site_by_name(DEFAULT_SITE)
        site_id = site["id"] if site else self.db.save_site(None, DEFAULT_SITE, "")
        return self.db.save_area(None, site_id, DEFAULT_AREA)

    def delete_door(self, door_id: int, user: str) -> None:
        door = self.db.get_door(door_id)
        if door is None:
            raise ServiceError("porta non trovata")
        self.db.delete_door(door_id)
        self.db.add_audit(user, "porta rimossa dall'app", door["name"])
