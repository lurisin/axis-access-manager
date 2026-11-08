"""Stato voluto: il payload Axis completo che l'app vuole sul controller, calcolato dal database e mai da oggetti letti.

Funzioni pure: nessuna lettura dal controller e nessuna scrittura. load_conf legge il database in una volta, senza
await in mezzo (quindi nessun'altra operazione dell'app lo cambia a metà), e dà la configurazione in memoria; build ne
calcola tipo → {chiave dell'app → payload Axis} per un controller. Le chiavi dell'app sono i token di oggi.

Porte, lettori, punti di accesso, nomi delle fasce, profilo dei pulsanti e impostazioni del dispositivo oggi stanno
solo sul controller: finché la presa in carico (presa_in_carico.py, nella 0.28 solo in memoria) non li mette nella
configurazione, quei tipi sono «da prendere in carico» (pending) e il confronto li mostra come sono sul controller.

Configurazione (conf), dal database con load_conf:
    persons [{id, name, category_id, axis_user_token, cards: [{number, enabled, axis_credential_token, access_profile,
    valid_from, valid_to, missing_since, ...}], ...}], categories [{id, name, color, group_token, valid_days,
    description, rules}], schedule_rules {token: {rules, holidays}}, holidays, controllers [{id, name, area_id}],
    doors [{id, controller_id, door_token, name, area_id, ...}], sites, inputs {controller_id: {io: riga}},
    default_group, default_category.
Dalla presa in carico, come le tabelle del passo 4 (vedi progetto, modello dei dati):
    controllers[*]: presa (True), device {time_zone, ntp, mqtt, mqtt_events}, rex_profile (token), io {IoName:
    assegnazione presa così com'è}; doors[*]: description, access_time... (secondi, DOOR_TIMES), lock_config {Nome:
    Valore}; readers [{controller_id, token, name, description, timeout, config, bus, io_mode, io_usage, io_extra}];
    access_points [{controller_id, token, name, description, door, entity_type, enabled, auth_profile, direction,
    attributes, readers}]; schedules [{token, name, description, rules, holidays, ical, factory}].
"""

from __future__ import annotations

import copy
import hashlib

from .backup import canonical
from .compare import KINDS, normalize
from .hardware import ALWAYS, _plain_mode
from .programming import (
    DOOR_MONITOR_OFF,
    DOOR_MONITOR_VALUES,
    DOOR_TIMES,
    REX_AUTHENTICATION,
    REX_ROLES,
    door_monitor_users,
    format_duration,
    parse_duration,
    rex_assignment,
    rex_configuration,
    rex_idpoint,
    rex_point_fields,
    rex_policies,
    rex_profile,
)
from .rules import holidays_ical, schedule_ical
from .service import USER_DESCRIPTION, new_credential

# Tipi che senza la presa in carico l'app non sa ancora com'è giusto che siano
PROGRAMMING = frozenset(KINDS) - {"utente", "tessera"}
# Campi dei controller nella configurazione: niente indirizzo né credenziali
CONTROLLER_FIELDS = ("id", "name", "area_id")


def load_conf(db, default_group: str = "") -> dict:
    """Configurazione dell'app dal database, in memoria (vedi in cima). default_group: il gruppo delle opzioni
    dell'add-on, che vale se nell'app non è scelto (AccessService.default_group). Niente password."""
    controllers = db.list_controllers()
    rules = db.all_category_rules()
    return {
        "persons": db.list_persons(),
        "categories": [{**c, "rules": rules.get(c["id"], [])} for c in db.list_categories()],
        "schedule_rules": db.schedule_rules(),
        "holidays": db.list_holidays(),
        "controllers": [{k: c[k] for k in CONTROLLER_FIELDS} for c in controllers],
        "doors": db.list_doors(),
        "sites": db.list_sites(),
        "inputs": {c["id"]: db.controller_inputs(c["id"]) for c in controllers},
        "default_group": db.get_setting("default_group") or default_group,
        "default_category": int(db.get_setting("default_category") or 0),
    }


def signature(kind: str, payload: dict) -> str:
    """Firma di un oggetto: sha256 del JSON canonico della sua forma normalizzata (compare.normalize). Un oggetto
    letto si firma dopo compare.project con l'oggetto voluto."""
    return hashlib.sha256(canonical(normalize(kind, payload))).hexdigest()


