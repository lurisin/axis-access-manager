"""Presa in carico: la programmazione che oggi sta solo sul controller diventa configurazione dell'app.

presa_in_carico(conf, letto, controller_id) è una funzione pura: parte dalla configurazione dell'app
(desired.load_conf) e da una lettura completa del controller (compare.read_strict) e calcola in memoria la
configurazione che l'app avrebbe dopo la presa in carico; desired.build ne fa lo stato voluto. Nella 0.28 non si salva
mai: serve al confronto in sola lettura (confronto). Dal passo 4 si fa una volta sola, con anteprima e conferma.

Cosa prende, senza cambiare chi entra dove:
- impostazioni del dispositivo (fuso, NTP, MQTT senza password, eventi MQTT);
- porte, con i tempi riportati nei limiti della serratura e la configurazione (lock_config) senza DoorMonitor.*
  (vengono dagli ingressi) e senza DoorSchedule* (sblocchi programmati: si tolgono); le porte del controller che l'app
  non mostrava diventano porte dell'app; il nome della porta è quello del controller;
- lettori (tutti i punti di identificazione che non sono di un ingresso), con configurazione e bus;
- punti di accesso che non sono di un ingresso, anche quelli dei pulsanti che non si riconoscono (punti opachi);
- pulsanti e Sensore Porta fatti a mano come ingressi dell'app, sugli ingressi senza ruolo nell'app;
- morsetti che non sono né bus dei lettori né ingressi, così come sono;
- profilo dei pulsanti (quello dell'app, riconosciuto come in programming);
- gruppi delle versioni vecchie (adopt_groups), Accesso Personalizzato (la persona passa alla tipologia che usa già il
  profilo delle sue tessere: nessuna tessera cambia accesso) e profili esterni usati dalle tessere, che diventano
  tipologie con lo stesso token (le righe dalle regole del profilo, rules_of); profili condivisi da più tipologie
  separati (la prima tiene il token); righe per le porte che i profili tenevano fuori dall'app;
- fasce: nomi e descrizioni, Sempre come riga di fabbrica, le altre standard_* in uso (o modificate nell'app)
  convertite in fasce dell'app con la stessa definizione e un token nuovo, le esterne in uso con lo stesso token.

I token nuovi (fasce convertite, profili separati, profilo dei pulsanti che manca) sono provvisori,
aam_<tipo>_<n>_<xxxx> calcolati dal token di partenza: il contatore nel database arriva con il passo 3.

Note: una per ogni oggetto preso, convertito o da guardare, {"kind", "key", "label", "text", "surprise", "warnings"};
compare.compare le usa per le righe «prende» e per le sorprese.
"""

from __future__ import annotations

import copy
import hashlib
import string

from .compare import DEVICE, LISTS, compare, exceptions_meaning
from .desired import build, door_within_limits
from .hardware import ALLOWED_ROLES, ALWAYS, CONTROLLER_INPUTS, FACTORY_SCHEDULES, READER_BUSES, is_factory
from .programming import (
    DOOR_TIMES,
    INPUT_ROLES,
    REX_AUTHENTICATION,
    REX_ROLES,
    _attribute,
    _own_rex_profile,
    door_monitor_of,
    is_rex_profile,
    parse_duration,
)
from .rules import holidays_ical, parse_schedule_ical
from .service import normalize_name, normalize_text

