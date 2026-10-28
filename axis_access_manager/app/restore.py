"""Ripristino selettivo da un backup: tipologie, persone con le loro tessere, porte e lettori.

Il ripristino parte sempre da un'anteprima, "Cosa Cambierà": per ogni oggetto diverso tra il backup e lo stato
attuale c'è una riga da spuntare. Rimettere quello che è stato cancellato o modificato è spuntato di serie;
togliere quello che è stato aggiunto dopo il backup no, perché di solito è lavoro buono fatto nel frattempo.
Le scritture passano dagli stessi servizi delle modifiche a mano, con gli stessi controlli: tempi della porta
entro i limiti della serratura, nessuno sblocco, scritture sospese se il controller è cambiato.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from . import full_restore
from .backup import COMMON, DOOR_TIMES, FIELD_LABELS, _item, _shown, common_view, context_of, controller_view
from .db import Database
from .programming import ProgrammingService, parse_duration
from .rules_service import RulesService
from .service import AccessService, ServiceError
from .vapix import VapixError

# Sezioni che si possono ripristinare, nell'ordine in cui si applicano: le porte prima delle tipologie (una regola
# può indicare una porta che torna nell'app), le tipologie prima delle persone
RESTORE_SECTIONS = {"porte": "Porte e Lettori", "tipologie": "Tipologie", "persone": "Persone e Tessere"}
# Con il ripristino completo di un controller: prima impostazioni e programmazione, le tessere per ultime
ALL_SECTIONS = {
    "dispositivo": full_restore.FULL_SECTIONS["dispositivo"],
    "controller": full_restore.FULL_SECTIONS["controller"],
    **RESTORE_SECTIONS,
    "tessere": full_restore.FULL_SECTIONS["tessere"],
}
CARD_FIELDS = ("access_profile", "valid_from", "valid_to")
MISSING_PLACES = "alcune regole indicano porte, aree o fasce orarie che non ci sono più o hanno cambiato nome"
# Regole su porte di un controller che adesso non risponde: la fotografia non dice se sono ancora nell'app
UNREAD_DOORS = "il controller non risponde: al ripristino si controlla che le porte delle regole siano ancora nell'app"
# Configurazione della porta (serratura, contatto, relè): qui non si scrive
LOCK_CONFIG = "la configurazione della serratura e del contatto porta si ripristina con il Ripristino Completo"


@dataclass
class Runtime:
    """Servizi con cui si scrive: gli stessi delle modifiche a mano."""

    db: Database
    access: AccessService
    rules: RulesService
    programming: ProgrammingService
    # Controller in uso: per ora porte e lettori si ripristinano solo lì
    controller_id: int | None
    # Ripristino completo: password del broker MQTT scritta nell'anteprima e credenziali MQTT che l'add-on conosce
    mqtt_password: str = ""
    mqtt_known: dict[str, str] = dataclasses.field(default_factory=dict)
    # Dispositivo del controller nuovo e token cambiati, tra una riga e l'altra
    cache: dict = dataclasses.field(default_factory=dict)


def _changes(current: dict | None, saved: dict, only=None) -> list[dict]:
    """Campi diversi: com'è adesso → come tornerà. only: solo questi campi."""
    if current is None:
        return []
    names = list(dict.fromkeys([*saved["fields"], *current["fields"]]))
    return [
        {"label": FIELD_LABELS.get(f, f), "old": _shown(current, f), "new": _shown(saved, f)}
        for f in names
        if current["fields"].get(f) != saved["fields"].get(f) and (only is None or f in only)
    ]


def _new(key: str, section: str, action: str, label: str, text: str, **extra) -> dict:
    return {
        "key": key,
        "section": section,
        "action": action,
        "label": label,
        "text": text,
        "changes": [],
        "warnings": [],
        "blocked": "",
        "default": action != "togli",
        **extra,
    }