# Tessere e utenti


def _user_token(person: dict) -> str:
    """Utente Axis della persona; vuoto per le persone lette da una tessera senza utente («credenziale:...»)."""
    token = person.get("axis_user_token") or ""
    return "" if token.startswith("credenziale:") else token


def user_payload(person: dict) -> dict:
    """Utente Axis di una persona: esiste sul controller solo se la persona ha almeno una tessera."""
    return {"token": _user_token(person), "Name": person["name"], "Description": USER_DESCRIPTION, "Attribute": []}


def credential_payload(card: dict, person: dict, default_group: str = "") -> dict:
    """Tessera Axis completa come la scrive l'app (service.new_credential), con il suo token. Il profilo è quello
    della tessera nell'app (fino al passo 4 cards.access_profile), quello predefinito se manca."""
    access = {
        "access_profile": card.get("access_profile") or default_group,
        "valid_from": card.get("valid_from") or "",
        "valid_to": card.get("valid_to") or "",
    }
    payload = new_credential(_user_token(person), person["name"], card["number"], access, bool(card["enabled"]))
    payload["token"] = card["axis_credential_token"]
    return payload


# Tipologie: profili di accesso


def door_schedules(rules: list[dict], doors: list[dict], door_area: dict, area_site: dict) -> dict[str, list[str]]:
    """Porte coperte dalle righe di una tipologia, con le fasce di ognuna: area e struttura valgono per tutte le loro
    porte, anche quelle aggiunte dopo. doors: le porte del controller; door_area: id della porta → area; area_site:
    id dell'area → struttura."""
    result: dict[str, list[str]] = {}
    for door in doors:
        area = door_area.get(door["id"], 0)
        places = {("door", door["id"]), ("area", area), ("site", area_site.get(area, 0))}
        for rule in rules:
            if (rule["target"], rule["target_id"]) in places:
                schedules = result.setdefault(door["door_token"], [])
                if rule["schedule"] not in schedules:
                    schedules.append(rule["schedule"])
    return result


def places(conf: dict, controller_id: int) -> tuple[list[dict], dict[int, int], dict[int, int]]:
    """Porte del controller, area di ogni porta (la sua o quella del controller) e struttura di ogni area."""
    controllers = {c["id"]: c for c in conf.get("controllers") or []}
    doors = [d for d in conf.get("doors") or [] if d["controller_id"] == controller_id]
    door_area = {
        d["id"]: d.get("area_id") or (controllers.get(d["controller_id"]) or {}).get("area_id") or 0 for d in doors
    }
    area_site = {a["id"]: s["id"] for s in conf.get("sites") or [] for a in s.get("areas") or []}
    return doors, door_area, area_site


def profile_policies(schedules_by_door: dict[str, list[str]], points: list[dict], owned: set[str]) -> list[dict]:
    """Una regola per ogni punto di accesso con lettore delle porte coperte, con le fasce di quella porta. Pulsanti
    e relè (i punti degli ingressi in owned e quelli con il modo REXOnly) aprono senza tessera: niente regole."""
    policies = []
    for point in points:
        schedules = schedules_by_door.get(point.get("Entity"))
        if (
            not schedules
            or point.get("token") in owned
            or REX_AUTHENTICATION in (point.get("AuthenticationProfile") or [])
        ):
            continue
        policy = {"AccessPoint": point["token"], "AuthorizationProfile": [], "Attribute": []}
        policies.append({**policy, "Schedule": list(schedules)})
    return policies


