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

# Sezioni che si possono ripristinare, nell'ordine in cui si applicano: le tipologie prima delle persone
RESTORE_SECTIONS = {"tipologie": "Tipologie", "persone": "Persone e Tessere", "porte": "Porte e Lettori"}
# Con il ripristino completo di un controller: prima impostazioni e programmazione, le tessere per ultime
ALL_SECTIONS = {
    "dispositivo": full_restore.FULL_SECTIONS["dispositivo"],
    "controller": full_restore.FULL_SECTIONS["controller"],
    **RESTORE_SECTIONS,
    "tessere": full_restore.FULL_SECTIONS["tessere"],
}
CARD_FIELDS = ("access_profile", "valid_from", "valid_to")


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


def _changes(current: dict | None, saved: dict) -> list[dict]:
    """Campi diversi: com'è adesso → come tornerà."""
    if current is None:
        return []
    names = list(dict.fromkeys([*saved["fields"], *current["fields"]]))
    return [
        {"label": FIELD_LABELS.get(f, f), "old": _shown(current, f), "new": _shown(saved, f)}
        for f in names
        if current["fields"].get(f) != saved["fields"].get(f)
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
) -> list[dict]:
    """Righe dell'anteprima per le sezioni scelte. Le chiavi che iniziano con _ servono solo al ripristino."""
    items: list[dict] = []
    full = [s for s in full_restore.FULL_SECTIONS if s in sections]
    if full and controller_id:
        items += full_restore.plan(saved, current, full, controller_id, mqtt_users)
    saved_common, current_common = saved.get(COMMON) or {}, current.get(COMMON) or {}
    saved_ctx, current_ctx = context_of(saved), context_of(current)
    if "tipologie" in sections:
        items += _plan_categories(saved_common, current_common, saved_ctx, current_ctx, current)
    if "persone" in sections:
        items += _plan_persons(saved_common, current_common, saved_ctx, current_ctx)
    if "porte" in sections:
        key = f"controller:{controller_id}"
        if controller_id and key in saved and key in current:
            items += _plan_doors(saved[key], current[key], saved_ctx, current_ctx)
        elif controller_id and key in saved:
            items.append(
                {
                    **_new("porte", "porte", "riporta", "Porte e lettori", "Ripristina porte e lettori"),
                    "blocked": "il controller non risponde: porte e lettori si ripristinano quando torna raggiungibile",
                }
            )
    return items


def public(items: list[dict]) -> list[dict]:
    return [{k: v for k, v in item.items() if not k.startswith("_")} for item in items]


# Tipologie


def _plan_categories(saved: dict, current: dict, saved_ctx: dict, current_ctx: dict, current_parts: dict) -> list[dict]:
    saved_view, current_view = (
        common_view(saved, saved_ctx)["tipologie"],
        common_view(current, current_ctx)["tipologie"],
    )
    by_id = {c["id"]: c for c in current.get("categories", [])}
    by_name = {c["name"].lower(): c for c in current.get("categories", [])}
    doors = {door["id"] for key, part in current_parts.items() if key != COMMON for door in part.get("app_doors", [])}
    areas = {a["id"] for s in current.get("sites", []) for a in s.get("areas", [])}
    sites = {s["id"] for s in current.get("sites", [])}
    schedules = {s["token"] for s in current.get("schedules", [])}
    default = int((current.get("settings") or {}).get("default_category") or 0)
    used = {p["category_id"] for p in current.get("persons", [])}
    items, matched = [], set()
    for category in saved.get("categories", []):
        now = by_id.get(category["id"]) or by_name.get(category["name"].lower())
        key = f"tipologia:{category['id']}"
        saved_item = saved_view[str(category["id"])]
        fields = {k: category[k] for k in ("name", "color", "valid_days", "description", "rules")}
        if now is not None:
            matched.add(now["id"])
            changes = _changes(current_view[str(now["id"])], saved_item)
            if changes:
                item = _new(key, "tipologie", "riporta", category["name"], "Riporta com'era", _target=now["id"])
                item.update(changes=changes, _fields=fields)
                items.append(item)
            continue
        item = _new(
            key, "tipologie", "ricrea", category["name"], "Ricrea la tipologia", _fields=fields, _id=category["id"]
        )
        item["changes"] = [{"label": "Regole", "old": "—", "new": rules_label(saved_item)}]
        missing = [
            r
            for r in category["rules"]
            if r["schedule"] not in schedules
            or r["target_id"] not in {"door": doors, "area": areas, "site": sites}.get(r["target"], set())
        ]
        if missing:
            item["blocked"] = "alcune regole indicano porte, aree o fasce orarie che non esistono più"
        items.append(item)
    for category in current.get("categories", []):
        if category["id"] in matched:
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


