"""Porte che una tipologia apre secondo le sue righe di accesso (porta, area o struttura → fascia), calcolate solo dal
database: il profilo della tipologia sul controller e il «non abilitato» del registro partono dallo stesso calcolo."""

from __future__ import annotations

from collections.abc import Collection

from .db import Database
from .installation import InstallationService

# Porte del controller in uso, area di ogni porta e struttura di ogni area
Layout = tuple[list[dict], dict[int, int], dict[int, int]]


def places(db: Database, deleted_areas: Collection[int] = ()) -> Layout:
    """Porte del controller in uso, area di ogni porta e struttura di ogni area.
    deleted_areas: come sarebbero dopo l'eliminazione di quelle aree (porte e controller restano senza)."""
    installation = InstallationService(db)
    controllers = {c["id"]: {**c, "area_id": 0} if c["area_id"] in deleted_areas else c for c in db.list_controllers()}
    doors = [{**d, "area_id": 0} if d["area_id"] in deleted_areas else d for d in installation.doors()]
    door_area = {d["id"]: installation.door_area(d, controllers) for d in doors}
    area_site = {a["id"]: s["id"] for s in db.list_sites() for a in s["areas"] if a["id"] not in deleted_areas}
    return doors, door_area, area_site


def door_schedules(db: Database, rules: list[dict], layout: Layout | None = None) -> dict[str, list[str]]:
    """Porte coperte dalle righe, con le fasce di ognuna: area e struttura valgono per tutte le loro porte,
    anche quelle aggiunte dopo. layout: il risultato di places, se già letto."""
    doors, door_area, area_site = layout or places(db)
    result: dict[str, list[str]] = {}
    for door in doors:
        area = door_area.get(door["id"], 0)
        targets = {("door", door["id"]), ("area", area), ("site", area_site.get(area, 0))}
        for rule in rules:
            if (rule["target"], rule["target_id"]) in targets:
                schedules = result.setdefault(door["door_token"], [])
                if rule["schedule"] not in schedules:
                    schedules.append(rule["schedule"])
    return result


def category_doors(db: Database, category_id: int, layout: Layout | None = None) -> set[str]:
    """Token delle porte che la tipologia apre secondo le sue righe: senza righe nessuna."""
    rules = db.category_rules(category_id) if category_id else []
    return set(door_schedules(db, rules, layout)) if rules else set()