def profile_payload(values: dict, policies: list[dict], base: dict | None = None) -> dict:
    """Profilo della tipologia (values: name e description) con le regole policies. base: il profilo da cui partire,
    con i campi che restano (token, validità); senza, un profilo nuovo senza token."""
    used = {t for p in policies for t in p["Schedule"]}
    profile = copy.deepcopy(base) if base else {}
    profile.setdefault("token", "")
    profile.update(
        {
            "Name": values["name"],
            "Description": values["description"] or f"Tipologia {values['name']} (Axis Access Manager)",
            # Senza nessuna regola non deve aprire niente
            "Enabled": bool(policies),
            "ValidFrom": profile.get("ValidFrom") or "",
            "ValidTo": profile.get("ValidTo") or "",
            "AuthenticationProfile": profile.get("AuthenticationProfile") or [],
            "Attribute": profile.get("Attribute") or [],
            # L'A1210 valuta anche la fascia del profilo: con fasce diverse per porta deve lasciar passare sempre
            "Schedule": [next(iter(used))] if len(used) == 1 else [ALWAYS],
            "AccessPolicy": policies,
        }
    )
    return profile


def rex_payload(token: str, wanted: dict[str, str]) -> dict:
    """Profilo dei pulsanti con una regola per ogni ingresso che apre (punto di accesso → fascia), senza regole
    conservate dal profilo sul controller."""
    return rex_profile(wanted, base={"token": token, "ValidFrom": "", "ValidTo": "", "Attribute": []})


# Fasce orarie


def schedule_payload(
    token: str, name: str, description: str, rules: list[dict], holidays: bool, holiday_list: list[dict]
) -> dict:
    """Fascia oraria con le regole dell'app; con holidays le eccezioni sono le festività holiday_list (quelle di
    adesso: cambiarle cambia la fascia)."""
    return {
        "token": token,
        "Name": name,
        "Description": description,
        "ScheduleDefinition": schedule_ical(rules),
        "ExceptionScheduleDefinition": holidays_ical(holiday_list) if holidays else None,
        "Attribute": [],
    }


def _schedule(schedule: dict, holiday_list: list[dict]) -> dict:
    ical = schedule.get("ical")
    if ical:
        # Definizione presa in carico che l'editor non sa leggere: si riscrive tale e quale
        return {
            "token": schedule["token"],
            "Name": schedule["name"],
            "Description": schedule.get("description") or "",
            "ScheduleDefinition": ical.get("definition") or "",
            "ExceptionScheduleDefinition": ical.get("exceptions") or None,
            "Attribute": [],
        }
    return schedule_payload(
        schedule["token"],
        schedule["name"],
        schedule.get("description") or "",
        schedule.get("rules") or [],
        bool(schedule.get("holidays")),
        holiday_list,
    )


# Porte, lettori, morsetti e punti di accesso


def door_within_limits(door: dict) -> tuple[dict, list[str]]:
    """La porta (oggetto Axis) con i tempi riportati entro i limiti della serratura (DOOR_TIMES), e un avviso per
    ogni tempo cambiato."""
    payload, warnings = copy.deepcopy(door), []
    for field, (_, label, low, high) in DOOR_TIMES.items():
        seconds = parse_duration(payload.get(field))
        if seconds is None or low <= seconds <= high:
            continue
        fixed = min(max(seconds, low), high)
        payload[field] = format_duration(fixed)
        warnings.append(
            f"{label}: {seconds:g} s è fuori dai limiti della serratura ({low}-{high} s), si scrive {fixed} s"
        )
    return payload, warnings


def door_payload(door: dict) -> dict:
    """Porta Axis dalla porta dell'app: nome unico, descrizione e i tempi che ha (in secondi nell'app)."""
    times = {field: format_duration(door[key]) for field, (key, *_) in DOOR_TIMES.items() if door.get(key) is not None}
    return {
        "token": door["door_token"],
        "Name": door["name"][:64],
        "Description": door.get("description") or "",
        **times,
    }


def door_config_payload(door: dict, inputs: list[dict]) -> dict:
    """Configurazione della porta: serratura e relè presi in carico (lock_config) più il Sensore Porta, che viene
    dagli ingressi. Mai una DoorScheduleConfiguration. Senza dispositivo: il controller ci mette il suo."""
    monitor = next(
        (r for r in inputs if r.get("role") == "door_monitor" and r.get("door_token") == door["door_token"]), None
    )
    values = {
        **(door.get("lock_config") or {}),
        **(
            {"DoorMonitor.Type": "Standard", **DOOR_MONITOR_VALUES[monitor.get("contact") or "no"]}
            if monitor
            else DOOR_MONITOR_OFF
        ),
    }
    return {
        "token": door["door_token"],
        "DeviceUUID": "",
        "Configuration": [{"Name": k, "Value": v} for k, v in values.items()],
    }


