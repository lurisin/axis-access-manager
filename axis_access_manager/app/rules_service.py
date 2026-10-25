"""Tipologie di tessera, gruppi (profili di accesso Axis), fasce orarie e festività."""

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
    parse_schedule_ical,
    schedule_ical,
)
from .service import AccessService, ServiceError, normalize_name, normalize_text
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

STANDARD_NAMES = {
    "standard_always": "Sempre",
    "standard_office_hours": "Orario d'Ufficio",
    "standard_weekends": "Fine Settimana",
}

COLORS = ["blue", "green", "orange", "red", "purple", "teal", "pink", "gray"]


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
        hidden = self.db.hidden("schedule")
        tokens = {s.get("token") for s in schedules}
        for token in set(rules) - tokens:
            # Fascia eliminata dal controller con un altro strumento
            self.db.delete_schedule_rules(token)
        result = []
        for schedule in schedules:
            token = schedule.get("token", "")
            managed = rules.get(token)
            parsed = managed["rules"] if managed else parse_schedule_ical(schedule.get("ScheduleDefinition"))
            result.append(
                {
                    "token": token,
                    "name": (None if managed else STANDARD_NAMES.get(token)) or schedule.get("Name") or token,
                    "description": schedule.get("Description") or "",
                    "standard": token.startswith(STANDARD_PREFIX),
                    "managed": managed is not None,
                    "hidden": token in hidden,
                    "rules": parsed or [],
                    "readable": parsed is not None,
                    "holidays": managed["holidays"] if managed else bool(schedule.get("ExceptionScheduleDefinition")),
                    "summary": describe_rules(parsed) if parsed else "",
                    "groups": [p.get("Name") or p.get("token") for p in profiles if token in profile_schedules(p)],
                }
            )
        result.sort(key=lambda s: (s["hidden"], s["name"].lower()))
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
        async with self.lock:
            # Anche le fasce predefinite o create con altri strumenti: da qui in poi le gestisce l'add-on
            if token not in {s.get("token") for s in await self.controller.list_schedules()}:
                raise ServiceError("fascia oraria non trovata sul controller")
            await self.controller.set_schedule(self._schedule_body(token, name, description, rules, holidays))
            self.db.save_schedule_rules(token, rules, holidays)
            self.db.add_audit(user, "fascia oraria modificata", f"{name}: {describe_rules(rules)}")
        return {"token": token}

    async def delete_schedule(self, token: str, user: str) -> None:
        async with self.lock:
            profiles = await self.controller.list_access_profiles()
            users = [p.get("Name") or p.get("token") for p in profiles if token in profile_schedules(p)]
            if users:
                raise ServiceError(f"fascia usata dai gruppi {', '.join(users)}: cambia prima la loro fascia")
            await self.controller.remove_schedule(token)
            self.db.delete_schedule_rules(token)
            self.db.set_hidden("schedule", token, False)
            self.db.add_audit(user, "fascia oraria eliminata", token)

    def set_hidden(self, kind: str, token: str, hidden: bool, user: str) -> None:
        """Nasconde dalle liste un gruppo o una fascia che non serve, senza toccare il controller."""
        if kind not in ("group", "schedule"):
            raise ServiceError("elemento sconosciuto")
        if hidden and kind == "group" and token == self.access.default_group:
            raise ServiceError("il gruppo predefinito non si può nascondere: scegline prima un altro")
        self.db.set_hidden(kind, token, hidden)
        what = "gruppo" if kind == "group" else "fascia oraria"
        self.db.add_audit(user, f"{what} {'nascosto' if hidden else 'mostrato'}", token)

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
        hidden = self.db.hidden("group")
        default = self.access.default_group
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
                    "default": token == default,
                    "hidden": token in hidden and token != default,
                    "valid_from": profile.get("ValidFrom") or "",
                    "valid_to": profile.get("ValidTo") or "",
                    "access_points": [
                        p.get("AccessPoint") for p in profile.get("AccessPolicy") or [] if p.get("AccessPoint")
                    ],
                }
            )
        groups.sort(key=lambda g: (not g["default"], g["hidden"], g["name"].lower()))
        return {"groups": groups, "schedules": schedules, "categories": self.list_categories()}

    async def _template_profile(self, schedule: str) -> dict:
        """Nuovo profilo copiato da quello predefinito (stessa porta), o costruito dai punti di accesso."""
        if self.access.default_group:
            found = await self.controller.get_access_profiles([self.access.default_group])
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
        if token == self.access.default_group:
            raise ServiceError("è il gruppo predefinito: scegline prima un altro come predefinito")
        used_by = [c["name"] for c in self.db.list_categories() if c["group_token"] == token]
        if used_by:
            raise ServiceError(f"gruppo usato dalle tipologie {', '.join(used_by)}: cambia prima il loro gruppo")
        persons = self.db.profile_usage().get(token, 0)
        if persons:
            raise ServiceError(f"gruppo assegnato a {persons} persone: spostale prima in un altro gruppo")
        async with self.lock:
            await self.controller.remove_access_profile(token)
            self.db.set_hidden("group", token, False)
            self.db.add_audit(user, "gruppo eliminato", token)

    async def set_default_group(self, token: str, user: str) -> None:
        tokens = {p.get("token") for p in await self.controller.list_access_profiles()}
        if token not in tokens:
            raise ServiceError("gruppo non trovato sul controller")
        self.db.set_setting("default_group", token)
        self.db.set_hidden("group", token, False)
        self.db.add_audit(user, "gruppo predefinito cambiato", token)

    # Tipologie di tessera

    def list_categories(self) -> list[dict]:
        return self.db.list_categories()

    def _category_fields(self, fields: dict) -> dict:
        name = normalize_name(fields.get("name", ""))
        if len(name) > 40:
            raise ServiceError("nome della tipologia troppo lungo (massimo 40 caratteri)")
        color = fields.get("color") or COLORS[0]
        if color not in COLORS:
            raise ServiceError("colore non valido")
        try:
            valid_days = int(fields.get("valid_days") or 0)
        except (TypeError, ValueError) as err:
            raise ServiceError("durata non valida") from err
        if not 0 <= valid_days <= 3650:
            raise ServiceError("durata tra 0 (illimitata) e 3650 giorni")
        return {
            "name": name,
            "color": color,
            "group_token": (fields.get("group_token") or "").strip(),
            "valid_days": valid_days,
            "description": normalize_text(fields.get("description"), 200, "descrizione"),
        }

    async def _check_group(self, token: str) -> None:
        if token and token not in {p.get("token") for p in await self.controller.list_access_profiles()}:
            raise ServiceError("il gruppo scelto non esiste più sul controller")

    async def save_category(self, category_id: int | None, fields: dict, user: str, apply: bool = False) -> dict:
        """Crea o modifica una tipologia; con apply il nuovo gruppo vale subito per tutte le sue persone."""
        values = self._category_fields(fields)
        other = self.db.category_by_name(values["name"])
        if other and other["id"] != category_id:
            raise ServiceError(f"esiste già la tipologia {other['name']}")
        old = self.db.get_category(category_id) if category_id else None
        if category_id and old is None:
            raise ServiceError("tipologia non trovata")
        if not old or old["group_token"] != values["group_token"]:
            await self._check_group(values["group_token"])
        category_id = self.db.save_category(category_id, values)
        self.db.add_audit(user, "tipologia modificata" if old else "tipologia creata", values["name"])
        updated = 0
        if apply and values["group_token"]:
            for person in self.db.persons_of_category(category_id):
                cards = person["cards"]
                if cards and cards[0]["access_profile"] != values["group_token"]:
                    first = cards[0]
                    await self.access.set_person_access(
                        person["id"], values["group_token"], first["valid_from"], first["valid_to"], user
                    )
                    updated += 1
        return {"id": category_id, "updated": updated}

    def delete_category(self, category_id: int, user: str) -> None:
        category = self.db.get_category(category_id)
        if category is None:
            raise ServiceError("tipologia non trovata")
        self.db.delete_category(category_id)
        self.db.add_audit(user, "tipologia eliminata", category["name"])