COLORS = ["blue", "green", "orange", "red", "purple", "teal", "pink", "gray"]
# Elenchi senza i quali la presa in carico non si fa: un elenco non letto non è vuoto
PRESA_LISTS = (
    "doors",
    "door_configurations",
    "idpoints",
    "idpoint_configurations",
    "io_assignments",
    "access_points",
    "access_profiles",
    "schedules",
)
# Valori di serie di una porta nuova dell'app (come le colonne della tabella doors)
DOOR_DEFAULTS = {
    "snapshot_camera": "",
    "snapshot_on_granted": False,
    "ha_buttons": True,
    "intercom_name": "",
    "intercom_open_door": False,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
    "area_id": 0,
}
SURPRISE_REX = "pulsante fatto a mano sul controller"
SURPRISE_MONITOR = "Sensore Porta fatto a mano sul controller"
SURPRISE_EXTERNAL = "profilo esterno usato da tessere dell'app"
SURPRISE_SHARED = "profilo in comune tra più tipologie"
SURPRISE_TIMES = "tempi della porta oltre i limiti della serratura"
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _nocase(text: str) -> str:
    """Come COLLATE NOCASE di SQLite: maiuscole e minuscole uguali, solo per le lettere ASCII."""
    return text.translate(_ASCII_LOWER)


def _new_id(rows: list[dict]) -> int:
    """Numero provvisorio di una riga nuova, solo in memoria: negativo, -1, -2... nell'ordine in cui nasce."""
    return min([r["id"] for r in rows if isinstance(r.get("id"), int)] + [0]) - 1


def _token(kind: str, n: int, seed: str) -> str:
    """Token provvisorio aam_<tipo>_<n>_<xxxx>, sempre uguale per lo stesso oggetto di partenza."""
    return f"aam_{kind}_{n}_{hashlib.sha256(seed.encode()).hexdigest()[:4]}"


def _note(kind: str, key: str, label: str, text: str, surprise: str = "", warnings=()) -> dict:
    return {"kind": kind, "key": key, "label": label, "text": text, "surprise": surprise, "warnings": list(warnings)}


# Gruppi delle versioni vecchie e profili esterni → tipologie


def rules_of(profile: dict, points: dict[str, str], doors: dict[str, int]) -> list[dict]:
    """Righe di accesso lette da un profilo esistente: una per porta dell'app, con la sua fascia.
    points: punto di accesso → porta (token); doors: token della porta dell'app → id."""
    rules: list[dict] = []
    for policy in profile.get("AccessPolicy") or []:
        door = doors.get(points.get(policy.get("AccessPoint"), ""))
        schedule = next(iter(policy.get("Schedule") or profile.get("Schedule") or []), "")
        rule = {"target": "door", "target_id": door, "schedule": schedule}
        if door and schedule and rule not in rules:
            rules.append(rule)
    return rules


def category_for(token: str, profile: dict, categories: list[dict]) -> dict:
    """Tipologia che usa il profilo token: quella nata da quel gruppo, se c'è (con il suo id), altrimenti i campi di
    una tipologia nuova con il nome del gruppo (senza id). categories: le tipologie di adesso."""
    name = normalize_name(profile.get("Name") or token)[:40]
    # Si cerca la tipologia nata da quel gruppo: un'altra con lo stesso profilo dice chi è un'altra persona
    names = {name.lower(), f"{name[:33]} (gruppo)".lower()}
    found = next((c for c in categories if c["group_token"] == token and c["name"].lower() in names), None)
    if found:
        return found
    if any(_nocase(c["name"]) == _nocase(name) for c in categories):
        name = f"{name[:33]} (gruppo)"
    return {
        "name": name,
        "color": COLORS[len(categories) % len(COLORS)],
        "group_token": token,
        "valid_days": 0,
        "description": normalize_text(profile.get("Description"), 200, "descrizione"),
    }


def _category(conf: dict, token: str, profiles: dict, points: dict, doors: dict) -> dict:
    """Tipologia che usa il profilo, creata in memoria (numero provvisorio) se non c'è, con le righe dal profilo."""
    profile = profiles.get(token) or {}
    categories = conf["categories"]
    category = category_for(token, profile, categories)
    if "id" in category:
        return category
    category = {**category, "id": _new_id(categories), "rules": rules_of(profile, points, doors) if profile else []}
    categories.append(category)
    # Nell'ordine del database (per nome, come COLLATE NOCASE): chi cerca dopo trova le tipologie come le troverebbe lì
    categories.sort(key=lambda c: _nocase(c["name"]))
    return category


