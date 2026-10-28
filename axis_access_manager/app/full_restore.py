"""Ripristino completo di un controller resettato o sostituito.

Dal backup si ricrea la programmazione con i token di allora, così tessere, tipologie, eventi ed entità di
Home Assistant restano collegati: fasce orarie (con le regole dell'app e le festività di adesso), modi di
identificazione, porte con la loro configurazione, lettori, morsetti (ingressi e uscite), punti di accesso e
profili. Poi si riscrivono le tessere che il controller non ha più e, a parte, si rimettono fuso orario, NTP e MQTT.

Valgono le regole di sempre: tempi di apertura entro i limiti della serratura, mai uno sblocco permanente
(la programmazione oraria delle porte non si ripristina), nessuna password nei backup. Con un controller
sostituito il dispositivo (DeviceUUID) del vecchio diventa quello nuovo: le configurazioni si scrivono senza,
e il controller ci mette il suo.
"""

from __future__ import annotations

import copy
import json

from .backup import COMMON, FIELD_LABELS, READ_LABELS, context_of
from .programming import CONTROLLER_INPUTS, DOOR_TIMES, format_duration, link_access_points, parse_duration
from .rules import describe_rules, holidays_ical
from .service import ServiceError, plural
from .vapix import VapixError

FULL_SECTIONS = {
    "dispositivo": "Impostazioni del Controller",
    "controller": "Programmazione del Controller",
    "tessere": "Tessere sul Controller",
}
UUID_FIELDS = ("DeviceUUID", "DoorDeviceUUID")
# Le voci degli errori di lettura del backup (READ_LABELS) come titolo delle righe che non si possono ripristinare
READ_TITLES = {
    "schedules": "Fasce Orarie",
    "doors": "Porte",
    "door_configurations": "Configurazione delle Porte",
    "idpoints": "Lettori",
    "idpoint_configurations": "Configurazione dei Lettori",
    "io_assignments": "Ingressi e Uscite",
    "access_points": "Punti di Accesso",
    "access_profiles": "Profili di Accesso",
    "credentials": "Tessere",
}
NOT_READ = "dati non letti nel backup o adesso: restano come sono"
# Token rifiutati da un controller, con quello nuovo che ha dato (JSON vecchio → nuovo): i backup di prima hanno
# ancora i vecchi, e senza questa mappa un secondo ripristino ricreerebbe gli stessi oggetti una seconda volta
SETTING_TOKENS = "restore_tokens"
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


