"""Notifiche dell'app: cosa va gestito, ordinato per gravità, con il numero sulla campanella.

Le notifiche non si salvano: si ricavano ogni volta da eventi recenti e stato dei collegamenti.
Eventi uguali (stessa persona o tessera, stessa porta, stesso motivo) a pochi minuti l'uno dall'altro
diventano una sola notifica con il numero dei tentativi.
Si salvano solo i tipi scelti nelle impostazioni e quelle già viste.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .db import Database
from .service import ServiceError

# Tipo → nome, descrizione, livello predefinito
TYPES = {
    "door_alarm": (
        "Allarmi delle Porte",
        "Porta forzata, aperta troppo a lungo, manomissione del lettore o del controller.",
        "urgent",
    ),
    "controller": ("Controller Non Raggiungibile", "Il controller è offline o non risponde all'add-on.", "urgent"),
    "mqtt": ("Collegamento MQTT Perso", "L'add-on non riceve più gli eventi dal broker MQTT.", "urgent"),
    "unknown_card": ("Tessere Sconosciute", "Tessere non registrate passate sul lettore.", "urgent"),
    "denied": (
        "Accessi Negati",
        "Accessi negati a persone registrate: fuori fascia, porta non autorizzata, tessera scaduta.",
        "urgent",
    ),
    "expiring": ("Tessere in Scadenza", "Persone con tessere che scadono nei prossimi tre giorni.", "urgent"),
    "camera": ("Telecamere Non Disponibili", "L'ultima foto di una telecamera delle porte non è riuscita.", "urgent"),
    "controller_changed": (
        "Controller Cambiato",
        "All'indirizzo del controller risponde un apparecchio diverso: scritture sospese finché non si conferma.",
        "urgent",
    ),
    "clock": (
        "Orologio del Controller",
        "L'ora del controller è sbagliata di oltre un minuto: le fasce orarie scattano all'ora sbagliata.",
        "urgent",
    ),
    "missing_cards": (
        "Tessere Non Presenti Sul Controller",
        "Tessere che l'app conosce ma che sul controller non ci sono più: da riscrivere o togliere dall'app.",
        "urgent",
    ),
}
# Guasti dell'impianto: restano finché non si risolvono, anche dopo la presa visione
FAULT_TYPES = ("controller", "mqtt", "camera", "controller_changed", "clock", "missing_cards")
# Tipi con nomi o eventi: permesso necessario per vederli
VISIBILITY = {
    "door_alarm": "registro",
    "denied": "registro",
    "unknown_card": "persone",
    "expiring": "persone",
    "missing_cards": "persone",
}
# Attiva: compare in Da Gestire e conta sulla campanella. "info" (fino alla 0.13) vale come attiva.
LEVELS = {"urgent": "Attiva", "off": "Disattivata"}
OLD_LEVELS = {"info": "urgent"}
RECENT = timedelta(hours=24)
EXPIRING = timedelta(days=3)
GROUP_WINDOW = timedelta(minutes=5)
MAX_EVENTS = 500
SEVERITY_ORDER = {"crit": 0, "warn": 1, "low": 2}
SETTING_LEVELS = "notifications"
SETTING_SEEN = "notifications_seen"


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


class NotificationService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # Impostazioni

    def levels(self) -> dict[str, str]:
        try:
            saved = json.loads(self.db.get_setting(SETTING_LEVELS) or "{}")
        except ValueError:
            saved = {}
        saved = {k: OLD_LEVELS.get(v, v) for k, v in saved.items()}
        return {k: saved.get(k) if saved.get(k) in LEVELS else default for k, (_, _, default) in TYPES.items()}

    def set_levels(self, data: dict, user: str) -> dict[str, str]:
        levels = self.levels()
        for key, level in data.items():
            if key not in TYPES:
                raise ServiceError(f"tipo di notifica sconosciuto: {key}")
            if level not in LEVELS:
                raise ServiceError(f"livello non valido: {level}")
            levels[key] = level
        self.db.set_setting(SETTING_LEVELS, json.dumps(levels))
        self.db.add_audit(
            user, "notifiche modificate", ", ".join(f"{TYPES[k][0]}: {LEVELS[v]}" for k, v in levels.items())
        )
        return levels

    def types(self) -> list[dict]:
        levels = self.levels()
        return [
            {"key": k, "name": name, "description": desc, "default": default, "level": levels[k]}
            for k, (name, desc, default) in TYPES.items()
        ]

    # Notifiche attive

    def collect(
        self,
        *,
        mqtt_configured: bool,
        mqtt_connected: bool,
        controller_configured: bool,
        controller_online: bool | None,
        sync: dict,
        door_names: dict[str, str],
        cameras: list[dict] | None = None,
        controllers: list[dict] | None = None,
        missing: dict | None = None,
        now: datetime | None = None,
    ) -> list[dict]:
        """Notifiche dei tipi attivi, dalla più grave e, a parità, dalla più recente."""
        now = now or datetime.now(tz=UTC)
        levels = self.levels()
        since = (now - RECENT).astimezone(UTC).isoformat(timespec="seconds")
        items: list[dict] = []

        def add(kind: str, item_id: str, severity: str, title: str, text: str, ts: str, **extra) -> None:
            if levels[kind] != "off":
                items.append(
                    {
                        "id": item_id,
                        "type": kind,
                        "level": levels[kind],
                        "severity": severity,
                        "title": title,
                        "text": text,
                        "ts": ts,
                        "first_ts": ts,
                        "count": 1,
                        "door": "",
                        "door_token": "",
                        "events": [],
                        "event": None,
                        "snapshot": None,
                        "snapshot_error": "",
                        "link": "",
                        **extra,
                    }
                )

        def door_of(event: dict) -> str:
            name = door_names.get(event.get("door") or "")
            if not name and event.get("kind") == "manomissione" and not event.get("door"):
                # Coperchio del controller: non riguarda una porta
                return event.get("source") or "Controller"
            if not name and len(door_names) == 1:
                name = next(iter(door_names.values()))
            return name or "porta non indicata"

        def add_group(kind: str, severity: str, title: str, text: str, group: list[dict], link: str = "") -> None:
            last = group[-1]
            shot = next((e for e in reversed(group) if e.get("snapshot")), None)
            add(
                kind,
                f"g{group[0]['id']}-{last['id']}",
                severity,
                f"{title} ×{len(group)}" if len(group) > 1 else title,
                text,
                last["ts"],
                first_ts=group[0]["ts"],
                count=len(group),
                door=door_of(last),
                door_token=last.get("door") or "",
                events=[e["id"] for e in group],
                event=last["id"],
                snapshot=shot["id"] if shot else None,
                snapshot_error="" if shot else last.get("snapshot_error") or "",
                person_id=last.get("person_id"),
                card=last.get("card") or "",
                link=link,
            )

        if controller_configured:
            if mqtt_configured and not mqtt_connected:
                add(
                    "mqtt",
                    "mqtt",
                    "crit",
                    "Collegamento MQTT Perso",
                    "Gli eventi del controller non arrivano all'add-on: registro e stato porta non si aggiornano.",
                    now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            controllers = controllers or []
            unreachable = [c for c in controllers if c.get("unreachable")]
            for c in unreachable:
                # Controllo periodico di ogni controller: non risponde da almeno un minuto
                add(
                    "controller",
                    f"controller:{c['id']}",
                    "crit",
                    "Controller Non Raggiungibile",
                    f"{c['name']} · {_capital(c.get('error') or 'non risponde')}",
                    c.get("checked_at") or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            reachable = bool(controllers) and not unreachable
            if mqtt_connected and controller_online is False and reachable:
                # Il controller risponde all'add-on ma non pubblica sul broker
                add(
                    "mqtt",
                    "mqtt:controller",
                    "crit",
                    "Eventi del Controller Non Arrivano",
                    "Il controller risponde ma non è collegato al broker MQTT: "
                    "registro e stato porta non si aggiornano.",
                    now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            elif not unreachable and (sync.get("ok") is False or (controller_online is False and not controllers)):
                detail = sync.get("error") if sync.get("ok") is False else "il controller risulta offline"
                add(
                    "controller",
                    "controller",
                    "crit",
                    "Controller Non Raggiungibile",
                    _capital(detail or ""),
                    sync.get("at") or now.isoformat(timespec="seconds"),
                    link="#impianti/stato",
                )
            for c in controllers:
                if c.get("identity_change"):
                    add(
                        "controller_changed",
                        f"changed:{c['id']}:{c.get('serial') or ''}",
                        "crit",
                        "Controller Cambiato",
                        _capital(c["identity_change"]) + ". Fino ad allora le modifiche non vengono scritte.",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link="#impianti",
                    )
                if c.get("clock_problem"):
                    offset = c["clock_offset"]
                    add(
                        "clock",
                        f"clock:{c['id']}",
                        "warn",
                        "Orologio del Controller",
                        f"{c['name']} · {_offset_text(offset)} rispetto a Home Assistant",
                        c.get("checked_at") or now.isoformat(timespec="seconds"),
                        link="#impianti",
                    )
        if missing and missing.get("count"):
            add(
                "missing_cards",
                f"missing:{missing['since']}",
                "crit" if missing.get("mass") else "warn",
                "Tessere Non Presenti Sul Controller",
                f"{missing['count']} tessere di {missing['persons']} persone non sono più sul controller: "
                "riscrivile o toglile dall'app",
                missing["since"],
                link="#persone/mancanti",
            )
        titles = {"allarme": "Allarme Porta", "manomissione": "Manomissione"}
        if levels["door_alarm"] != "off":
            alarms = self.db.list_events(kind="allarme,manomissione", since=since, limit=MAX_EVENTS)
            for group in _groups(alarms, lambda e: (e["kind"], e.get("door") or "", e.get("detail") or "")):
                last = group[-1]
                add_group("door_alarm", "crit", titles[last["kind"]], _capital(last.get("detail") or ""), group)
        if levels["denied"] != "off" or levels["unknown_card"] != "off":
            denied = self.db.list_events(kind="negato", since=since, limit=MAX_EVENTS)

            def who(e: dict) -> tuple:
                return e.get("person_id") or e.get("person_name") or e.get("card"), e.get("door"), e.get("detail")

            for group in _groups(denied, who):
                last = group[-1]
                reason = last.get("detail") or ""
                if last.get("person_id") or last.get("person_name"):
                    # Persona registrata: un tentativo è poco, più tentativi di fila meritano attenzione
                    text = " · ".join(x for x in (last.get("person_name") or "", reason) if x)
                    add_group("denied", "warn" if len(group) > 1 else "low", "Accesso Negato", text, group)
                else:
                    # Tessera non registrata: ripetuta è un tentativo insistito
                    text = f"Tessera {last['card']}" if last.get("card") else "Tessera non registrata"
                    severity = "crit" if len(group) > 1 else "warn"
                    add_group("unknown_card", severity, "Tessera Sconosciuta", text, group, "#da-registrare")
        for cam in cameras or []:
            # Telecamera di una porta con l'ultima foto non riuscita: sparisce alla prima foto buona
            add(
                "camera",
                f"cam:{cam['camera']}",
                "warn",
                "Telecamera Non Disponibile",
                f"{cam['name']} · {cam['error']}",
                cam["ts"],
                door=", ".join(cam.get("doors") or []),
                link="#impianti",
            )
        if levels["expiring"] != "off":
            for person in self.db.list_persons():
                ends = [e for e in (_parse(c.get("valid_to") or "") for c in person["cards"]) if e]
                end = min(ends) if ends else None
                if end and now < end <= now + EXPIRING:
                    add(
                        "expiring",
                        f"x{person['id']}:{end.isoformat()}",
                        "low",
                        "Tessera in Scadenza",
                        f"{person['name']} · scade il {end.astimezone():%d/%m %H:%M}",
                        (end - EXPIRING).isoformat(timespec="seconds"),
                        person_id=person["id"],
                        link=f"#persone/{person['id']}",
                    )
        items.sort(key=lambda i: (_parse(i["ts"]) or now).timestamp(), reverse=True)
        items.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
        return items

    # Già viste

    def seen(self) -> set[str]:
        try:
            return set(json.loads(self.db.get_setting(SETTING_SEEN) or "[]"))
        except ValueError:
            return set()

    def mark_seen(self, ids: list[str], current: list[dict]) -> None:
        """Segna come viste; dimentica quelle che non ci sono più, così se il problema torna si rivede."""
        active = {i["id"] for i in current}
        seen = (self.seen() | set(ids)) & active
        self.db.set_setting(SETTING_SEEN, json.dumps(sorted(seen)))

    def listing(self, current: list[dict], perms: set[str] | None = None) -> dict:
        """Notifiche per un utente: nomi, tessere ed eventi solo a chi ha il permesso di vederli."""
        seen = self.seen()
        active = {i["id"] for i in current}
        if seen - active:
            seen &= active
            self.db.set_setting(SETTING_SEEN, json.dumps(sorted(seen)))
        hidden = {k for k, perm in VISIBILITY.items() if perms is not None and perm not in perms}
        items = [{**i, "seen": i["id"] in seen} for i in current if i["type"] not in hidden]
        return {
            "items": items,
            "open": sum(1 for i in items if not i["seen"]),
            "types": self.types(),
            "levels": LEVELS,
        }


def _offset_text(seconds: float) -> str:
    """Scarto dell'orologio: "3 min avanti", "45 s indietro"."""
    way = "avanti" if seconds > 0 else "indietro"
    value = abs(seconds)
    if value >= 3600:
        return f"{value / 3600:.1f} ore {way}".replace(".", ",")
    if value >= 120:
        return f"{round(value / 60)} min {way}"
    return f"{round(value)} s {way}"


def _capital(text: str) -> str:
    return text[:1].upper() + text[1:]


def _groups(events: list[dict], key) -> list[list[dict]]:
    """Eventi uguali per la chiave, ciascuno entro GROUP_WINDOW dal precedente: dal più vecchio al più recente."""
    open_groups: dict = {}
    groups: list[list[dict]] = []
    dated = [(_parse(e.get("ts") or ""), e) for e in events]
    for at, event in sorted(dated, key=lambda d: (d[0].timestamp() if d[0] else 0, d[1]["id"])):
        k = key(event)
        group = open_groups.get(k)
        last = _parse(group[-1].get("ts") or "") if group else None
        if group and at and last and at - last <= GROUP_WINDOW:
            group.append(event)
        else:
            group = [event]
            open_groups[k] = group
            groups.append(group)
    return groups