def adopt_groups(conf: dict, profiles: dict[str, dict], points: dict[str, str], doors: dict[str, int]):
    """Dalle versioni con gruppi e tipologie separati, senza cambiare chi entra dove:
    - una tipologia senza gruppo prende quello predefinito, e le righe di accesso dal suo profilo;
    - chi non ha tipologia entra in quella del suo gruppo, creata con lo stesso nome se manca;
    - il gruppo predefinito diventa la tipologia predefinita.
    conf: categories (con rules, nell'ordine per nome), persons (con cards), default_group, default_category;
    profiles: i profili del controller per token; points: punto di accesso → porta; doors: porta dell'app → id.
    Restituisce (conf cambiata, quante modifiche). Le tipologie nuove hanno un numero provvisorio negativo."""
    conf = copy.deepcopy(conf)
    default = conf.get("default_group") or ""
    fixed = 0
    for category in conf["categories"]:
        if not category["group_token"] and default:
            category["group_token"] = default
            fixed += 1
    for category in conf["categories"]:
        if not category["rules"] and category["group_token"] in profiles:
            # Profilo solo su porte che l'app non gestisce: nessuna riga, e non è una modifica
            if adopted := rules_of(profiles[category["group_token"]], points, doors):
                category["rules"] = adopted
                fixed += 1
    for person in conf["persons"]:
        if person["category_id"]:
            continue
        token = person["cards"][0]["access_profile"] if person["cards"] else default
        if not token:
            continue
        person["category_id"] = _category(conf, token, profiles, points, doors)["id"]
        fixed += 1
    if not conf.get("default_category") and default:
        conf["default_category"] = _category(conf, default, profiles, points, doors)["id"]
        fixed += 1
    return conf, fixed


# Ingressi fatti a mano


def rex_input_of(
    io: str, assigned: list[dict], points: list[dict], configs: list[dict], profiles: list[dict]
) -> dict | None:
    """Pulsante o relè impostato sul controller senza l'app: l'ingresso ha un punto di identificazione REX e un
    punto di accesso REXOnly con quel punto. Restituisce le impostazioni dell'ingresso, None se non si riconosce."""
    if "rex" not in ALLOWED_ROLES.get(io, ()):
        return None
    users = next((a.get("IoUser") or [] for a in assigned if a.get("IoName") == io), [])
    user = next((u for u in users if u.get("Type") == "idpoint" and u.get("Usage") in ("REX", "REXSupervised")), None)
    if user is None or not user.get("token"):
        return None
    idpoint = user["token"]
    point = next(
        (
            p
            for p in points
            if REX_AUTHENTICATION in (p.get("AuthenticationProfile") or [])
            and any(d.get("IdPoint") == idpoint for d in p.get("IdPointDevice") or [])
            and p.get("token")
            and p.get("Entity")
        ),
        None,
    )
    if point is None:
        return None
    config = next((c for c in configs if c.get("token") == idpoint), {})
    values = {str(c.get("Name")): str(c.get("Value")) for c in config.get("Configuration") or []}
    schedule = next(
        (
            next(iter(policy.get("Schedule") or []), "")
            for profile in profiles
            if is_rex_profile(profile)
            for policy in profile.get("AccessPolicy") or []
            if policy.get("AccessPoint") == point["token"]
        ),
        "",
    )
    return {
        "name": (point.get("Name") or INPUT_ROLES["rex"])[:64],
        "role": "rex",
        "door_token": point["Entity"],
        "contact": "nc" if values.get("IdPoint.REX.Type", "").endswith("High") else "no",
        "supervised": user.get("Usage") == "REXSupervised",
        "schedule": "" if schedule == ALWAYS else schedule,
        "idpoint_token": idpoint,
        "access_point_token": point["token"],
    }


# Presa in carico