def plan(
    saved: dict[str, dict],
    current: dict[str, dict],
    sections: list[str],
    controller_id: int | None,
    mqtt_users: set[str] | None = None,
    tokens: dict[str, str] | None = None,
) -> list[dict]:
    """Righe dell'anteprima per le sezioni scelte. Le chiavi che iniziano con _ servono solo al ripristino.
    tokens: token cambiati dai ripristini completi già fatti (full_restore.renamed_tokens), vecchio → nuovo."""
    if tokens:
        # Il backup li ha ancora vecchi: senza, gli oggetti già ricreati sembrerebbero da ricreare di nuovo
        saved = full_restore._swap(saved, tokens)
    items: list[dict] = []
    full = [s for s in full_restore.FULL_SECTIONS if s in sections]
    if full and controller_id:
        items += full_restore.plan(saved, current, full, controller_id, mqtt_users)
    saved_common, current_common = saved.get(COMMON) or {}, current.get(COMMON) or {}
    saved_ctx, current_ctx = context_of(saved), context_of(current)
    key = f"controller:{controller_id}"
    doors: list[dict] = []
    if "porte" in sections:
        if controller_id and key in saved and key in current:
            doors = _plan_doors(saved[key], current[key], saved_ctx, current_ctx)
        elif controller_id and key in saved:
            doors = [
                _new(
                    "porte",
                    "porte",
                    "riporta",
                    "Porte e Lettori",
                    "Ripristina porte e lettori",
                    blocked="il controller non risponde: porte e lettori si ripristinano quando torna raggiungibile",
                    _covers=["porte"],
                )
            ]
    # Porte che questo ripristino rimette nell'app: le regole delle tipologie le possono indicare
    readded = {(controller_id, i["_token"]) for i in doors if i.get("_readd") and not i["blocked"]}
    matched = match_categories(saved_common, current_common)
    categories: list[dict] = []
    if "tipologie" in sections:
        places = _places(saved, current, saved_ctx, current_ctx, readded)
        categories = _plan_categories(saved_common, current_common, saved_ctx, current_ctx, matched, places)
    persons: list[dict] = []
    if "persone" in sections:
        # Tipologie che questo ripristino ricrea (nome → bloccata) e profili che ci sono adesso sul controller
        recreated = {i["label"].lower(): bool(i["blocked"]) for i in categories if i["action"] == "ricrea"}
        part = current.get(key)
        profiles = None
        if part is not None and not full_restore._read_problem(part, "access_profiles"):
            profiles = {p.get("token") for p in part.get("access_profiles", [])}
        persons = _plan_persons(saved_common, current_common, saved_ctx, current_ctx, matched, recreated, profiles)
    return items + doors + categories + persons