def reader_payload(reader: dict) -> dict:
    timeout = reader.get("timeout")
    return {
        "token": reader["token"],
        "Name": (reader.get("name") or "")[:64],
        "Description": reader.get("description") or "",
        **({"Timeout": format_duration(timeout)} if timeout is not None else {}),
    }


def _config(token: str, values: dict[str, str]) -> dict:
    return {"token": token, "DeviceUUID": "", "Configuration": [{"Name": k, "Value": v} for k, v in values.items()]}


def point_payload(point: dict) -> dict:
    """Punto di accesso dell'app: porta, lettori, modo di identificazione e direzione. Senza dispositivo."""
    direction = point.get("direction") or ""
    return {
        "token": point["token"],
        "Name": (point.get("name") or "")[:64],
        "Description": point.get("description") or "",
        "Entity": point.get("door") or "",
        "EntityType": point.get("entity_type") or "axtdc:Door",
        "Enabled": point.get("enabled") is not False,
        "IdPointDevice": [{"IdPoint": r, "DeviceUUID": ""} for r in point.get("readers") or []],
        "AuthenticationProfile": list(point.get("auth_profile") or []),
        "Attribute": [
            *(point.get("attributes") or []),
            *([{"Name": "Direction", "Value": direction}] if direction else []),
        ],
        "DoorDeviceUUID": "",
    }


def input_payloads(io: str, row: dict) -> dict[str, dict[str, dict]]:
    """Oggetti Axis di un ingresso dell'app, per tipo: punto di identificazione, configurazione, morsetto e punto di
    accesso di un pulsante o di un relè; il morsetto del Sensore Porta (la configurazione della porta la mette
    door_config_payload). Costruiti solo dall'app, con i token salvati nell'ingresso."""
    found: dict[str, dict[str, dict]] = {}
    role = row.get("role") or ""
    values = {**row, "supervised": bool(row.get("supervised")), "contact": row.get("contact") or "no"}
    if role in REX_ROLES and row.get("idpoint_token"):
        idpoint = row["idpoint_token"]
        found["lettore"] = {idpoint: rex_idpoint(io, values, idpoint)}
        found["config_lettore"] = {idpoint: _config(idpoint, rex_configuration(values))}
        mode, users = rex_assignment(io, values, idpoint)
        found["morsetto"] = {io: {"IoName": io, "IoMode": mode, "IoUser": users}}
        if row.get("access_point_token"):
            point = {"token": row["access_point_token"], **rex_point_fields(io, values, idpoint), "DoorDeviceUUID": ""}
            found["punto"] = {row["access_point_token"]: point}
    elif role == "door_monitor" and row.get("door_token"):
        users = door_monitor_users(row["door_token"])
        found["morsetto"] = {io: {"IoName": io, "IoMode": _plain_mode(io), "IoUser": users}}
    return found


def device_payload(device: dict) -> dict[str, dict]:
    """Impostazioni del dispositivo prese in carico: fuso, NTP, broker MQTT (mai la password) ed eventi MQTT."""
    found = {}
    for key, field in (("fuso", "time_zone"), ("ntp", "ntp"), ("mqtt", "mqtt"), ("mqtt_eventi", "mqtt_events")):
        if field in device:
            value = copy.deepcopy(device[field])
            if field == "mqtt" and isinstance(value, dict):
                value.pop("password", None)
                for part in ("server", "ssl"):
                    if isinstance(value.get(part), dict):
                        value[part] = {k: v for k, v in value[part].items() if k != "password"}
            found[key] = {"value": value}
    return found


# Stato voluto completo


def _controller(conf: dict, controller_id: int) -> dict:
    return next((c for c in conf.get("controllers") or [] if c["id"] == controller_id), {})