def _card_item(card: dict, ctx: dict) -> dict:
    return _item(
        card["number"],
        {"enabled": card["enabled"], **{k: card[k] for k in CARD_FIELDS}},
        {
            "enabled": "attiva" if card["enabled"] else "sospesa",
            "access_profile": ctx["profiles"].get(card["access_profile"], card["access_profile"] or "—"),
        },
    )


def _plan_persons(saved: dict, current: dict, saved_ctx: dict, current_ctx: dict) -> list[dict]:
    saved_view, current_view = common_view(saved, saved_ctx)["persone"], common_view(current, current_ctx)["persone"]
    current_persons = {(p["id"], p["created_at"]): p for p in current.get("persons", [])}
    current_cards = {c["number"].upper(): c for c in current.get("cards", [])}
    current_names = {p["id"]: p["name"] for p in current.get("persons", [])}
    category_names = {c["name"].lower() for c in current.get("categories", [])}
    saved_cards: dict[int, list[dict]] = {}
    for card in saved.get("cards", []):
        saved_cards.setdefault(card["person_id"], []).append(card)
    items, matched = [], set()
    for person in saved.get("persons", []):
        ident = (person["id"], person["created_at"])
        now = current_persons.get(ident)
        key = f"persona:{person['id']}:{person['created_at']}"
        item = _new(key, "persone", "riporta" if now else "ricrea", person["name"], "")
        category = saved_ctx["categories"].get(person["category_id"], "") if person["category_id"] else ""
        if category and category.lower() not in category_names:
            item["warnings"].append(
                f"la tipologia {category} non c'è più: ripristinala insieme, "
                "altrimenti la persona resta senza tipologia"
            )
        cards = []
        for card in saved_cards.get(person["id"], []):
            owner = current_cards.get(card["number"].upper())
            if owner and (now is None or owner["person_id"] != now["id"]):
                item["warnings"].append(
                    f"la tessera {card['number']} ora è di "
                    f"{current_names.get(owner['person_id'], 'un altro')}: resta sua"
                )
                continue
            cards.append(card)
        item["_person"], item["_cards"], item["_category"] = person, cards, category
        if now is None:
            item["text"] = "Ricrea la persona"
            item["changes"] = [
                {"label": "Tessere", "old": "—", "new": ", ".join(c["number"] for c in cards) or "—"},
                *([{"label": "Tipologia", "old": "—", "new": category}] if category else []),
            ]
            if not cards:
                item["blocked"] = "tutte le sue tessere ora sono di altre persone"
            items.append(item)
            continue
        matched.add(ident)
        changes = _changes(current_view[f"{now['id']}:{now['created_at']}"], saved_view[f"{ident[0]}:{ident[1]}"])
        mine = {c["number"].upper(): c for c in current.get("cards", []) if c["person_id"] == now["id"]}
        wanted = {c["number"].upper(): c for c in cards}
        for number, card in wanted.items():
            if number not in mine:
                changes.append({"label": f"Tessera {card['number']}", "old": "—", "new": "rimessa"})
                continue
            for change in _changes(_card_item(mine[number], current_ctx), _card_item(card, saved_ctx)):
                changes.append({**change, "label": f"Tessera {card['number']}: {change['label'].lower()}"})
        for number, card in mine.items():
            if number not in wanted:
                changes.append(
                    {"label": f"Tessera {card['number']}", "old": "c'è", "new": "tolta (aggiunta dopo il backup)"}
                )
        if changes:
            item["text"] = "Riporta com'era"
            item["changes"] = changes
            if not cards:
                item["blocked"] = "tutte le sue tessere ora sono di altre persone"
            items.append(item)
    for person in current.get("persons", []):
        if (person["id"], person["created_at"]) in matched:
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
    items = []
    for door in saved.get("doors", []):
        token = door.get("token", "")
        saved_item = saved_view["porte"][token]
        item = _new(f"porta:{token}", "porte", "riporta", saved_item["label"], "Riporta com'era")
        if token not in current_doors:
            item["blocked"] = "non è più programmata sul controller: ricreala con il ripristino completo"
            items.append(item)
            continue
        current_item = current_view["porte"][token]
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
        changes = _changes(current_item, saved_item)
        if token not in current_app and token in saved_app:
            changes.append({"label": "Gestita dall'App", "old": "no", "new": "sì"})
        if changes:
            item.update(changes=changes, _token=token, _values=values, _app=saved_app.get(token))
            items.append(item)
    for token, saved_item in saved_view["lettori"].items():
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
    gone = [k for k in keys if k not in {i["key"] for i in items}]
    result["unchanged"] = len(gone)
    return result