def presa_in_carico(conf: dict, letto: dict, controller_id: int) -> tuple[dict, list[dict]]:
    """Configurazione dell'app dopo la presa in carico del controller (vedi in cima), calcolata in memoria e mai
    salvata, e le note. Senza uno degli elenchi di PRESA_LISTS non prende niente: la configurazione resta com'è e
    una nota dice cosa manca."""
    conf = copy.deepcopy(conf)
    notes: list[dict] = []
    lists = letto.get("lists") or {}
    controller = next((c for c in conf.get("controllers") or [] if c["id"] == controller_id), None)
    missing = [LISTS[key][0] for key in PRESA_LISTS if key not in lists]
    if controller is None or missing:
        reason = f"elenchi non letti: {', '.join(missing)}" if controller is not None else "controller non trovato"
        notes.append(_note("presa", "", "Presa in Carico", f"Non si fa: {reason}"))
        return conf, notes
    controller["presa"] = True
    for key in ("doors", "readers", "access_points", "schedules"):
        conf.setdefault(key, [])
    inputs = conf.setdefault("inputs", {}).setdefault(controller_id, {})
    _take_device(controller, lists, notes)
    before = {d["door_token"] for d in conf["doors"] if d["controller_id"] == controller_id}
    _take_doors(conf, controller_id, lists, notes)
    _take_inputs(inputs, controller_id, lists, notes)
    _take_readers_and_points(conf, controller, inputs, lists, notes)
    _take_categories(conf, controller_id, before, lists, notes)
    _take_rex_profile(controller, inputs, lists, notes)
    _take_schedules(conf, inputs, lists, notes)
    return conf, notes


def _by_token(items, field: str = "token") -> dict[str, dict]:
    return {str(i[field]): i for i in items or [] if isinstance(i, dict) and i.get(field)}


def _take_device(controller: dict, lists: dict, notes: list[dict]) -> None:
    device = lists.get(DEVICE) or {}
    controller["device"] = {
        field: copy.deepcopy(device[field]) for field in ("time_zone", "ntp", "mqtt", "mqtt_events") if field in device
    }
    for key, field, label in (
        ("fuso", "time_zone", "Fuso Orario"),
        ("ntp", "ntp", "Server NTP"),
        ("mqtt", "mqtt", "Collegamento MQTT"),
        ("mqtt_eventi", "mqtt_events", "Eventi MQTT"),
    ):
        if field in device:
            notes.append(_note("dispositivo", key, label, "Preso dal controller"))


def _take_doors(conf: dict, controller_id: int, lists: dict, notes: list[dict]) -> None:
    app_doors = {d["door_token"]: d for d in conf["doors"] if d["controller_id"] == controller_id}
    configs = _by_token(lists["door_configurations"])
    for door in lists["doors"]:
        token = str(door.get("token") or "")
        if not token:
            continue
        fixed, warnings = door_within_limits(door)
        surprise = SURPRISE_TIMES if warnings else ""
        row = app_doors.get(token)
        name = str(door.get("Name") or "") or token
        if row is None:
            row = {
                "id": _new_id(conf["doors"]),
                "controller_id": controller_id,
                "door_token": token,
                "name": name,
                **DOOR_DEFAULTS,
            }
            conf["doors"].append(row)
            app_doors[token] = row
            text = "Porta del controller che l'app non mostrava: diventa una porta dell'app"
        else:
            text = "Presa dal controller"
            if row["name"] != name:
                warnings.insert(0, f"nell'app si chiamava {row['name']}: prende il nome del controller")
            row["name"] = name
        row["description"] = door.get("Description") or ""
        for field, (key, *_) in DOOR_TIMES.items():
            row[key] = parse_duration(fixed.get(field))
        config = configs.get(token)
        row["lock_config"] = (
            None
            if config is None
            else {
                str(c.get("Name")): str(c.get("Value"))
                for c in config.get("Configuration") or []
                if isinstance(c, dict) and not str(c.get("Name")).startswith(("DoorMonitor.", "DoorSchedule"))
            }
        )
        notes.append(_note("porta", token, f"Porta {name}", text, surprise, warnings))
        if config is not None:
            notes.append(_note("config_porta", token, f"Configurazione della Porta {name}", "Presa dal controller"))