def public(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


# Tipologie


def match_categories(saved: dict, current: dict) -> dict[int, dict]:
    """Tipologia di adesso per ogni tipologia del backup. Per numero solo se è proprio quella (stesso nome o stesso
    profilo): SQLite dà a una tipologia nuova il numero dell'ultima eliminata. Altrimenti per nome."""
    now = current.get("categories", [])
    by_id = {c["id"]: c for c in now}
    by_name = {c["name"].lower(): c for c in now}
    matched: dict[int, dict] = {}
    for category in saved.get("categories", []):
        same = by_id.get(category["id"])
        if same and (
            same["name"].lower() == category["name"].lower()
            or (category.get("group_token") and same.get("group_token") == category["group_token"])
        ):
            matched[category["id"]] = same
    taken = {c["id"] for c in matched.values()}
    for category in saved.get("categories", []):
        other = by_name.get(category["name"].lower())
        if category["id"] not in matched and other and other["id"] not in taken:
            matched[category["id"]] = other
            taken.add(other["id"])
    return matched


def _door_keys(parts: dict[str, dict]) -> dict[int, tuple[int, str]]:
    """Porte dell'app per numero, con controller e token: il numero si può riusare, il token no."""
    return {
        door["id"]: (int(key.split(":", 1)[1]), door["door_token"])
        for key, part in parts.items()
        if key != COMMON
        for door in part.get("app_doors", [])
    }


def _same_place(saved_id, saved_names: dict, current_names: dict) -> int | None:
    """Numero di adesso di un'area o di una struttura del backup, riconosciuta dal nome (il numero può essere quello
    di un'altra creata dopo). None se non c'è più o ha cambiato nome."""
    name = saved_names.get(saved_id)
    if name is None:
        return None
    if current_names.get(saved_id) == name:
        return saved_id
    return next((i for i, n in current_names.items() if n == name), None)


def _places(saved: dict, current: dict, saved_ctx: dict, current_ctx: dict, readded: set) -> dict:
    return {
        "saved_doors": _door_keys(saved),
        "current_doors": {v: k for k, v in _door_keys(current).items()},
        "readded": readded,
        # Controller letti adesso: le porte degli altri non sono nella fotografia
        "read": {int(key.split(":", 1)[1]) for key in current if key != COMMON},
        "schedules": {s["token"] for s in (current.get(COMMON) or {}).get("schedules", [])},
        "saved_ctx": saved_ctx,
        "current_ctx": current_ctx,
    }


def _map_rules(rules: list[dict], places: dict) -> tuple[list[dict], str, bool, bool]:
    """Regole del backup con i numeri di adesso, il testo da mostrare (con i nomi di adesso), se qualche posto o
    fascia non c'è più e se qualche porta è di un controller che non risponde. Le porte si riconoscono dal token,
    aree e strutture dal nome."""
    saved_ctx, current_ctx = places["saved_ctx"], places["current_ctx"]
    mapped, shown, missing, unread = [], [], False, False
    for rule in rules:
        target, schedule, door = rule["target"], rule["schedule"], None
        if target == "door":
            door = places["saved_doors"].get(rule["target_id"])
            now = places["current_doors"].get(door) if door else None
            name = current_ctx["doors"].get(now) or saved_ctx["doors"].get(rule["target_id"])
            # Senza la parte del controller la porta sembrava sparita dall'app: la si cerca al ripristino
            unknown = door is not None and door[0] not in places["read"]
            unread |= unknown
            found = now is not None or door in places["readded"] or unknown
        elif target in ("area", "site"):
            kind = "areas" if target == "area" else "sites"
            now = _same_place(rule["target_id"], saved_ctx[kind], current_ctx[kind])
            name = saved_ctx[kind].get(rule["target_id"])
            found = now is not None
        else:
            now, name, found = None, "", False
        missing |= not found or schedule not in places["schedules"]
        # La porta si cerca di nuovo al ripristino: può tornare nell'app con un numero nuovo
        mapped.append(
            {
                "target": target,
                "target_id": now,
                "schedule": schedule,
                **({"_door": door, "_name": name} if door else {}),
            }
        )
        place = name or f"{target} {rule['target_id']}"
        when = current_ctx["schedules"].get(schedule) or saved_ctx["schedules"].get(schedule) or schedule
        shown.append(f"{place}: {when}")
    return mapped, "; ".join(shown) or "—", missing, unread


def _plan_categories(
    saved: dict, current: dict, saved_ctx: dict, current_ctx: dict, matched: dict[int, dict], places: dict
) -> list[dict]:
    saved_view, current_view = (
        common_view(saved, saved_ctx)["tipologie"],
        common_view(current, current_ctx)["tipologie"],
    )
    default = int((current.get("settings") or {}).get("default_category") or 0)
    used = {p["category_id"] for p in current.get("persons", [])}
    current_rules = {c["id"]: c["rules"] for c in current.get("categories", [])}
    items = []
    for category in saved.get("categories", []):
        now = matched.get(category["id"])
        key = f"tipologia:{category['id']}"
        saved_item = saved_view[str(category["id"])]
        fields = {k: category[k] for k in ("name", "color", "valid_days", "description")}
        if now is not None and category["rules"] == current_rules.get(now["id"]):
            # Regole uguali, numero per numero: le aree e le porte che indicano non sono state eliminate (si può
            # solo se nessuna tipologia le usa), al massimo rinominate
            rules, unread = category["rules"], False
            missing = any(r["schedule"] not in places["schedules"] for r in rules)
            saved_item = {
                **saved_item,
                "show": {**saved_item["show"], "rules": rules_label(current_view[str(now["id"])])},
            }
        else:
            rules, shown, missing, unread = _map_rules(category["rules"], places)
            plain = [{k: r[k] for k in ("target", "target_id", "schedule")} for r in rules]
            saved_item = {
                **saved_item,
                "fields": {**saved_item["fields"], "rules": plain},
                "show": {**saved_item["show"], "rules": shown},
            }
        if now is not None:
            changes = _changes(current_view[str(now["id"])], saved_item)
            if changes:
                item = _new(key, "tipologie", "riporta", category["name"], "Riporta com'era", _target=now["id"])
                item.update(changes=changes, _fields=fields, _rules=rules)
                if missing:
                    item["blocked"] = MISSING_PLACES
                elif unread:
                    item["warnings"].append(UNREAD_DOORS)
                items.append(item)
            continue
        item = _new(
            key,
            "tipologie",
            "ricrea",
            category["name"],
            "Ricrea la tipologia",
            _fields=fields,
            _rules=rules,
            _id=category["id"],
        )
        item["changes"] = [{"label": "Regole", "old": "—", "new": rules_label(saved_item)}]
        if missing:
            item["blocked"] = MISSING_PLACES
        elif unread:
            item["warnings"].append(UNREAD_DOORS)
        items.append(item)
    taken = {c["id"] for c in matched.values()}
    for category in current.get("categories", []):
        if category["id"] in taken:
            continue
        item = _new(
            f"tipologia-nuova:{category['id']}",
            "tipologie",
            "togli",
            category["name"],
            "Elimina: creata dopo il backup",
            _target=category["id"],
        )
        if category["id"] == default:
            item["blocked"] = "è la tipologia predefinita"
        elif category["id"] in used:
            item["blocked"] = "ci sono persone con questa tipologia"
        items.append(item)
    return items


def rules_label(item: dict) -> str:
    return _shown(item, "rules")


# Persone e tessere


def _card_item(card: dict, profile: str) -> dict:
    """Tessera da confrontare; profile: il suo accesso come si mostra."""
    return _item(
        card["number"],
        {"enabled": card["enabled"], **{k: card[k] for k in CARD_FIELDS}},
        {"enabled": "attiva" if card["enabled"] else "sospesa", "access_profile": profile},
    )


def _profile_name(token: str, current_ctx: dict, saved_ctx: dict) -> str:
    return current_ctx["profiles"].get(token) or saved_ctx["profiles"].get(token) or token or "—"


def _card_access(card: dict, target: dict | None, pending: str, current_ctx: dict, saved_ctx: dict):
    """Profilo che la tessera avrà (None se lo dà una tipologia ricreata da questo ripristino) e come mostrarlo."""
    if not card.get("_category_access"):
        return card["access_profile"], _profile_name(card["access_profile"], current_ctx, saved_ctx)
    if target is not None:
        return target["group_token"], target["name"]
    return None, f"{pending} (ricreata)" if pending else "—"


def _plan_persons(
    saved: dict,
    current: dict,
    saved_ctx: dict,
    current_ctx: dict,
    matched: dict[int, dict],
    recreated: dict[str, bool],
    profiles: set[str] | None,
) -> list[dict]:
    """matched: tipologie di adesso per numero del backup; recreated: tipologie che questo ripristino ricrea (nome
    in minuscolo → bloccata); profiles: profili di accesso che ci sono adesso sul controller (None: non si sa)."""
    saved_view, current_view = common_view(saved, saved_ctx)["persone"], common_view(current, current_ctx)["persone"]
    current_persons = {(p["id"], p["created_at"]): p for p in current.get("persons", [])}
    current_cards = {c["number"].upper(): c for c in current.get("cards", [])}
    current_names = {p["id"]: p["name"] for p in current.get("persons", [])}
    categories = {c["id"]: c for c in current.get("categories", [])}
    default = categories.get(int((current.get("settings") or {}).get("default_category") or 0))
    if default is not None and not default.get("group_token"):
        default = None
    in_backup = {c["number"].upper() for c in saved.get("cards", [])}
    saved_cards: dict[int, list[dict]] = {}
    for card in saved.get("cards", []):
        saved_cards.setdefault(card["person_id"], []).append(card)
    items, matched_persons = [], set()
    for person in saved.get("persons", []):
        ident = (person["id"], person["created_at"])
        now = current_persons.get(ident)
        key = f"persona:{person['id']}:{person['created_at']}"
        item = _new(key, "persone", "riporta" if now else "ricrea", person["name"], "")
        name = saved_ctx["categories"].get(person["category_id"], "") if person["category_id"] else ""
        # La tipologia di adesso: la stessa anche se rinominata, oppure quella che questo ripristino ricrea
        category = matched.get(person["category_id"]) if name else None
        pending = name if name and category is None and recreated.get(name.lower()) is False else ""
        cards = []
        for card in saved_cards.get(person["id"], []):
            owner = current_cards.get(card["number"].upper())
            if owner and (now is None or owner["person_id"] != now["id"]):
                item["warnings"].append(
                    f"la tessera {card['number']} ora è di "
                    f"{current_names.get(owner['person_id'], 'un altro')}: resta sua"
                )
                continue
            cards.append(dict(card))
        mine = {c["number"].upper(): c for c in current.get("cards", []) if now and c["person_id"] == now["id"]}
        lost = []
        for card in cards:
            have = mine.get(card["number"].upper())
            if have is not None and not card["access_profile"]:
                # Nel backup senza profilo: resta quello di adesso
                card["access_profile"] = have["access_profile"]
            elif profiles is not None and card["access_profile"] not in profiles:
                # Profilo sparito: quello della tipologia, detto nell'anteprima
                card["_category_access"] = True
                lost.append(card["number"])
        target = category
        if lost and category is None and not pending:
            if default is None:
                item["blocked"] = (
                    f"il profilo di accesso della tessera {lost[0]} non c'è più e non c'è una tipologia predefinita"
                )
            else:
                # Né il profilo né la tipologia: quella predefinita, ma solo se la spunti
                target = default
                item["default"] = False
        if name and target is None and not pending:
            how = " e non si può ricreare:" if name.lower() in recreated else ": ripristinala insieme, altrimenti"
            item["warnings"].append(f"la tipologia {name} non c'è più{how} la persona resta senza tipologia")
        for number in lost:
            what = f"il profilo di accesso della tessera {number}"
            if pending:
                text = f"{what} non c'è più: avrà quello della tipologia {pending}, ricreata con questo ripristino"
            elif category is not None:
                text = f"{what} non c'è più: avrà quello della tipologia {category['name']}"
            elif target is not None:
                gone = f"e la tipologia {name} non ci sono più" if name else "non c'è più"
                text = f"{what} {gone}: avrà l'accesso della tipologia predefinita {target['name']}"
            else:
                continue
            item["warnings"].append(text)
        category_name = target["name"] if target else pending
        item["_person"], item["_cards"] = person, cards
        item["_category_id"], item["_category"] = (target["id"] if target else 0), pending
        if now is None:
            item["text"] = "Ricrea la persona"
            item["changes"] = [
                {"label": "Tessere", "old": "—", "new": ", ".join(c["number"] for c in cards) or "—"},
                *([{"label": "Tipologia", "old": "—", "new": category_name}] if category_name else []),
                *[
                    {
                        "label": f"Tessera {c['number']}: accesso",
                        "old": "—",
                        "new": _card_access(c, target, pending, current_ctx, saved_ctx)[1],
                    }
                    for c in cards
                    if c.get("_category_access")
                ],
            ]
            if not cards:
                item["blocked"] = "tutte le sue tessere ora sono di altre persone"
            items.append(item)
            continue
        matched_persons.add(ident)
        current_item = current_view[f"{now['id']}:{now['created_at']}"]
        changes = _changes(current_item, saved_view[f"{ident[0]}:{ident[1]}"], only=("name", "company", "notes"))
        if pending or (target["id"] if target else 0) != now["category_id"]:
            changes.append(
                {
                    "label": FIELD_LABELS["category_id"],
                    "old": _shown(current_item, "category_id"),
                    "new": category_name or "nessuna",
                }
            )
        wanted = {c["number"].upper(): c for c in cards}
        for number, card in wanted.items():
            profile, shown = _card_access(card, target, pending, current_ctx, saved_ctx)
            if number not in mine:
                changes.append({"label": f"Tessera {card['number']}", "old": "—", "new": "rimessa"})
                if card.get("_category_access"):
                    changes.append({"label": f"Tessera {card['number']}: accesso", "old": "—", "new": shown})
                continue
            have = mine[number]
            for change in _changes(
                _card_item(have, _profile_name(have["access_profile"], current_ctx, saved_ctx)),
                _card_item({**card, "access_profile": profile}, shown),
            ):
                changes.append({**change, "label": f"Tessera {card['number']}: {change['label'].lower()}"})
        if changes:
            item["text"] = "Riporta com'era"
            item["changes"] = changes
            if not cards and not mine:
                item["blocked"] = "tutte le sue tessere ora sono di altre persone"
            items.append(item)
        # Tessere aggiunte dopo il backup: restano, e si eliminano solo spuntando la loro riga. Non quelle che nel
        # backup erano di un'altra persona: la riga dell'altra dice già che restano di questa
        for number, card in mine.items():
            if number in wanted or number in in_backup:
                continue
            row = _new(
                f"tessera-nuova:{card['number']}",
                "persone",
                "togli",
                person["name"],
                f"Elimina la tessera {card['number']}: aggiunta dopo il backup",
                _target=card["number"],
                _owner=now["id"],
            )
            if len(mine) == 1 and not cards:
                row["blocked"] = f"è l'unica tessera di {now['name']}"
            items.append(row)
    for person in current.get("persons", []):
        if (person["id"], person["created_at"]) in matched_persons:
            continue
        items.append(
            _new(
                f"persona-nuova:{person['id']}",
                "persone",
                "togli",
                person["name"],
                "Elimina: registrata dopo il backup",
                _target=person["id"],
            )
        )
    return items


# Porte e lettori


def _door_values(door: dict) -> dict:
    values = {"name": door.get("Name") or "", "description": door.get("Description") or ""}
    for field, (key, *_) in DOOR_TIMES.items():
        if field in door:
            values[key] = parse_duration(door[field])
    return values


def _plan_doors(saved: dict, current: dict, saved_ctx: dict, current_ctx: dict) -> list[dict]:
    saved_view, current_view = controller_view(saved, saved_ctx), controller_view(current, current_ctx)
    current_doors = {d.get("token"): d for d in current.get("doors", [])}
    saved_app = {d["door_token"]: d for d in saved.get("app_doors", [])}
    current_app = {d["door_token"]: d for d in current.get("app_doors", [])}
    # Un elenco non letto (nel backup o adesso) non è vuoto: le sue porte non sono "sparite"
    unread = [
        k
        for k in ("doors", "idpoints")
        if full_restore._read_problem(saved, k) or full_restore._read_problem(current, k)
    ]
    items = [
        _new(f"porte-non-lette:{k}", "porte", "riporta", full_restore.READ_TITLES[k], "", blocked=full_restore.NOT_READ)
        for k in unread
    ]
    # La configurazione si confronta solo se letta da tutte e due le parti
    configs = not any(
        full_restore._read_problem(p, "door_configurations") or not p.get("door_configurations")
        for p in (saved, current)
    )
    for door in [] if "doors" in unread else saved.get("doors", []):
        token = door.get("token", "")
        saved_item = saved_view["porte"][token]
        item = _new(f"porta:{token}", "porte", "riporta", saved_item["label"], "Riporta com'era")
        if token not in current_doors:
            item["blocked"] = "non è più programmata sul controller: ricreala con il ripristino completo"
            items.append(item)
            continue
        current_item = current_view["porte"][token]
        app = saved_app.get(token)
        # Solo quello che il ripristino scrive: nome, descrizione e tempi, e le impostazioni dell'app se la porta
        # era nell'app al momento del backup
        fields = {"Name", "Description", *DOOR_TIMES}
        if app:
            fields |= set(Database.DOOR_FIELDS) - {"door_token"}
            # L'area si riconosce dal nome: il suo numero può essere quello di un'area creata dopo
            area = _same_place(app["area_id"], saved_ctx["areas"], current_ctx["areas"]) if app["area_id"] else 0
            if area is None:
                area = (current_app.get(token) or {}).get("area_id") or 0
                where = saved_ctx["areas"].get(app["area_id"]) or "della porta"
                item["warnings"].append(
                    f"l'area {where} non c'è più o ha cambiato nome: la posizione della porta resta com'è"
                )
            app = {**app, "area_id": area}
            saved_item = {
                **saved_item,
                "fields": {**saved_item["fields"], "area_id": area},
                "show": {**saved_item["show"], "area_id": current_ctx["areas"].get(area, "come il controller")},
            }
        values = _door_values(door)
        for field, (key, label, low, high) in DOOR_TIMES.items():
            seconds = values.get(key)
            if (
                seconds is not None
                and not low <= seconds <= high
                and seconds != parse_duration(current_doors[token].get(field))
            ):
                item["warnings"].append(
                    f"{label}: {seconds} s è fuori dai limiti della serratura ({low}-{high} s), resta com'è"
                )
                values.pop(key)
        # Una porta tornata nell'app non ha i campi dell'app: "—" → "—" non è una modifica da mostrare
        changes = [c for c in _changes(current_item, saved_item, only=fields) if c["old"] != c["new"]]
        if token not in current_app and app:
            changes.append({"label": "Gestita dall'App", "old": "no", "new": "sì"})
            item["_readd"] = True
        lock = _changes(current_item, saved_item, only=("configuration",)) if configs else []
        if changes:
            if lock:
                item["warnings"].append(LOCK_CONFIG)
            item.update(changes=changes, _token=token, _values=values, _app=app)
            items.append(item)
        elif lock:
            # Solo la serratura è diversa: qui non si scrive, ma l'anteprima non deve dire che è tutto come prima
            old, new = current_item["fields"]["configuration"], saved_item["fields"]["configuration"]
            item["changes"] = [
                {"label": name, "old": old.get(name) or "—", "new": new.get(name) or "—"}
                for name in dict.fromkeys([*new, *old])
                if old.get(name) != new.get(name)
            ]
            item["blocked"] = LOCK_CONFIG
            items.append(item)
    for token, saved_item in [] if "idpoints" in unread else saved_view["lettori"].items():
        current_item = current_view["lettori"].get(token)
        if current_item is None:
            continue
        changes = [
            c for c in _changes(current_item, saved_item) if c["label"] in ("Nome sul Controller", "Descrizione")
        ]
        if changes:
            item = _new(f"lettore:{token}", "porte", "riporta", saved_item["label"], "Riporta il nome com'era")
            item.update(
                changes=changes,
                _token=token,
                _values={"name": saved_item["fields"]["Name"], "description": saved_item["fields"]["Description"]},
            )
            items.append(item)
    return items


# Applicazione


async def apply(items: list[dict], keys: list[str], runtime: Runtime, user: str) -> dict:
    """Applica le righe scelte nell'ordine di ALL_SECTIONS (dentro una sezione, quello dell'anteprima, che mette
    prima chi è usato da altri). Una riga che non riesce non ferma le altre."""
    order = list(ALL_SECTIONS)
    chosen = [i for i in items if i["key"] in set(keys)]
    chosen.sort(key=lambda i: order.index(i["section"]))
    result: dict = {"applied": [], "failed": [], "reload": False}
    for item in chosen:
        if item["blocked"]:
            result["failed"].append({"label": item["label"], "error": item["blocked"]})
            continue
        outcome: dict = {}
        try:
            if item["section"] in full_restore.FULL_SECTIONS:
                outcome = await full_restore.apply_item(item, runtime, user)
                # Porte, lettori e punti di accesso ricreati: il collegamento al controller riparte
                result["reload"] |= item["section"] != "tessere"
            elif item["section"] == "tipologie":
                await _apply_category(item, runtime, user)
            elif item["section"] == "persone":
                await _apply_person(item, runtime, user)
            else:
                result["reload"] |= await _apply_door(item, runtime, user)
        except (ServiceError, VapixError) as err:
            result["failed"].append({"label": item["label"], "error": str(err)})
            continue
        result["applied"].append({"label": item["label"], "text": outcome.get("text") or item["text"]})
        result["failed"] += outcome.get("failed", [])
    if renamed := runtime.cache.get("tokens"):
        result["renamed"] = len(renamed)
    present = {i["key"] for i in items}
    gone = [k for k in keys if k not in present]
    # Righe scelte nell'anteprima e sparite perché adesso il controller non risponde: non riuscite, non "già come
    # nel backup" (la riga bloccata che le copre dice il motivo)
    covers = {section: i for i in items if i["blocked"] for section in i.get("_covers", ())}
    lost = [k for k in gone if _section_of(k) in covers]
    for cover in {covers[_section_of(k)]["key"]: covers[_section_of(k)] for k in lost}.values():
        if cover["key"] not in keys:
            result["failed"].append({"label": cover["label"], "error": cover["blocked"]})
    result["unchanged"] = len(gone) - len(lost)
    return result


def _section_of(key: str) -> str:
    """Sezione di una riga del ripristino dalla sua chiave (per le righe che non sono più nel piano)."""
    if key.startswith("disp-"):
        return "dispositivo"
    if key.startswith("prog-"):
        return "controller"
    if key == "tessere":
        return "tessere"
    if key.startswith(("porta:", "lettore:")):
        return "porte"
    return ""


async def _apply_category(item: dict, runtime: Runtime, user: str) -> None:
    if item["action"] == "togli":
        await runtime.rules.delete_category(item["_target"], user)
        return
    fields = {**item["_fields"], "rules": _resolved_rules(item["_rules"], runtime.db)}
    if item["action"] == "ricrea":
        await runtime.rules.save_category(None, fields, user, new_id=item["_id"])
    else:
        await runtime.rules.save_category(item["_target"], fields, user)


def _resolved_rules(rules: list[dict], db: Database) -> list[dict]:
    """Regole con il numero di adesso di ogni porta, cercata dal token: può essere appena tornata nell'app."""
    doors = {(d["controller_id"], d["door_token"]): d["id"] for d in db.list_doors()}
    resolved = []
    for rule in rules:
        target_id = rule["target_id"]
        if rule.get("_door"):
            target_id = doors.get(tuple(rule["_door"]))
            if target_id is None:
                raise ServiceError(f"la porta {rule['_name']} non è nell'app: ripristinala insieme alla tipologia")
        resolved.append({"target": rule["target"], "target_id": target_id, "schedule": rule["schedule"]})
    return resolved


async def _apply_person(item: dict, runtime: Runtime, user: str) -> None:
    if item["key"].startswith("tessera-nuova:"):
        await _remove_card(item, runtime, user)
        return
    if item["action"] == "togli":
        await runtime.access.delete_person(item["_target"], user)
        return
    db = runtime.db
    category = db.get_category(item["_category_id"]) if item["_category_id"] else None
    if category is None and item["_category"]:
        # Tipologia ricreata da questo stesso ripristino
        category = next((c for c in db.list_categories() if c["name"].lower() == item["_category"].lower()), None)
    profiles = {p.get("token") for p in await runtime.access.controller.list_access_profiles()}
    cards = []
    for card in item["_cards"]:
        access = {k: card[k] for k in CARD_FIELDS}
        if card.get("_category_access"):
            # Profilo sparito: quello della tipologia, come detto nell'anteprima
            if not category or not category["group_token"]:
                if item["_category"]:
                    raise ServiceError(f"la tipologia {item['_category']} non è stata ricreata: ripristinala insieme")
                raise ServiceError(f"il profilo di accesso della tessera {card['number']} non c'è più sul controller")
            access["access_profile"] = category["group_token"]
        elif access["access_profile"] not in profiles:
            # Sparito dopo l'anteprima: mai un accesso diverso senza dirlo
            raise ServiceError(
                f"il profilo di accesso della tessera {card['number']} non c'è più sul controller: rifai l'anteprima"
            )
        cards.append({"number": card["number"], "enabled": card["enabled"], **access})
    person = item["_person"]
    current = db.get_person(person["id"])
    if current is not None and current["created_at"] == person["created_at"]:
        # Le tessere aggiunte dopo il backup restano com'erano: si eliminano solo con la loro riga
        wanted = {c["number"].upper() for c in cards}
        cards += [
            {"number": c["number"], "enabled": c["enabled"], **{k: c[k] for k in CARD_FIELDS}}
            for c in current["cards"]
            if c["number"].upper() not in wanted
        ]
    await runtime.access.restore_person(person, cards, category["id"] if category else 0, user)


async def _remove_card(item: dict, runtime: Runtime, user: str) -> None:
    """Tessera aggiunta dopo il backup, da eliminare: mai l'ultima della persona, che sparirebbe con lei."""
    card = runtime.db.card_by_number(item["_target"])
    if card is None:
        return
    if card["person_id"] != item["_owner"]:
        raise ServiceError(f"la tessera {card['number']} ora è di {card['person_name']}: resta sua")
    person = runtime.db.get_person(card["person_id"])
    if len(person["cards"]) < 2:
        raise ServiceError(f"è l'unica tessera di {person['name']}: la persona resterebbe senza tessere")
    await runtime.access.delete_card(card["id"], user)


async def _apply_door(item: dict, runtime: Runtime, user: str) -> bool:
    """True se sono cambiate le impostazioni della porta nell'app (serve riavviare il collegamento)."""
    if item["key"].startswith("lettore:"):
        await runtime.programming.update_reader(item["_token"], item["_values"], user)
        return False
    await runtime.programming.update_door(item["_token"], item["_values"], user)
    saved = item.get("_app")
    if not saved:
        return False
    current = next(
        (
            d
            for d in runtime.db.list_doors()
            if d["door_token"] == item["_token"] and d["controller_id"] == runtime.controller_id
        ),
        None,
    )
    fields = {k: saved[k] for k in Database.DOOR_FIELDS}
    if current and all(current[k] == fields[k] for k in Database.DOOR_FIELDS):
        return False
    runtime.db.save_door(current["id"] if current else None, runtime.controller_id, fields)
    runtime.db.add_audit(user, "porta ripristinata da backup", fields["name"])
    return True