def _programming(conf: dict, controller_id: int) -> dict[str, dict[str, dict]]:
    """Porte, configurazioni, lettori, morsetti, punti, gestiti, fasce, profili e dispositivo voluti, dalla
    configurazione presa in carico."""
    controller = _controller(conf, controller_id)
    inputs = conf.get("inputs", {}).get(controller_id) or {}
    rows = list(inputs.values())
    objects: dict[str, dict[str, dict]] = {kind: {} for kind in PROGRAMMING}
    objects["dispositivo"] = device_payload(controller.get("device") or {})
    holiday_list = conf.get("holidays") or []
    for schedule in conf.get("schedules") or []:
        if not schedule.get("factory"):
            objects["fascia"][schedule["token"]] = _schedule(schedule, holiday_list)
    for door in conf.get("doors") or []:
        if door["controller_id"] != controller_id or "lock_config" not in door:
            continue
        objects["porta"][door["door_token"]] = door_payload(door)
        if door.get("lock_config") is not None:
            objects["config_porta"][door["door_token"]] = door_config_payload(door, rows)
    buses: dict[str, dict] = {}
    for reader in conf.get("readers") or []:
        if reader["controller_id"] != controller_id:
            continue
        objects["lettore"][reader["token"]] = reader_payload(reader)
        if reader.get("config") is not None:
            objects["config_lettore"][reader["token"]] = _config(reader["token"], reader["config"])
        if reader.get("bus"):
            bus = buses.setdefault(
                reader["bus"], {"IoName": reader["bus"], "IoMode": reader.get("io_mode") or "", "IoUser": []}
            )
            user = {"Type": "idpoint", "token": reader["token"], "Usage": reader.get("io_usage") or ""}
            bus["IoUser"].append({**user, **(reader.get("io_extra") or {})})
    objects["morsetto"].update(buses)
    for name, assignment in (controller.get("io") or {}).items():
        objects["morsetto"][name] = {"IoName": name, **copy.deepcopy(assignment)}
    for point in conf.get("access_points") or []:
        if point["controller_id"] == controller_id:
            objects["punto"][point["token"]] = point_payload(point)
    for io, row in inputs.items():
        for kind, items in input_payloads(io, row).items():
            objects[kind].update(items)
    objects["gestiti"] = {"gestiti": {"AccessPoint": sorted(objects["punto"])}}
    if controller.get("rex_profile") and (wanted := rex_policies(rows)):
        objects["pulsanti"][controller["rex_profile"]] = rex_payload(controller["rex_profile"], wanted)
    owned = {r["access_point_token"] for r in rows if r.get("access_point_token")}
    points = list(objects["punto"].values())
    layout = places(conf, controller_id)
    for category in conf.get("categories") or []:
        token = category.get("group_token") or ""
        if not token or token in objects["tipologia"]:
            continue
        policies = profile_policies(door_schedules(category.get("rules") or [], *layout), points, owned)
        objects["tipologia"][token] = profile_payload(category, policies, {"token": token})
    return objects


def build(conf: dict, controller_id: int, kinds=None) -> dict:
    """Stato voluto per un controller: {"kinds": tipi calcolati, "objects": {tipo: {chiave: payload}}, "pending":
    tipi da prendere in carico, "known": {"utente": token di tutte le persone}}. kinds: i tipi da calcolare
    (compare.KINDS), None tutti. Senza la presa in carico del controller i tipi della programmazione sono pending."""
    kinds = [k for k in KINDS if kinds is None or k in kinds]
    objects: dict[str, dict[str, dict]] = {}
    pending = []
    taken = bool(_controller(conf, controller_id).get("presa"))
    if taken and PROGRAMMING & set(kinds):
        objects.update({k: v for k, v in _programming(conf, controller_id).items() if k in kinds})
    elif not taken:
        pending = [k for k in kinds if k in PROGRAMMING]
    default_group = conf.get("default_group") or ""
    persons = conf.get("persons") or []
    if "utente" in kinds:
        objects["utente"] = {_user_token(p): user_payload(p) for p in persons if p.get("cards") and _user_token(p)}
    if "tessera" in kinds:
        objects["tessera"] = {
            card["axis_credential_token"]: credential_payload(card, person, default_group)
            for person in persons
            for card in person.get("cards") or []
            if card.get("axis_credential_token")
        }
    known = sorted({_user_token(p) for p in persons if _user_token(p)})
    return {"kinds": kinds, "objects": objects, "pending": pending, "known": {"utente": known}}
