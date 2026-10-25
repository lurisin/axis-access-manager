"""Programmazione del controller dall'app: nomi e tempi delle porte, lettori, collegamento lettore-porta.

Ogni scrittura legge prima l'oggetto dal controller, cambia solo i campi scelti e salva la versione
precedente: dall'app si può annullare la modifica. Nessun comando di sblocco passa da qui.
"""

from __future__ import annotations

import copy
import logging
import re

from .db import Database
from .service import ServiceError, normalize_name, normalize_text
from .vapix import A1210Client, VapixError

_LOGGER = logging.getLogger(__name__)

DURATION_PATTERN = re.compile(r"^P(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$")

# Tempi della porta: campo Axis → (chiave nell'app, minimo, massimo in secondi)
DOOR_TIMES = {
    "AccessTime": ("access_time", 1, 600),
    "ExtendedAccessTime": ("extended_access_time", 1, 3600),
    "OpenTooLongTime": ("open_too_long_time", 1, 3600),
    "ExtendedOpenTooLongTime": ("extended_open_too_long_time", 1, 7200),
    "PreAlarmTime": ("pre_alarm_time", 0, 3600),
}
DIRECTIONS = {"in": "Ingresso", "out": "Uscita"}


def parse_duration(value: str | None) -> float | None:
    """Durata xs:duration (PT10S, PT1M30S) in secondi; None se assente o non riconosciuta."""
    if not value:
        return None
    match = DURATION_PATTERN.match(str(value).strip())
    if not match:
        return None
    hours, minutes, seconds = (float(x) if x else 0.0 for x in match.groups())
    total = hours * 3600 + minutes * 60 + seconds
    return int(total) if total == int(total) else total


def format_duration(seconds: float) -> str:
    return f"PT{int(seconds)}S" if seconds == int(seconds) else f"PT{seconds}S"


def _configuration(items: list[dict], token: str) -> list[dict]:
    for item in items:
        if item.get("token") == token:
            return [
                {"name": str(c.get("Name", "")), "value": str(c.get("Value", ""))}
                for c in item.get("Configuration") or []
            ]
    return []


def _attribute(item: dict, name: str) -> str:
    for attribute in item.get("Attribute") or []:
        if attribute.get("Name") == name:
            return str(attribute.get("Value") or "")
    return ""


