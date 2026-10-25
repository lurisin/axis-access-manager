"""Notifiche dell'app: cosa richiede attenzione, con il numero sulla campanella per quelle urgenti.

Le notifiche non si salvano: si ricavano ogni volta da eventi recenti e stato dei collegamenti.
Si salvano solo i tipi scelti nelle impostazioni e quelle già viste.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from .db import Database
from .service import ServiceError

# Tipo → nome, descrizione, livello predefinito
TYPES = {
    "door_alarm": ("Allarmi delle Porte", "Porta forzata, aperta troppo a lungo o manomissione del lettore.", "urgent"),
    "controller": ("Controller Non Raggiungibile", "Il controller è offline o non risponde all'add-on.", "urgent"),
    "mqtt": ("Collegamento MQTT Perso", "L'add-on non riceve più gli eventi dal broker MQTT.", "urgent"),
    "unknown_card": ("Tessere Sconosciute", "Tessere lette ma non ancora registrate.", "info"),
    "expiring": ("Tessere in Scadenza", "Persone con tessere che scadono nei prossimi tre giorni.", "info"),
    "denied": ("Accessi Negati", "Ogni accesso negato delle ultime 24 ore: utile solo con poco traffico.", "off"),
}
# Tipi con nomi o eventi: permesso necessario per vederli
VISIBILITY = {"door_alarm": "registro", "denied": "registro", "unknown_card": "persone", "expiring": "persone"}
LEVELS = {"urgent": "Urgente", "info": "Informativa", "off": "Disattivata"}
RECENT = timedelta(hours=24)
EXPIRING = timedelta(days=3)
MAX_EVENTS = 50
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
        now: datetime | None = None,
    ) -> list[dict]:
        """Notifiche dei tipi attivi, dalla più recente."""
        now = now or datetime.now(tz=UTC)
        levels = self.levels()
        since = (now - RECENT).astimezone(UTC).isoformat(timespec="seconds")
        items: list[dict] = []

        def add(kind: str, item_id: str, title: str, text: str, ts: str, link: str = "") -> None:
            if levels[kind] != "off":
                items.append(
                    {
                        "id": item_id,
                        "type": kind,
                        "level": levels[kind],
                        "title": title,
                        "text": text,
                        "ts": ts,
                        "link": link,
                    }
                )

        def door_of(event: dict) -> str:
            name = door_names.get(event.get("door") or "")
            if not name and len(door_names) == 1:
                name = next(iter(door_names.values()))
            return name or "porta non indicata"

        if controller_configured:
            if mqtt_configured and not mqtt_connected:
                add(
                    "mqtt",
                    "mqtt",
                    "Collegamento MQTT Perso",
                    "Gli eventi del controller non arrivano all'add-on.",
                    now.isoformat(timespec="seconds"),
                    "#impianti/stato",
                )
            if controller_online is False or sync.get("ok") is False:
                detail = sync.get("error") if sync.get("ok") is False else "il controller risulta offline"
                add(
                    "controller",
                    "controller",
                    "Controller Non Raggiungibile",
                    detail or "",
                    sync.get("at") or now.isoformat(timespec="seconds"),
                    "#impianti/stato",
                )
        titles = {"allarme": "Allarme Porta", "manomissione": "Manomissione"}
        for event in self.db.list_events(kind="allarme,manomissione", since=since, limit=MAX_EVENTS):
            text = " · ".join(x for x in (door_of(event), event.get("detail") or "") if x)
            add("door_alarm", f"e{event['id']}", titles[event["kind"]], text, event["ts"], "#registro")
        if levels["denied"] != "off":
            for event in self.db.list_events(kind="negato", since=since, limit=MAX_EVENTS):
                who = event.get("person_name") or event.get("card") or "tessera"
                text = " · ".join(x for x in (who, door_of(event), event.get("detail") or "") if x)
                add("denied", f"e{event['id']}", "Accesso Negato", text, event["ts"], "#registro")
        for card in self.db.list_unknown_cards():
            text = f"Tessera {card['number']}, letta {card['count']} " + ("volta" if card["count"] == 1 else "volte")
            add(
                "unknown_card",
                f"u{card['number']}:{card['last_seen']}",
                "Tessera Sconosciuta",
                text,
                card["last_seen"],
                "#da-registrare",
            )
        if levels["expiring"] != "off":
            for person in self.db.list_persons():
                ends = [e for e in (_parse(c.get("valid_to") or "") for c in person["cards"]) if e]
                end = min(ends) if ends else None
                if end and now < end <= now + EXPIRING:
                    add(
                        "expiring",
                        f"x{person['id']}:{end.isoformat()}",
                        "Tessera in Scadenza",
                        f"{person['name']} · scade il {end.astimezone():%d/%m %H:%M}",
                        (end - EXPIRING).isoformat(timespec="seconds"),
                        f"#persone/{person['id']}",
                    )
        items.sort(key=lambda i: _parse(i["ts"]) or now, reverse=True)
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
            "urgent": sum(1 for i in items if i["level"] == "urgent" and not i["seen"]),
            "types": self.types(),
            "levels": LEVELS,
        }