def _take_inputs(inputs: dict, controller_id: int, lists: dict, notes: list[dict]) -> None:
    """Sensore Porta e pulsanti messi a mano sul controller, sugli ingressi che nell'app non hanno un ruolo."""
    assigned = lists["io_assignments"]
    for io in CONTROLLER_INPUTS:
        row = inputs.get(io) or {}
        if row.get("role"):
            continue
        found = door_monitor_of(io, assigned, lists["door_configurations"])
        surprise = SURPRISE_MONITOR
        if found is None:
            found = rex_input_of(
                io, assigned, lists["access_points"], lists["idpoint_configurations"], lists["access_profiles"]
            )
            surprise = SURPRISE_REX
        if found is None:
            continue
        values = {
            "controller_id": controller_id,
            "io": io,
            "name": INPUT_ROLES[found["role"]],
            "contact": "no",
            "supervised": False,
            "schedule": "",
            "idpoint_token": "",
            "access_point_token": "",
            **found,
        }
        inputs[io] = values
        label = f"{CONTROLLER_INPUTS[io][0]}: {INPUT_ROLES[values['role']]}"
        text = f"Diventa l'ingresso {values['name']} dell'app"
        notes.append(_note("morsetto", io, label, text, surprise))
        for kind, key in (
            ("lettore", values["idpoint_token"]),
            ("config_lettore", values["idpoint_token"]),
            ("punto", values["access_point_token"]),
        ):
            if key:
                notes.append(_note(kind, key, label, text, surprise))


def _take_readers_and_points(conf: dict, controller: dict, inputs: dict, lists: dict, notes: list[dict]) -> None:
    controller_id = controller["id"]
    configs = _by_token(lists["idpoint_configurations"])
    rex_rows = [r for r in inputs.values() if r.get("role") in REX_ROLES]
    owned_idpoints = {r["idpoint_token"] for r in rex_rows if r.get("idpoint_token")}
    owned_points = {r["access_point_token"] for r in rex_rows if r.get("access_point_token")}
    users: dict[str, tuple[dict, dict]] = {}
    for assignment in lists["io_assignments"]:
        for user in assignment.get("IoUser") or []:
            if user.get("Type") == "idpoint" and user.get("token"):
                users[str(user["token"])] = (assignment, user)
    readers: dict[str, dict] = {}
    for idpoint in lists["idpoints"]:
        token = str(idpoint.get("token") or "")
        if not token or token in owned_idpoints:
            continue
        assignment, user = users.get(token, ({}, {}))
        bus = assignment.get("IoName") if assignment.get("IoName") in READER_BUSES else ""
        config = configs.get(token)
        reader = {
            "id": _new_id(conf["readers"]),
            "controller_id": controller_id,
            "token": token,
            "name": idpoint.get("Name") or "",
            "description": idpoint.get("Description") or "",
            "timeout": parse_duration(idpoint.get("Timeout")),
            "config": None
            if config is None
            else {str(c.get("Name")): str(c.get("Value")) for c in config.get("Configuration") or []},
            "bus": bus,
            "io_mode": (assignment.get("IoMode") or "") if bus else "",
            "io_usage": (user.get("Usage") or "") if bus else "",
            "io_extra": {k: v for k, v in user.items() if k not in ("Type", "token", "Usage")} if bus else {},
        }
        conf["readers"].append(reader)
        readers[token] = reader
        label = f"Lettore {reader['name'] or token}"
        notes.append(_note("lettore", token, label, "Preso dal controller"))
        if config is not None:
            notes.append(_note("config_lettore", token, f"Configurazione del {label}", "Presa dal controller"))
    covered = {r["io"] for r in inputs.values() if r.get("role") in (*REX_ROLES, "door_monitor")}
    used = {io for io, row in inputs.items() if row.get("role")}
    kept: dict[str, dict] = {}
    for assignment in lists["io_assignments"]:
        name = str(assignment.get("IoName") or "")
        users_here = assignment.get("IoUser") or []
        if not name or not users_here or name in covered:
            continue
        if name in READER_BUSES and all(str(u.get("token")) in readers for u in users_here):
            notes.append(_note("morsetto", name, f"Morsetto {READER_BUSES[name]}", "Preso dal controller"))
            continue
        if name in used:
            # Ingresso con un ruolo nell'app che sul controller è usato da altro: si libera (vedi input_problem)
            continue
        for reader in readers.values():
            if reader["bus"] == name:
                reader.update(bus="", io_mode="", io_usage="", io_extra={})
        kept[name] = {"IoMode": assignment.get("IoMode") or "", "IoUser": copy.deepcopy(users_here)}
        notes.append(_note("morsetto", name, f"Morsetto {name}", "Preso così com'è dal controller"))
    controller["io"] = kept
    for point in lists["access_points"]:
        token = str(point.get("token") or "")
        if not token or token in owned_points:
            continue
        conf["access_points"].append(
            {
                "id": _new_id(conf["access_points"]),
                "controller_id": controller_id,
                "token": token,
                "name": point.get("Name") or "",
                "description": point.get("Description") or "",
                "door": point.get("Entity") or "",
                "entity_type": point.get("EntityType") or "",
                "enabled": point.get("Enabled") is not False,
                "auth_profile": list(point.get("AuthenticationProfile") or []),
                "direction": _attribute(point, "Direction"),
                "attributes": [a for a in point.get("Attribute") or [] if a.get("Name") != "Direction"],
                "readers": [str(d["IdPoint"]) for d in point.get("IdPointDevice") or [] if d.get("IdPoint")],
            }
        )
        rex = REX_AUTHENTICATION in (point.get("AuthenticationProfile") or [])
        text = "Pulsante che non si riconosce: preso come punto di accesso" if rex else "Preso dal controller"
        notes.append(_note("punto", token, f"Punto di Accesso {point.get('Name') or token}", text))


