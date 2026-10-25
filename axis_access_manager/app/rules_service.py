"""Gruppi (profili di accesso Axis), fasce orarie e festività scritti sul controller."""

from __future__ import annotations

import copy
import logging

from .db import Database
from .rules import (
    STANDARD_PREFIX,
    RuleError,
    describe_rules,
    holidays_ical,
    italian_holidays,
    normalize_holiday,
    normalize_rules,
    schedule_ical,
)
from .service import AccessService, ServiceError, normalize_name, normalize_text
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

STANDARD_NAMES = {
    "standard_always": "Sempre",
    "standard_office_hours": "Orario d'ufficio",
    "standard_weekends": "Fine settimana",
}


def profile_schedules(profile: dict) -> list[str]:
    """Fasce orarie usate da un profilo, nell'ordine in cui compaiono."""
    tokens: list[str] = []
    for token in profile.get("Schedule") or []:
        if token not in tokens:
            tokens.append(token)
    for policy in profile.get("AccessPolicy") or []:
        for token in policy.get("Schedule") or []:
            if token not in tokens:
                tokens.append(token)
    return tokens


class RulesService:
    def __init__(self, access: AccessService) -> None:
        self.access = access
        self.db: Database = access.db
        self.controller = access.controller
        self.settings = access.settings

    @property
    def lock(self):
        return self.access.lock

    # Fasce orarie

    async def list_schedules(self, profiles: list[dict] | None = None) -> list[dict]:
        schedules = await self.controller.list_schedules()
        if profiles is None:
            profiles = await self._profiles_or_empty()
        rules = self.db.schedule_rules()
        tokens = {s.get("token") for s in schedules}
        for token in set(rules) - tokens:
            # Fascia eliminata dal controller con un altro strumento
            self.db.delete_schedule_rules(token)
        result = []
        for schedule in schedules:
            token = schedule.get("token", "")
            managed = rules.get(token)
            result.append(
                {
                    "token": token,
                    "name": STANDARD_NAMES.get(token) or schedule.get("Name") or token,
                    "description": schedule.get("Description") or "",
                    "standard": token.startswith(STANDARD_PREFIX),
                    "managed": managed is not None,
                    "rules": managed["rules"] if managed else [],
                    "holidays": managed["holidays"] if managed else bool(schedule.get("ExceptionScheduleDefinition")),
                    "summary": describe_rules(managed["rules"]) if managed else "",
                    "groups": [p.get("Name") or p.get("token") for p in profiles if token in profile_schedules(p)],
                }
            )
        result.sort(key=lambda s: (not s["standard"], s["name"].lower()))
        return result

    async def _profiles_or_empty(self) -> list[dict]:
        try:
            return await self.controller.list_access_profiles()
        except VapixError as err:
            _LOGGER.warning("Gruppi non leggibili: %s", err)
            return []

    def _schedule_body(self, token: str, name: str, description: str, rules: list[dict], holidays: bool) -> dict:
        return {
            "token": token,
            "Name": name,
            "Description": description,
            "ScheduleDefinition": schedule_ical(rules),
            "ExceptionScheduleDefinition": holidays_ical(self.db.list_holidays()) if holidays else None,
            "Attribute": [],
        }

    @staticmethod
    def _check_rules(rules: list[dict]) -> list[dict]:
        try:
            return normalize_rules(rules)
        except RuleError as err:
            raise ServiceError(str(err)) from err

    async def create_schedule(self, name: str, description: str, rules: list[dict], holidays: bool, user: str) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        rules = self._check_rules(rules)
        async with self.lock:
            token = await self.controller.set_schedule(self._schedule_body("", name, description, rules, holidays))
            if not token:
                raise ServiceError("il controller non ha restituito il token della nuova fascia oraria")
            self.db.save_schedule_rules(token, rules, holidays)
            self.db.add_audit(user, "fascia oraria creata", f"{name}: {describe_rules(rules)}")
        return {"token": token}

    async def update_schedule(
        self, token: str, name: str, description: str, rules: list[dict], holidays: bool, user: str
    ) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        rules = self._check_rules(rules)
        if token.startswith(STANDARD_PREFIX):
            raise ServiceError("le fasce predefinite del controller non si modificano: creane una nuova")
        if token not in self.db.schedule_rules():
            raise ServiceError("fascia creata fuori dall'add-on: per cambiarla creane una nuova e assegnala ai gruppi")
        async with self.lock:
            await self.controller.set_schedule(self._schedule_body(token, name, description, rules, holidays))
            self.db.save_schedule_rules(token, rules, holidays)
            self.db.add_audit(user, "fascia oraria modificata", f"{name}: {describe_rules(rules)}")
        return {"token": token}

    async def delete_schedule(self, token: str, user: str) -> None:
        if token.startswith(STANDARD_PREFIX):
            raise ServiceError("le fasce predefinite del controller non si eliminano")
        async with self.lock:
            profiles = await self.controller.list_access_profiles()
            users = [p.get("Name") or p.get("token") for p in profiles if token in profile_schedules(p)]
            if users:
                raise ServiceError(f"fascia usata dai gruppi {', '.join(users)}: cambia prima la loro fascia")
            await self.controller.remove_schedule(token)
            self.db.delete_schedule_rules(token)
            self.db.add_audit(user, "fascia oraria eliminata", token)

    # Festività

    async def add_holidays(self, holidays: list[dict], user: str) -> None:
        cleaned = []
        for holiday in holidays:
            try:
                day, name = normalize_holiday(holiday.get("day", ""), holiday.get("name", ""))
            except RuleError as err:
                raise ServiceError(str(err)) from err
            cleaned.append((day, name, bool(holiday.get("yearly"))))
        for day, name, yearly in cleaned:
            self.db.add_holiday(day, name, yearly)
        self.db.add_audit(user, "festività aggiunte", ", ".join(f"{d} {n}".strip() for d, n, _ in cleaned))
        await self.reapply_holidays()

    async def add_italian_holidays(self, year: int, user: str) -> None:
        await self.add_holidays(italian_holidays([year, year + 1]), user)

    async def delete_holiday(self, day: str, user: str) -> None:
        self.db.delete_holiday(day)
        self.db.add_audit(user, "festività eliminata", day)
        await self.reapply_holidays()

    async def reapply_holidays(self) -> int:
        """Riscrive le eccezioni delle fasce dell'add-on che escludono le festività."""
        managed = {t: r for t, r in self.db.schedule_rules().items() if r["holidays"]}
        if not managed:
            return 0
        async with self.lock:
            schedules = {s.get("token"): s for s in await self.controller.list_schedules()}
            count = 0
            for token, rule in managed.items():
                schedule = schedules.get(token)
                if schedule is None:
                    continue
                await self.controller.set_schedule(
                    self._schedule_body(
                        token, schedule.get("Name") or token, schedule.get("Description") or "", rule["rules"], True
                    )
                )
                count += 1
            return count

    # Gruppi

    async def list_groups(self) -> dict:
        profiles = await self.controller.list_access_profiles()
        schedules = await self.list_schedules(profiles)
        names = {s["token"]: s["name"] for s in schedules}
        usage = self.db.profile_usage()
        groups = []
        for profile in profiles:
            token = profile.get("token", "")
            schedule_tokens = profile_schedules(profile)
            groups.append(
                {
                    "token": token,
                    "name": profile.get("Name") or token,
                    "description": profile.get("Description") or "",
                    "enabled": profile.get("Enabled", True) is not False,
                    "schedules": schedule_tokens,
                    "schedule_names": [names.get(t, t) for t in schedule_tokens],
                    "persons": usage.get(token, 0),
                    "default": token == self.settings.access_profile,
                    "valid_from": profile.get("ValidFrom") or "",
                    "valid_to": profile.get("ValidTo") or "",
                }
            )
        groups.sort(key=lambda g: (not g["default"], g["name"].lower()))
        return {"groups": groups, "schedules": schedules}

    async def _template_profile(self, schedule: str) -> dict:
        """Nuovo profilo copiato da quello predefinito (stessa porta), o costruito dai punti di accesso."""
        if self.settings.access_profile:
            found = await self.controller.get_access_profiles([self.settings.access_profile])
            if found and found[0].get("AccessPolicy"):
                profile = copy.deepcopy(found[0])
                profile["token"] = ""
                return self._with_schedule(profile, schedule)
        points = [p for p in await self.controller.list_access_points() if p.get("Entity") == self.settings.door_token]
        if not points:
            raise ServiceError("nessun punto di accesso trovato per la porta configurata")
        return {
            "token": "",
            "Name": "",
            "Description": "",
            "Enabled": True,
            "Schedule": [],
            "ValidFrom": "",
            "ValidTo": "",
            "AuthenticationProfile": [],
            "Attribute": [],
            "AccessPolicy": [
                {"AccessPoint": p["token"], "AuthorizationProfile": [], "Attribute": [], "Schedule": [schedule]}
                for p in points
            ],
        }

    @staticmethod
    def _with_schedule(profile: dict, schedule: str) -> dict:
        for policy in profile.get("AccessPolicy") or []:
            policy["Schedule"] = [schedule]
        if profile.get("Schedule"):
            profile["Schedule"] = [schedule]
        return profile

    async def _check_schedule(self, schedule: str) -> None:
        if not schedule:
            raise ServiceError("scegli una fascia oraria")
        tokens = {s.get("token") for s in await self.controller.list_schedules()}
        if schedule not in tokens:
            raise ServiceError("la fascia oraria scelta non esiste più sul controller")

    async def create_group(self, name: str, description: str, schedule: str, user: str) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        async with self.lock:
            await self._check_schedule(schedule)
            profile = await self._template_profile(schedule)
            profile["Name"] = name
            profile["Description"] = description
            token = await self.controller.set_access_profile(profile)
            if not token:
                raise ServiceError("il controller non ha restituito il token del nuovo gruppo")
            self.db.add_audit(user, "gruppo creato", f"{name} ({schedule})")
        return {"token": token}

    async def update_group(self, token: str, name: str, description: str, schedule: str, user: str) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        if token == self.settings.access_profile:
            raise ServiceError("il gruppo predefinito delle opzioni non si modifica da qui: creane un altro")
        async with self.lock:
            await self._check_schedule(schedule)
            found = await self.controller.get_access_profiles([token])
            if not found:
                raise ServiceError("gruppo non trovato sul controller")
            profile = self._with_schedule(found[0], schedule)
            profile["Name"] = name
            profile["Description"] = description
            await self.controller.set_access_profile(profile)
            self.db.add_audit(user, "gruppo modificato", f"{name} ({schedule})")
        return {"token": token}

    async def delete_group(self, token: str, user: str) -> None:
        if token == self.settings.access_profile:
            raise ServiceError("il gruppo predefinito delle opzioni non si elimina")
        persons = self.db.profile_usage().get(token, 0)
        if persons:
            raise ServiceError(f"gruppo assegnato a {persons} persone: spostale prima in un altro gruppo")
        async with self.lock:
            await self.controller.remove_access_profile(token)
            self.db.add_audit(user, "gruppo eliminato", token)