async def _apply_category(item: dict, runtime: Runtime, user: str) -> None:
    if item["action"] == "togli":
        await runtime.rules.delete_category(item["_target"], user)
    elif item["action"] == "ricrea":
        await runtime.rules.save_category(None, item["_fields"], user, new_id=item["_id"])
    else:
        await runtime.rules.save_category(item["_target"], item["_fields"], user)


async def _apply_person(item: dict, runtime: Runtime, user: str) -> None:
    if item["action"] == "togli":
        await runtime.access.delete_person(item["_target"], user)
        return
    db = runtime.db
    categories = {c["name"].lower(): c for c in db.list_categories()}
    category = categories.get((item["_category"] or "").lower())
    profiles = {p.get("token") for p in await runtime.access.controller.list_access_profiles()}
    cards = []
    for card in item["_cards"]:
        access = {k: card[k] for k in CARD_FIELDS}
        if access["access_profile"] not in profiles:
            # Profilo sparito (tipologia ricreata o eliminata): quello della tipologia di adesso
            fallback = (category or {}).get("group_token") or runtime.access.default_group
            if not fallback:
                raise ServiceError(f"il profilo della tessera {card['number']} non c'è più sul controller")
            access["access_profile"] = fallback
        cards.append({"number": card["number"], "enabled": card["enabled"], **access})
    await runtime.access.restore_person(item["_person"], cards, category["id"] if category else 0, user)


async def _apply_door(item: dict, runtime: Runtime, user: str) -> bool:
    """True se sono cambiate le impostazioni della porta nell'app (serve riavviare il collegamento)."""
    if item["key"].startswith("lettore:"):
        await runtime.programming.update_reader(item["_token"], item["_values"], user)
        return False
    await runtime.programming.update_door(item["_token"], item["_values"], user)
    saved = item.get("_app")
    if not saved:
        return False
    current = next((d for d in runtime.db.list_doors() if d["door_token"] == item["_token"]), None)
    fields = {k: saved[k] for k in Database.DOOR_FIELDS}
    if current and all(current[k] == fields[k] for k in Database.DOOR_FIELDS):
        return False
    runtime.db.save_door(current["id"] if current else None, runtime.controller_id, fields)
    runtime.db.add_audit(user, "porta ripristinata da backup", fields["name"])
    return True