def _take_categories(conf: dict, controller_id: int, before: set[str], lists: dict, notes: list[dict]) -> None:
    profiles = _by_token(lists["access_profiles"])
    points = {str(p.get("token")): p.get("Entity") for p in lists["access_points"] if p.get("token")}
    doors = {d["door_token"]: d["id"] for d in conf["doors"] if d["controller_id"] == controller_id}
    known = {c["id"] for c in conf["categories"]}
    fields = ("categories", "persons", "default_group", "default_category")
    adopted, fixed = adopt_groups({key: conf.get(key) for key in fields}, profiles, points, doors)
    conf.update({key: adopted[key] for key in ("categories", "persons", "default_category")})
    if fixed:
        notes.append(_note("presa", "", "Gruppi", f"Gruppi delle versioni precedenti passati alle tipologie: {fixed}"))
    default = conf.get("default_group") or ""
    # Accesso Personalizzato: la persona passa alla tipologia che usa già il profilo delle sue tessere
    for person in conf["persons"]:
        cards = person.get("cards") or []
        if not cards:
            continue
        token = cards[0].get("access_profile") or default
        mine = next((c for c in conf["categories"] if c["id"] == person["category_id"]), None)
        if not token or (mine is not None and mine["group_token"] == token):
            target = mine
        else:
            target = next((c for c in conf["categories"] if c["group_token"] == token), None) or _category(
                conf, token, profiles, points, doors
            )
            person["category_id"] = target["id"]
            notes.append(
                _note(
                    "persona",
                    str(person["id"]),
                    person["name"],
                    f"Accesso Personalizzato: passa alla tipologia {target['name']}, che usa già il suo profilo",
                )
            )
        for card in cards:
            if target is not None and (card.get("access_profile") or default) != target["group_token"]:
                notes.append(
                    _note(
                        "tessera",
                        card.get("axis_credential_token") or "",
                        f"Tessera {card['number']} ({person['name']})",
                        f"Passa al profilo della tipologia {target['name']}, come le altre tessere della persona",
                        warnings=["cambia chi entra dove: questa tessera aveva un altro profilo"],
                    )
                )
                card["access_profile"] = target["group_token"]
    for category in conf["categories"]:
        if category["id"] not in known and category["group_token"] in profiles:
            notes.append(
                _note(
                    "tipologia",
                    category["group_token"],
                    f"Tipologia {category['name']}",
                    "Nuova tipologia con lo stesso profilo: nessuna tessera cambia accesso",
                    SURPRISE_EXTERNAL,
                )
            )
    # Profili condivisi: la prima tipologia (numero più basso) tiene il token, le altre ne ricevono uno nuovo
    seen: dict[str, dict] = {}
    split = 0
    for category in sorted(conf["categories"], key=lambda c: (c["id"] < 0, abs(c["id"]))):
        token = category["group_token"]
        if not token:
            continue
        if token not in seen:
            seen[token] = category
            continue
        split += 1
        category["group_token"] = _token("tipologia", split, f"{token}:{category['id']}")
        for person in conf["persons"]:
            if person["category_id"] == category["id"]:
                for card in person.get("cards") or []:
                    card["access_profile"] = category["group_token"]
        notes.append(
            _note(
                "tipologia",
                category["group_token"],
                f"Tipologia {category['name']}",
                f"Profilo separato da quello della tipologia {seen[token]['name']}: nasce il suo",
                SURPRISE_SHARED,
            )
        )
    # Porte che i profili tenevano fuori dall'app: ora sono dell'app, con le righe ricavate dal profilo
    by_id = {d["id"]: d["door_token"] for d in conf["doors"]}
    for category in conf["categories"]:
        profile = profiles.get(category["group_token"])
        if profile is None:
            continue
        extra = [
            rule
            for rule in rules_of(profile, points, doors)
            if by_id.get(rule["target_id"]) not in before and rule not in category["rules"]
        ]
        if extra:
            category["rules"] = [*category["rules"], *extra]
            notes.append(
                _note(
                    "tipologia",
                    category["group_token"],
                    f"Tipologia {category['name']}",
                    "Righe aggiunte per le porte che il profilo apriva fuori dall'app",
                )
            )