def renamed_tokens(db) -> dict[str, str]:
    """Token cambiati dai ripristini completi (vecchio → nuovo), da applicare ai backup prima dell'anteprima."""
    try:
        value = json.loads(db.get_setting(SETTING_TOKENS) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


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


def _differences(current: dict | None, saved: dict) -> list[dict]:
    """Campi diversi, com'è adesso → come tornerà; le voci di configurazione una per riga."""
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
        changes.append({"label": FIELD_NAMES.get(key, key), "old": _show(now.get(key)), "new": _show(then.get(key))})
    return changes


def _door_payload(door: dict) -> tuple[dict, list[str]]:
    """La porta del backup con i tempi riportati entro i limiti della serratura."""
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


def _hex(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch in "0123456789abcdef")


def _uuids(part: dict) -> set[str]:
    found = {c.get("DeviceUUID") for c in part.get("door_configurations", []) + part.get("idpoint_configurations", [])}
    for point in part.get("access_points", []):
        found.add(point.get("DoorDeviceUUID"))
        found |= {d.get("DeviceUUID") for d in point.get("IdPointDevice") or []}
    return {u for u in found if u}


def _devices(saved: dict, current: dict) -> tuple[list[str], bool, str]:
    """Dispositivi (DeviceUUID) del controller del backup, se è stato sostituito e, se si sa già, il nuovo.
    Sono suoi i DeviceUUID che finiscono con il suo numero di serie (sull'A1210 è così); se nessuno finisce così,
    quelli delle configurazioni di porte e lettori, che sull'A1210 sono sempre locali."""
    old_serial = _hex((saved.get("device") or {}).get("serial") or "")
    new_serial = _hex((current.get("device") or {}).get("serial") or "")
    found = _uuids(saved)
    local = {u for u in found if old_serial and _hex(u).endswith(old_serial)}
    if not local:
        configs = saved.get("door_configurations", []) + saved.get("idpoint_configurations", [])
        local = {c.get("DeviceUUID") for c in configs if c.get("DeviceUUID")}
    if not old_serial or not new_serial or old_serial == new_serial:
        return sorted(local), False, ""
    guess = next(iter(sorted(_uuids(current))), "")
    if not guess:
        # Stesso schema del vecchio con il numero di serie nuovo: si usa solo se il controller non dice il suo
        old = next((u for u in sorted(local) if _hex(u).endswith(old_serial)), "")
        if old and len(new_serial) == len(old_serial) == 12 and old.lower().endswith(old_serial):
            guess = old[: -len(old_serial)] + new_serial
    return sorted(local), True, guess


def _read_problem(part: dict, key: str) -> bool:
    label = READ_LABELS.get(key)
    return bool(label) and any(str(e).startswith(f"{label}:") for e in part.get("errors") or [])


def plan(
    saved_parts: dict[str, dict],
    current_parts: dict[str, dict],
    sections: list[str],
    controller_id: int,
    mqtt_users: set[str] | None = None,
) -> list[dict]:
    """Righe dell'anteprima del ripristino completo del controller in uso."""
    key = f"controller:{controller_id}"
    saved, current = saved_parts.get(key), current_parts.get(key)
    if saved is None:
        return [
            _row(
                "completo",
                sections[0],
                "riporta",
                "Ripristino Completo",
                "",
                blocked="questo backup non ha la configurazione del controller",
                _covers=list(sections),
            )
        ]
    if current is None:
        return [
            _row(
                "completo",
                sections[0],
                "riporta",
                "Ripristino Completo",
                "",
                blocked="il controller non risponde: si ripristina quando torna raggiungibile",
                # Le righe scelte nell'anteprima, sparite perché adesso non risponde, non sono "già come nel backup"
                _covers=list(sections),
            )
        ]
    items: list[dict] = []
    if "dispositivo" in sections:
        items += _plan_device(saved.get("device") or {}, current.get("device") or {}, mqtt_users or set())
    if "controller" in sections:
        items += _plan_programming(
            saved, current, context_of(saved_parts), saved_parts.get(COMMON) or {}, current_parts.get(COMMON) or {}
        )
    if "tessere" in sections:
        items += _plan_cards(current, current_parts.get(COMMON) or {})
    return items


# Impostazioni del dispositivo


def _plan_device(saved: dict, current: dict, mqtt_users: set[str]) -> list[dict]:
    items = []
    zone = saved.get("time_zone") or ""
    if zone and zone != current.get("time_zone"):
        item = _row("disp-fuso", "dispositivo", "riporta", "Fuso Orario", "Riporta com'era", _zone=zone)
        item["changes"] = [{"label": "Fuso Orario", "old": current.get("time_zone") or "—", "new": zone}]
        if "/" not in zone:
            item["blocked"] = "nel backup c'è solo il fuso in formato POSIX: impostalo dalla pagina web del controller"
        items.append(item)
    ntp = saved.get("ntp") or {}
    if (ntp.get("servers") or ntp.get("source")) and _norm(ntp) != _norm(current.get("ntp") or {}):
        item = _row("disp-ntp", "dispositivo", "riporta", "Server NTP", "Riporta com'era", _ntp=ntp)
        item["changes"] = _differences(current.get("ntp") or {}, ntp)
        items.append(item)
    mqtt = saved.get("mqtt") or {}
    if (mqtt.get("server") or {}).get("host") and _norm(mqtt) != _norm(current.get("mqtt") or {}):
        username = str(mqtt.get("username") or "")
        item = _row(
            "disp-mqtt",
            "dispositivo",
            "riporta",
            "Collegamento MQTT",
            "Riporta il broker e lo attiva: gli accessi tornano in diretta",
            _config=mqtt,
            # Password da scrivere nell'anteprima: nei backup non c'è; l'add-on conosce solo la sua
            password=bool(username) and username not in mqtt_users,
            username=username,
        )
        server = mqtt.get("server") or {}
        old_server = (current.get("mqtt") or {}).get("server") or {}
        item["changes"] = [
            {
                "label": "Broker",
                "old": f"{old_server.get('host')}:{old_server.get('port')}" if old_server.get("host") else "—",
                "new": f"{server.get('host')}:{server.get('port') or 1883}",
            },
            *([{"label": "Utente", "old": "—", "new": username}] if username else []),
        ]
        items.append(item)
    events = saved.get("mqtt_events") or {}
    if events and _norm(events) != _norm(current.get("mqtt_events") or {}):
        item = _row("disp-mqtt-eventi", "dispositivo", "riporta", "Eventi MQTT", "Riporta com'erano", _config=events)
        filters = [str(f.get("topicFilter") or "") for f in events.get("eventFilterList") or []]
        item["changes"] = [{"label": "Eventi", "old": "—", "new": ", ".join(filters) or "come nel backup"}]
        items.append(item)
    return items


# Programmazione


def _by(part: dict, key: str, field: str = "token") -> dict[str, dict]:
    return {str(o.get(field)): o for o in part.get(key) or [] if o.get(field)}


def _plan_programming(saved: dict, current: dict, ctx: dict, saved_common: dict, current_common: dict) -> list[dict]:
    local, replaced, guess = _devices(saved, current)
    # Appena resettato: nessuna tessera, purché l'elenco si sia letto davvero (un errore di lettura non è un elenco
    # vuoto, e un controller che funziona non deve sembrare resettato)
    fresh = (
        not _read_problem(current, "credentials") and not current.get("credentials") and bool(saved.get("credentials"))
    )
    names = {**ctx["doors"]}
    names.update({str(i.get("token")): i.get("Name") or i.get("token") for i in saved.get("idpoints") or []})
    for door in saved.get("doors") or []:
        names.setdefault(str(door.get("token")), door.get("Name") or door.get("token"))
    # Configurazioni di porte e lettori: sempre senza dispositivo. Il controller ci mette il suo e così le collega
    # a sé (con il DeviceUUID scritto esplicito il lettore restava scollegato dal controllo accessi: al banco le
    # tessere davano solo CardPresented). I punti di accesso cambiano dispositivo solo con un controller sostituito.
    common = {"_local": local, "_remap": local if replaced else [], "_guess": guess}
    items: list[dict] = []
    skipped = [k for k in READ_LABELS if k != "credentials" and (_read_problem(saved, k) or _read_problem(current, k))]

    def objects(key: str, field: str = "token") -> tuple[dict, dict] | None:
        if key in skipped or key not in saved:
            return None
        return _by(saved, key, field), _by(current, key, field)

    def add(prefix: str, token: str, label: str, saved_obj: dict, current_obj: dict | None, **extra) -> dict | None:
        changes = _differences(current_obj, saved_obj) if current_obj is not None else []
        if current_obj is not None and not changes and not extra.pop("force", False):
            return None
        action = "ricrea" if current_obj is None else "riporta"
        item = _row(
            f"prog-{prefix}:{token}",
            "controller",
            action,
            label,
            "Ricrea con il token del backup" if action == "ricrea" else "Riporta com'era",
            _kind=prefix,
            _token=token,
            _saved=saved_obj,
            **common,
            **extra,
        )
        item["changes"] = changes
        items.append(item)
        return item

    found = objects("schedules")
    if found:
        # Regole e festività delle fasce fatte con l'app: sono nei Dati Comuni e tornano insieme alla fascia
        app_rules = {s["token"]: s for s in saved_common.get("schedules") or [] if "rules" in s}
        managed = {s["token"] for s in current_common.get("schedules") or [] if "rules" in s}
        for token, schedule in found[0].items():
            label = _named("Fascia Oraria", ctx["schedules"].get(token) or schedule.get("Name") or token)
            rules = app_rules.get(token)
            extra = (
                {"_rules": {"rules": rules["rules"] or [], "holidays": bool(rules.get("holidays"))}} if rules else {}
            )
            now = found[1].get(token)
            if extra and extra["_rules"]["holidays"] and now is not None:
                # Le eccezioni si scrivono con le festività di adesso: quelle del giorno del backup non contano
                schedule = {**schedule, "ExceptionScheduleDefinition": now.get("ExceptionScheduleDefinition")}
            # Fascia uguale sul controller, ma l'app ne ha perso le regole: si rimettono solo quelle
            lost = bool(rules) and now is not None and token not in managed
            item = add("fascia", token, label, schedule, now, force=lost, **extra)
            if item is not None and lost and not item["changes"]:
                item.update(text="Riporta le regole della fascia nell'app", _app_only=True)
                item["changes"] = [
                    {"label": "Regole nell'App", "old": "—", "new": describe_rules(rules["rules"] or []) or "—"}
                ]
    if "authentication_profiles" in saved and "authentication_profiles" in current:
        now = _by(current, "authentication_profiles")
        for token, profile in _by(saved, "authentication_profiles").items():
            label = _named("Modo di Identificazione", profile.get("Name") or token)
            add("modo", token, label, profile, now.get(token))
    doors = objects("doors")
    configs = objects("door_configurations") or ({}, {})
    if doors:
        for token, door in doors[0].items():
            payload, warnings = _door_payload(door)
            config = configs[0].get(token)
            item = _plan_pair(
                "porta",
                token,
                _named("Porta", names.get(token) or token),
                payload,
                doors[1].get(token),
                config,
                configs[1].get(token),
                add,
            )
            if item is not None:
                item["warnings"] += warnings
                if "door_configurations" in skipped:
                    item["warnings"].append("la configurazione della porta non è nel backup: resta quella attuale")
    readers = objects("idpoints")
    reader_configs = objects("idpoint_configurations") or ({}, {})
    if readers:
        for token, reader in readers[0].items():
            _plan_pair(
                "lettore",
                token,
                _named("Lettore", reader.get("Name") or token),
                reader,
                readers[1].get(token),
                reader_configs[0].get(token),
                reader_configs[1].get(token),
                add,
            )
    io = objects("io_assignments", "IoName")
    if io:
        for name, assignment in io[0].items():
            if not assignment.get("IoUser"):
                continue
            now = io[1].get(name)
            item = add("io", name, f"Morsetto {_io_name(name)}", assignment, now)
            if item is not None:
                # Il morsetto c'è sempre: libero dopo un reset, si assegna come nel backup
                if not (now or {}).get("IoUser"):
                    item.update(action="ricrea", text="Assegna come nel backup")
                item["changes"] = [
                    {
                        "label": "Assegnato a",
                        "old": _io_users(io[1].get(name), names),
                        "new": _io_users(assignment, names),
                    }
                ]
    points = objects("access_points")
    if points:
        for token, point in points[0].items():
            add("punto", token, _named("Punto di Accesso", point.get("Name") or token), point, points[1].get(token))
        if current.get("access_controllers") is not None:
            item = _plan_managed(saved, current, points[0])
            if item is not None:
                items.append(item)
    profiles = objects("access_profiles")
    if profiles:
        for token, profile in profiles[0].items():
            label = _named("Profilo di Accesso", profile.get("Name") or token)
            add("profilo", token, label, profile, profiles[1].get(token))
    # Quello che c'è sul controller e non nel backup: dopo un reset sono gli oggetti di fabbrica, e si tolgono
    text = "Togli: non c'è nel backup" if fresh else "Togli: non c'era nel backup"
    if io:
        for name, assignment in io[1].items():
            if assignment.get("IoUser") and not (io[0].get(name) or {}).get("IoUser"):
                item = _row(
                    f"prog-io-libera:{name}",
                    "controller",
                    "togli",
                    f"Morsetto {_io_name(name)}",
                    "Libera: nel backup non era usato",
                    fresh,
                    _kind="io-libera",
                    _token=name,
                    _saved=assignment,
                )
                item["changes"] = [{"label": "Assegnato a", "old": _io_users(assignment, names), "new": "—"}]
                items.append(item)
    for key, prefix, noun in (
        ("access_points", "punto", "Punto di Accesso"),
        ("idpoints", "lettore", "Lettore"),
        ("doors", "porta", "Porta"),
        ("schedules", "fascia", "Fascia Oraria"),
    ):
        found = objects(key)
        # Fasce in più solo dopo un reset (sono quelle di fabbrica): altrimenti possono servire a tipologie e ingressi
        if not found or (key == "schedules" and not fresh):
            continue
        for token, obj in found[1].items():
            if token not in found[0]:
                items.append(
                    _row(
                        f"prog-{prefix}-togli:{token}",
                        "controller",
                        "togli",
                        _named(noun, obj.get("Name") or token),
                        text,
                        fresh,
                        _kind=f"{prefix}-togli",
                        _token=token,
                    )
                )
    for key in skipped:
        items.append(
            _row(
                f"prog-letto:{key}",
                "controller",
                "riporta",
                READ_TITLES[key],
                "",
                blocked=NOT_READ,
            )
        )
    return items


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
IO_NAMES = {"Ser0hd": "Lettori RS-485", "relay": "Relè Serratura", "Wiegand1": "Lettore Wiegand"}


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


# Tessere


def _plan_cards(current: dict, common: dict) -> list[dict]:
    if _read_problem(current, "credentials"):
        return [
            _row("tessere", "tessere", "ricrea", "Tessere", "", blocked="le tessere del controller ora non si leggono")
        ]
    present = {str(c.get("card") or "").upper() for c in current.get("credentials") or []}
    missing = [c for c in common.get("cards") or [] if c["number"].upper() not in present]
    if not missing:
        return []
    names = {p["id"]: p["name"] for p in common.get("persons") or []}
    persons = {c["person_id"] for c in missing}
    item = _row(
        "tessere",
        "tessere",
        "ricrea",
        "Tessere",
        f"Riscrive {plural(len(missing), 'tessera', 'tessere')} di {plural(len(persons), 'persona', 'persone')}, "
        "con accesso, validità e stato salvati nell'app",
    )
    shown = ", ".join(f"{c['number']} ({names.get(c['person_id'], '?')})" for c in missing[:8])
    item["changes"] = [{"label": "Tessere", "old": "—", "new": shown + (" e altre" if len(missing) > 8 else "")}]
    return [item]


# Applicazione


async def apply_item(item: dict, runtime, user: str) -> dict:
    """Scrive una riga; restituisce il testo del risultato e le parti non riuscite (per le tessere)."""
    controller = runtime.access.controller
    key = item["key"]
    if key == "disp-fuso":
        await controller.set_time_zone(item["_zone"])
    elif key == "disp-ntp":
        ntp = item["_ntp"]
        await controller.set_ntp(
            ntp.get("enabled") is not False, ntp.get("source") or "static", ntp.get("servers") or []
        )
    elif key == "disp-mqtt":
        config = copy.deepcopy(item["_config"])
        username = str(config.get("username") or "")
        if username:
            password = runtime.mqtt_known.get(username) or runtime.mqtt_password
            if not password:
                raise ServiceError(f"scrivi la password del broker MQTT per l'utente {username}")
            config["password"] = password
        await controller.set_mqtt_client(config)
    elif key == "disp-mqtt-eventi":
        await controller.set_mqtt_events(copy.deepcopy(item["_config"]))
    elif key == "tessere":
        return await _apply_cards(runtime, user)
    else:
        await _apply_programming(item, runtime, controller)
    return {}


def _replaced(device: str, item: dict) -> bool:
    return bool(device) and device in item["_remap"]


async def _new_device(item: dict, runtime, controller) -> str:
    """Dispositivo del controller nuovo, letto dalle configurazioni appena scritte (lì lo mette il controller)."""
    if runtime.cache.get("device"):
        return runtime.cache["device"]
    for reader in ("list_idpoint_configurations", "list_door_configurations"):
        try:
            configs = await getattr(controller, reader)()
        except VapixError:
            continue
        device = next((c.get("DeviceUUID") for c in configs if c.get("DeviceUUID")), "")
        if device:
            runtime.cache["device"] = device
            return device
    return item["_guess"]


def _config_payload(config: dict, item: dict, token: str) -> dict:
    device = config.get("DeviceUUID") or ""
    return {
        "token": token,
        "DeviceUUID": "" if device in item["_local"] else device,
        "Configuration": copy.deepcopy(config.get("Configuration") or []),
    }


def _swap(value, tokens: dict[str, str]):
    """Riferimenti agli oggetti ricreati con un token nuovo (fasce nei profili, lettori nei punti di accesso...)."""
    if not tokens:
        return value
    if isinstance(value, dict):
        return {k: _swap(v, tokens) for k, v in value.items()}
    if isinstance(value, list):
        return [_swap(v, tokens) for v in value]
    return tokens.get(value, value) if isinstance(value, str) else value


async def _create(runtime, item: dict, write, payload: dict, listing) -> str:
    """Scrive l'oggetto con il suo token. I token generati dal controller (Axis-<serie>:...) potrebbero non essere
    accettati da un altro controller: in quel caso lo si crea senza e il token nuovo si cambia in tutta l'app.
    Solo per un oggetto da ricreare, e solo se dopo l'errore non c'è ancora: un errore arrivato a scrittura fatta
    (per esempio un timeout) non deve lasciare un doppione sul controller."""
    token = payload.get("token") or ""
    try:
        written = await write(payload) or token
    except VapixError as err:
        if item["action"] != "ricrea" or not token.startswith("Axis-"):
            raise
        try:
            exists = token in {o.get("token") for o in await listing()}
        except VapixError:
            raise err from None
        if exists:
            return token
        written = await write({**payload, "token": ""})
        if not written:
            raise
    if written != token:
        _rename(runtime, token, written)
    return written


def _rename(runtime, old: str, new: str) -> None:
    """Token nuovo dato dal controller: si cambia in tutta l'app e si ricorda per i ripristini dagli stessi backup."""
    runtime.cache.setdefault("tokens", {})[old] = new
    runtime.db.replace_token(old, new)
    known = {k: new if v == old else v for k, v in renamed_tokens(runtime.db).items()}
    known[old] = new
    runtime.db.set_setting(SETTING_TOKENS, json.dumps(known))


async def _apply_programming(item: dict, runtime, controller) -> None:
    kind, token = item["_kind"], item["_token"]
    tokens = runtime.cache.get("tokens") or {}
    saved = _swap(copy.deepcopy(item.get("_saved")), tokens)
    if kind == "fascia":
        rules = item.get("_rules")
        if rules and rules["holidays"]:
            # Le festività di adesso, non quelle del giorno del backup
            saved["ExceptionScheduleDefinition"] = holidays_ical(runtime.db.list_holidays())
        token = tokens.get(token, token)
        if not item.get("_app_only") or saved != _swap(item["_saved"], tokens):
            token = await _create(runtime, item, controller.set_schedule, saved, controller.list_schedules)
        if rules:
            # Regole nell'app: senza, la fascia risulta esterna e le festività cambiate dopo non le arrivano
            runtime.db.save_schedule_rules(token, rules["rules"], rules["holidays"])
    elif kind == "modo":
        await controller.set_authentication_profile(saved)
    elif kind == "porta":
        door = await _create(runtime, item, controller.set_door, saved, controller.list_doors)
        if item.get("_config"):
            # Solo serratura, contatto e relè: la programmazione oraria della porta (sblocchi) non si tocca
            await controller.set_door_configuration(_config_payload(item["_config"], item, door))
    elif kind == "lettore":
        reader = await _create(runtime, item, controller.create_idpoint, saved, controller.list_idpoints)
        if item.get("_config"):
            payload = _config_payload(item["_config"], item, reader)
            values = {str(c.get("Name")): str(c.get("Value")) for c in payload["Configuration"]}
            await controller.set_idpoint_configuration(reader, payload["DeviceUUID"], values)
    elif kind == "io":
        await controller.set_io_assignment(token, saved.get("IoMode") or "", saved.get("IoUser") or [])
    elif kind == "punto":
        if _replaced(saved.get("DoorDeviceUUID") or "", item):
            saved["DoorDeviceUUID"] = await _new_device(item, runtime, controller)
        for device in saved.get("IdPointDevice") or []:
            if _replaced(device.get("DeviceUUID") or "", item):
                device["DeviceUUID"] = await _new_device(item, runtime, controller)
        await _create(runtime, item, controller.create_access_point, saved, controller.list_access_points)
    elif kind == "profilo":
        await _create(runtime, item, controller.set_access_profile, saved, controller.list_access_profiles)
    elif kind == "io-libera":
        await controller.set_io_assignment(token, saved.get("IoMode") or "", [])
    elif kind == "gestiti":
        existing = {p.get("token") for p in await controller.list_access_points()}
        await link_access_points(controller, add=[t for t in _swap(item["_points"], tokens) if t in existing])
    elif kind == "punto-togli":
        await link_access_points(controller, remove={token})
        await controller.remove_access_point(token)
    elif kind == "lettore-togli":
        await controller.remove_idpoint(token)
    elif kind == "porta-togli":
        await controller.remove_door(token)
    elif kind == "fascia-togli":
        await controller.remove_schedule(token)
    else:
        raise ServiceError("riga del ripristino non riconosciuta")


async def _apply_cards(runtime, user: str) -> dict:
    """Le tessere che il controller non ha più: si segnano con la sincronizzazione e si riscrivono."""
    await runtime.access.sync_from_controller()
    result = await runtime.access.restore_missing(user)
    failed = [{"label": f"Tessera {f['card']} ({f['person']})", "error": f["error"]} for f in result["failed"]]
    if not result["restored"] and failed:
        raise ServiceError(f"nessuna tessera riscritta: {failed[0]['error']}")
    return {"text": f"{plural(result['restored'], 'tessera riscritta', 'tessere riscritte')}", "failed": failed}