class ProgrammingService:
    def __init__(self, db: Database, controller: A1210Client, controller_id: int | None, lock) -> None:
        self.db = db
        self.controller = controller
        self.controller_id = controller_id
        self.lock = lock

    def _check(self) -> int:
        if not self.controller_id:
            raise ServiceError("configura prima il controller in Impianti")
        return self.controller_id

    async def read(self) -> dict:
        """Porte, lettori e punti di accesso così come sono programmati sul controller."""
        self._check()
        errors: list[str] = []

        async def safe(label: str, call, default=None):
            try:
                return await call()
            except VapixError as err:
                errors.append(f"{label}: {err}")
                return default if default is not None else []

        doors = await safe("porte", self.controller.list_doors)
        if not doors:
            # Firmware che non permette GetDoorList: almeno nomi e token
            doors = await safe("porte", self.controller.door_info_list)
        door_configs = await safe("configurazione porte", self.controller.list_door_configurations)
        readers = await safe("lettori", self.controller.list_idpoints)
        reader_configs = await safe("configurazione lettori", self.controller.list_idpoint_configurations)
        points = await safe("punti di accesso", self.controller.list_access_points)
        reader_names = {r.get("token"): r.get("Name") or r.get("token") for r in readers}
        door_names = {d.get("token"): d.get("Name") or d.get("token") for d in doors}
        return {
            "doors": [
                {
                    "token": d.get("token", ""),
                    "name": d.get("Name") or "",
                    "description": d.get("Description") or "",
                    "editable": "AccessTime" in d,
                    **{key: parse_duration(d.get(field)) for field, (key, _, _) in DOOR_TIMES.items()},
                    "configuration": _configuration(door_configs, d.get("token", "")),
                }
                for d in doors
            ],
            "readers": [
                {
                    "token": r.get("token", ""),
                    "name": r.get("Name") or "",
                    "description": r.get("Description") or "",
                    "location": r.get("Location") or "",
                    "timeout": parse_duration(r.get("Timeout")),
                    "configuration": _configuration(reader_configs, r.get("token", "")),
                }
                for r in readers
            ],
            "access_points": [
                {
                    "token": a.get("token", ""),
                    "name": a.get("Name") or "",
                    "description": a.get("Description") or "",
                    "door": a.get("Entity") or "",
                    "door_name": door_names.get(a.get("Entity"), a.get("Entity") or ""),
                    "readers": [i.get("IdPoint") for i in a.get("IdPointDevice") or [] if i.get("IdPoint")],
                    "reader_names": [
                        reader_names.get(i.get("IdPoint"), i.get("IdPoint"))
                        for i in a.get("IdPointDevice") or []
                        if i.get("IdPoint")
                    ],
                    "direction": _attribute(a, "Direction"),
                    "enabled": a.get("Enabled", True) is not False,
                }
                for a in points
            ],
            "directions": DIRECTIONS,
            "changes": self.db.list_controller_changes(self.controller_id),
            "errors": errors,
        }

    async def _save(self, kind: str, token: str, before: dict, after: dict, summary: str, user: str) -> None:
        if before == after:
            return
        setter = {"door": self.controller.set_door, "reader": self.controller.set_idpoint}.get(
            kind, self.controller.set_access_point
        )
        await setter(after)
        self.db.add_controller_change(user, self.controller_id, kind, token, summary, before, after)
        self.db.add_audit(user, "programmazione controller", summary)

    async def update_door(self, token: str, data: dict, user: str) -> None:
        self._check()
        async with self.lock:
            found = await self.controller.get_doors([token])
            if not found:
                raise ServiceError("porta non trovata sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            after["Name"] = normalize_name(data.get("name", before.get("Name", "")))[:64]
            after["Description"] = normalize_text(
                data.get("description", before.get("Description")), 1024, "descrizione"
            )
            for field, (key, low, high) in DOOR_TIMES.items():
                if data.get(key) is None or field not in before:
                    continue
                try:
                    seconds = float(data[key])
                except (TypeError, ValueError) as err:
                    raise ServiceError(f"tempo non valido: {data[key]}") from err
                if not low <= seconds <= high:
                    raise ServiceError(f"{key.replace('_', ' ')}: tra {low} e {high} secondi")
                after[field] = format_duration(seconds)
            await self._save("door", token, before, after, f"porta {after['Name']}", user)

    async def update_reader(self, token: str, data: dict, user: str) -> None:
        self._check()
        async with self.lock:
            found = await self.controller.get_idpoints([token])
            if not found:
                raise ServiceError("lettore non trovato sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            after["Name"] = normalize_name(data.get("name", before.get("Name", "")))[:64]
            after["Description"] = normalize_text(
                data.get("description", before.get("Description")), 1024, "descrizione"
            )
            if "location" in data:
                after["Location"] = normalize_text(data.get("location"), 64, "posizione")
            await self._save("reader", token, before, after, f"lettore {after['Name']}", user)

    async def update_access_point(self, token: str, data: dict, user: str) -> None:
        """Nome, porta comandata, lettori collegati, direzione e attivazione di un punto di accesso."""
        self._check()
        async with self.lock:
            found = await self.controller.get_access_points([token])
            if not found:
                raise ServiceError("punto di accesso non trovato sul controller")
            before = found[0]
            after = copy.deepcopy(before)
            if "name" in data:
                after["Name"] = normalize_name(data["name"])[:64]
            if "enabled" in data:
                after["Enabled"] = bool(data["enabled"])
            if data.get("door"):
                doors = {d.get("token") for d in await self.controller.door_info_list()}
                if data["door"] not in doors:
                    raise ServiceError("porta non trovata sul controller")
                after["Entity"] = data["door"]
                after["EntityType"] = before.get("EntityType") or "axtdc:Door"
            if "readers" in data:
                readers = {r.get("token") for r in await self.controller.list_idpoints()}
                chosen = [r for r in data["readers"] if r]
                if any(r not in readers for r in chosen):
                    raise ServiceError("lettore non trovato sul controller")
                devices = {i.get("IdPoint"): i for i in before.get("IdPointDevice") or []}
                after["IdPointDevice"] = [devices.get(r) or {"IdPoint": r, "DeviceUUID": ""} for r in chosen]
            if "direction" in data:
                direction = data["direction"] or ""
                if direction and direction not in DIRECTIONS:
                    raise ServiceError("direzione non valida")
                attributes = [a for a in after.get("Attribute") or [] if a.get("Name") != "Direction"]
                if direction:
                    attributes.append({"Name": "Direction", "Value": direction})
                after["Attribute"] = attributes
            await self._save(
                "access_point", token, before, after, f"punto di accesso {after.get('Name') or token}", user
            )

    async def undo(self, change_id: int, user: str) -> None:
        """Riporta l'oggetto com'era prima della modifica."""
        self._check()
        change = self.db.get_controller_change(change_id)
        if change is None or change["controller_id"] != self.controller_id:
            raise ServiceError("modifica non trovata")
        if change["undone"]:
            raise ServiceError("modifica già annullata")
        async with self.lock:
            setter = {"door": self.controller.set_door, "reader": self.controller.set_idpoint}.get(
                change["kind"], self.controller.set_access_point
            )
            await setter(change["before"])
            self.db.mark_change_undone(change_id)
            self.db.add_audit(user, "programmazione controller annullata", change["summary"])
