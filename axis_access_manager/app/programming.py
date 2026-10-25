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

    def _device_uuids(self, points: list[dict]) -> tuple[dict[str, str], str]:
        """Dispositivo (DeviceUUID) di ogni lettore, preso dai punti di accesso o, se il lettore ora è
        scollegato da tutto, dallo storico delle modifiche; il secondo valore è per i lettori mai visti.
        """
        objects = points + self.db.list_controller_change_objects(self._check(), "access_point")
        known: dict[str, str] = {}
        for item in objects:
            for device in item.get("IdPointDevice") or []:
                if device.get("IdPoint") and device.get("DeviceUUID"):
                    known.setdefault(device["IdPoint"], device["DeviceUUID"])
        # Lettori mai collegati: stesso dispositivo degli altri (i lettori sono del controller stesso)
        return known, next(iter(known.values()), "")

    @staticmethod
    def _devices(readers: list[str], current: list[dict], known: dict[str, str], fallback: str) -> list[dict]:
        """Voci IdPointDevice per i lettori scelti; chi non ha il dispositivo lo riprende da known."""
        by_reader = {d.get("IdPoint"): d for d in current}
        return [
            by_reader[r]
            if (by_reader.get(r) or {}).get("DeviceUUID")
            else {"IdPoint": r, "DeviceUUID": known.get(r) or fallback}
            for r in readers
        ]

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
                known, fallback = self._device_uuids(await self.controller.list_access_points())
                after["IdPointDevice"] = self._devices(chosen, before.get("IdPointDevice") or [], known, fallback)
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

    async def set_links(self, links: list[dict], user: str) -> int:
        """Collegamenti lettore → porta dallo schema del controller, salvati tutti insieme.

        Sull'Axis un collegamento è un lettore dentro un punto di accesso esistente della porta, con la sua
        direzione: qui si spostano solo i lettori tra i punti di accesso, senza crearne o eliminarne.
        Restituisce quanti punti di accesso sono cambiati.
        """
        self._check()
        async with self.lock:
            points = await self.controller.list_access_points()
            readers = {r.get("token") for r in await self.controller.list_idpoints()}
            doors = {d.get("token"): d.get("Name") or d.get("token") for d in await self.controller.door_info_list()}
            points = [p for p in points if p.get("Entity") in doors]
            known, fallback = self._device_uuids(points)
            wanted: dict[str, list[str]] = {p["token"]: [] for p in points}
            enabled: dict[str, bool] = {}
            for link in links:
                reader, door = link.get("reader") or "", link.get("door") or ""
                direction = link.get("direction") or ""
                if reader not in readers:
                    raise ServiceError(f"lettore non trovato sul controller: {reader}")
                if door not in doors:
                    raise ServiceError(f"porta non trovata sul controller: {door}")
                if direction and direction not in DIRECTIONS:
                    raise ServiceError("direzione non valida")
                choices = [p for p in points if p.get("Entity") == door and _attribute(p, "Direction") == direction]
                if not choices:
                    where = DIRECTIONS.get(direction, "senza direzione").lower()
                    raise ServiceError(
                        f"sul controller la porta {doors[door]} non ha un punto di accesso in {where}: "
                        "scegli una direzione che la porta ha già"
                    )
                point = next((p for p in choices if reader in wanted[p["token"]]), None) or next(
                    (p for p in choices if any(i.get("IdPoint") == reader for i in p.get("IdPointDevice") or [])),
                    choices[0],
                )
                if reader not in wanted[point["token"]]:
                    wanted[point["token"]].append(reader)
                enabled[point["token"]] = enabled.get(point["token"], True) and link.get("enabled", True) is not False
            updates = []
            for point in points:
                token = point["token"]
                before_readers = [i.get("IdPoint") for i in point.get("IdPointDevice") or []]
                after = copy.deepcopy(point)
                current = point.get("IdPointDevice") or []
                # Stessi lettori: si salva comunque se a uno manca il dispositivo e lo si conosce
                order = before_readers if set(before_readers) == set(wanted[token]) else wanted[token]
                devices = self._devices(order, current, known, fallback)
                if devices != current:
                    after["IdPointDevice"] = devices
                if token in enabled and (point.get("Enabled", True) is not False) != enabled[token]:
                    after["Enabled"] = enabled[token]
                if after != point:
                    gains = bool(set(wanted[token]) - set(before_readers))
                    updates.append((not gains, token, point, after))
            # Prima i punti che ricevono lettori, poi quelli che li perdono: un errore a metà non lascia
            # un lettore scollegato da tutto
            for _, token, before, after in sorted(updates, key=lambda u: u[0]):
                names = ", ".join(i.get("IdPoint") for i in after.get("IdPointDevice") or []) or "nessun lettore"
                door_name = doors.get(after.get("Entity"), after.get("Entity"))
                await self._save(
                    "access_point", token, before, after, f"collegamenti di {door_name} ({token}): {names}", user
                )
            return len(updates)

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