def _take_rex_profile(controller: dict, inputs: dict, lists: dict, notes: list[dict]) -> None:
    own = _own_rex_profile([p for p in lists["access_profiles"] if isinstance(p, dict)])
    wanted = [r for r in inputs.values() if r.get("role") in REX_ROLES and r.get("access_point_token")]
    if own is not None and own.get("token"):
        controller["rex_profile"] = own["token"]
        points = {r["access_point_token"] for r in wanted}
        # Regole che l'app conservava per pulsanti che non conosce: nello stato voluto non ci sono più
        lost = [p.get("AccessPoint") for p in own.get("AccessPolicy") or [] if p.get("AccessPoint") not in points]
        warnings = [f"il punto di accesso {token} perde il permesso di aprire senza tessera" for token in lost]
        notes.append(_note("pulsanti", own["token"], "Profilo dei Pulsanti", "Preso dal controller", warnings=warnings))
    elif wanted:
        controller["rex_profile"] = _token("pulsanti", 1, str(controller["id"]))
        text = "Sul controller non c'è: nasce per i pulsanti e i relè degli ingressi"
        notes.append(_note("pulsanti", controller["rex_profile"], "Profilo dei Pulsanti", text))
    else:
        controller["rex_profile"] = ""


def _take_schedules(conf: dict, inputs: dict, lists: dict, notes: list[dict]) -> None:
    rules = conf.get("schedule_rules") or {}
    in_use = {r["schedule"] for c in conf["categories"] for r in c.get("rules") or []}
    in_use |= {r["schedule"] for r in inputs.values() if r.get("schedule")}
    holidays_now = exceptions_meaning(holidays_ical(conf.get("holidays") or []))
    read = _by_token(lists["schedules"])
    renamed: dict[str, str] = {}
    converted = 0
    for token, schedule in read.items():
        name, description = schedule.get("Name") or "", schedule.get("Description") or ""
        if token == ALWAYS:
            # Di fabbrica: l'app non la scrive mai, il confronto controlla solo che valga sempre
            entry = {"token": token, "name": FACTORY_SCHEDULES[ALWAYS], "description": description, "rules": []}
            conf["schedules"].append({**entry, "holidays": False, "ical": None, "factory": True})
            continue
        factory = is_factory("fascia", token)
        if token not in in_use and token not in rules:
            continue
        if token in rules:
            entry = {"rules": rules[token]["rules"], "holidays": bool(rules[token]["holidays"]), "ical": None}
        else:
            definition, exceptions = schedule.get("ScheduleDefinition"), schedule.get("ExceptionScheduleDefinition")
            parsed = parse_schedule_ical(definition)
            holidays = bool(exceptions) and exceptions_meaning(exceptions) == holidays_now
            if parsed is not None and (not exceptions or holidays):
                entry = {"rules": parsed, "holidays": holidays, "ical": None}
            else:
                # Definizione che l'editor non sa leggere: si riscrive tale e quale
                entry = {
                    "rules": [],
                    "holidays": False,
                    "ical": {"definition": definition or "", "exceptions": exceptions or ""},
                }
        if factory:
            converted += 1
            new = _token("fascia", converted, token)
            renamed[token] = new
            label = FACTORY_SCHEDULES.get(token) or name or token
            conf["schedules"].append(
                {"token": new, "name": label, "description": description, **entry, "factory": False}
            )
            notes.append(
                _note(
                    "fascia",
                    new,
                    f"Fascia Oraria {label}",
                    f"La fascia di fabbrica {token} in uso diventa una fascia dell'app con la stessa definizione",
                )
            )
            continue
        conf["schedules"].append(
            {"token": token, "name": name or token, "description": description, **entry, "factory": False}
        )
        text = "Presa dal controller" if token in rules else "Fascia esterna in uso: presa dal controller"
        notes.append(_note("fascia", token, f"Fascia Oraria {name or token}", text))
    for token, rule in rules.items():
        if token in read or token in renamed or token == ALWAYS:
            continue
        label = FACTORY_SCHEDULES.get(token) or token
        entry = {"rules": rule["rules"], "holidays": bool(rule["holidays"]), "ical": None, "factory": False}
        if is_factory("fascia", token):
            # Di fabbrica e modificata nell'app, ma sul controller non c'è: come le altre, una fascia dell'app nuova
            converted += 1
            renamed[token] = _token("fascia", converted, token)
        conf["schedules"].append({"token": renamed.get(token, token), "name": label, "description": "", **entry})
        text = "Sul controller non c'è: si ricrea"
        notes.append(_note("fascia", renamed.get(token, token), f"Fascia Oraria {label}", text))
    for category in conf["categories"]:
        category["rules"] = [{**r, "schedule": renamed.get(r["schedule"], r["schedule"])} for r in category["rules"]]
    for row in inputs.values():
        if row.get("schedule") in renamed:
            row["schedule"] = renamed[row["schedule"]]


def confronto(conf: dict, letto: dict, controller_id: int, kinds=None) -> dict:
    """Presa in carico, stato voluto e confronto in una volta, in memoria e in sola lettura: cosa l'app prenderebbe
    in carico («prende»), cosa riporterebbe com'è nell'app, cosa ricreerebbe e cosa toglierebbe. Come compare.compare,
    più "notes": le note della presa in carico. kinds: i tipi da confrontare (compare.KINDS), None tutti."""
    adopted, notes = presa_in_carico(conf, letto, controller_id)
    result = compare(build(adopted, controller_id, kinds), letto, notes)
    result["notes"] = notes
    return result
