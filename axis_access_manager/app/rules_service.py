"""Tipologie di tessera, gruppi (profili di accesso Axis), fasce orarie e festività."""

from __future__ import annotations

import copy
import logging

from .db import Database
from .installation import InstallationService
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
    "standard_after_hours": "Fuori Orario",
    "standard_weekends": "Fine Settimana",
}

ALWAYS = "standard_always"
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
        # Chi usa la fascia: le tipologie dei profili, o il nome del profilo se nessuna tipologia lo usa
        owners: dict[str, list[str]] = {}
        for category in self.db.list_categories():
            owners.setdefault(category["group_token"], []).append(category["name"])
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
                    "groups": [
                        name
                        for p in profiles
                        if token in profile_schedules(p)
                        for name in owners.get(p.get("token"), [p.get("Name") or p.get("token")])
                    ],
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

    async def reapply_holidays(self, only_changed: bool = False) -> int:
        """Riscrive le eccezioni delle fasce dell'add-on che escludono le festività.

        Con only_changed tocca solo le fasce il cui elenco di eccezioni sul controller non è quello atteso
        (ad esempio perché la scrittura era stata rifiutata da una versione precedente)."""
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
                body = self._schedule_body(
                    token, schedule.get("Name") or token, schedule.get("Description") or "", rule["rules"], True
                )
                if only_changed and schedule.get("ExceptionScheduleDefinition") == body["ExceptionScheduleDefinition"]:
                    continue
                await self.controller.set_schedule(body)
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
            "Schedule": [schedule],
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
        """La fascia va sia nel profilo sia in ogni regola: l'A1210 (fw 10.12) valuta anche quella del profilo,
        e con quella vuota non fa mai entrare nessuno."""
        for policy in profile.get("AccessPolicy") or []:
            policy["Schedule"] = [schedule]
        profile["Schedule"] = [schedule]
        return profile

    async def repair_group_schedules(self) -> int:
        """Gruppi salvati dalle versioni precedenti con la fascia solo nelle regole: la copia anche nel profilo.

        Tocca solo i profili senza fascia propria e con la stessa fascia in tutte le regole. Restituisce quanti.
        """
        repaired = 0
        async with self.lock:
            for profile in await self.controller.list_access_profiles():
                if profile.get("Schedule"):
                    continue
                tokens = {t for p in profile.get("AccessPolicy") or [] for t in p.get("Schedule") or []}
                if len(tokens) != 1:
                    continue
                fixed = copy.deepcopy(profile)
                fixed["Schedule"] = [next(iter(tokens))]
                await self.controller.set_access_profile(fixed)
                self.db.add_audit(
                    "sistema", "gruppo riparato", f"{profile.get('Name') or profile.get('token')}: fascia"
                )
                repaired += 1
        return repaired

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

    # Tipologie: chi è la persona, dove e quando entra. Ogni tipologia ha il suo profilo sul controller,
    # generato dalle righe Porta / Area / Struttura → Fascia, che l'app tiene aggiornato.

    @property
    def default_category(self) -> int:
        return int(self.db.get_setting("default_category") or 0)

    def list_categories(self) -> list[dict]:
        categories = self.db.list_categories()
        rules = self.db.all_category_rules()
        persons = self.db.list_persons()
        default = self.default_category
        for category in categories:
            category["rules"] = rules.get(category["id"], [])
            category["default"] = category["id"] == default
            members = [p for p in persons if p["category_id"] == category["id"]]
            category["custom"] = sum(1 for p in members if self._custom(p, category))
        return categories

    @staticmethod
    def _custom(person: dict, category: dict) -> bool:
        """Accesso diverso da quello della tipologia, deciso a mano o rimasto dalle versioni precedenti."""
        cards = person.get("cards") or []
        return bool(cards) and cards[0]["access_profile"] != category["group_token"]

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
            "valid_days": valid_days,
            "description": normalize_text(fields.get("description"), 200, "descrizione"),
        }

    def _places(self) -> tuple[list[dict], dict[int, int], dict[int, int]]:
        """Porte del controller in uso, area di ogni porta e struttura di ogni area."""
        installation = InstallationService(self.db)
        controllers = {c["id"]: c for c in self.db.list_controllers()}
        doors = installation.doors()
        door_area = {d["id"]: installation.door_area(d, controllers) for d in doors}
        area_site = {a["id"]: s["id"] for s in self.db.list_sites() for a in s["areas"]}
        return doors, door_area, area_site

    def _check_access_rules(self, rows: list[dict] | None) -> list[dict]:
        doors, _, area_site = self._places()
        targets = {
            "door": {d["id"] for d in doors},
            "area": set(area_site),
            "site": set(area_site.values()) | {s["id"] for s in self.db.list_sites()},
        }
        rules = []
        for row in rows or []:
            target, schedule = str(row.get("target") or ""), str(row.get("schedule") or "").strip()
            try:
                target_id = int(row.get("target_id") or 0)
            except (TypeError, ValueError) as err:
                raise ServiceError("riga di accesso non valida") from err
            if target not in targets:
                raise ServiceError("riga di accesso non valida: scegli porta, area o struttura")
            if target_id not in targets[target]:
                raise ServiceError("una riga di accesso indica una porta, un'area o una struttura che non esiste più")
            if not schedule:
                raise ServiceError("scegli la fascia oraria di ogni riga")
            rule = {"target": target, "target_id": target_id, "schedule": schedule}
            if rule not in rules:
                rules.append(rule)
        if not rules:
            raise ServiceError("aggiungi almeno una riga: dove e quando entra chi ha questa tipologia")
        return rules

    def _door_schedules(self, rules: list[dict]) -> dict[str, list[str]]:
        """Porte coperte dalle righe, con le fasce di ognuna: area e struttura valgono per tutte le loro porte,
        anche quelle aggiunte dopo."""
        doors, door_area, area_site = self._places()
        result: dict[str, list[str]] = {}
        for door in doors:
            area = door_area.get(door["id"], 0)
            places = {("door", door["id"]), ("area", area), ("site", area_site.get(area, 0))}
            for rule in rules:
                if (rule["target"], rule["target_id"]) in places:
                    schedules = result.setdefault(door["door_token"], [])
                    if rule["schedule"] not in schedules:
                        schedules.append(rule["schedule"])
        return result

    async def _build_profile(self, values: dict, rules: list[dict], base: dict | None) -> dict:
        """Profilo della tipologia: una regola per ogni punto di accesso con lettore delle porte coperte.
        Le regole su porte che l'app non gestisce restano come sono."""
        door_schedules = self._door_schedules(rules)
        points = await self.controller.list_access_points()
        owned = {r["access_point_token"] for r in self.db.all_controller_inputs() if r.get("access_point_token")}
        app_doors = {d["door_token"] for d in InstallationService(self.db).doors()}
        point_door = {p.get("token"): p.get("Entity") for p in points}
        policies = [
            policy
            for policy in (base or {}).get("AccessPolicy") or []
            if policy.get("AccessPoint") in point_door and point_door[policy.get("AccessPoint")] not in app_doors
        ]
        for point in points:
            schedules = door_schedules.get(point.get("Entity"))
            # Pulsanti e relè degli ingressi aprono senza tessera: non servono regole
            if not schedules or point.get("token") in owned or "REXOnly" in (point.get("AuthenticationProfile") or []):
                continue
            policy = {"AccessPoint": point["token"], "AuthorizationProfile": [], "Attribute": []}
            policies.append({**policy, "Schedule": list(schedules)})
        if not any(point_door.get(p["AccessPoint"]) in app_doors for p in policies):
            raise ServiceError("nelle porte scelte non c'è nessun lettore programmato sul controller")
        used = {t for p in policies for t in p["Schedule"]}
        profile = copy.deepcopy(base) if base else {}
        profile.setdefault("token", "")
        profile.update(
            {
                "Name": values["name"],
                "Description": values["description"] or f"Tipologia {values['name']} (Axis Access Manager)",
                "Enabled": True,
                "ValidFrom": profile.get("ValidFrom") or "",
                "ValidTo": profile.get("ValidTo") or "",
                "AuthenticationProfile": profile.get("AuthenticationProfile") or [],
                "Attribute": profile.get("Attribute") or [],
                # L'A1210 valuta anche la fascia del profilo: con fasce diverse per porta deve lasciar passare sempre
                "Schedule": [next(iter(used))] if len(used) == 1 else [ALWAYS],
                "AccessPolicy": policies,
            }
        )
        return profile

    def _profile_shared(self, token: str, category_id: int | None) -> bool:
        """Profilo usato anche da altre tipologie o da tessere di persone di altre tipologie."""
        if any(c["group_token"] == token and c["id"] != category_id for c in self.db.list_categories()):
            return True
        return any(
            p["category_id"] != category_id and any(c["access_profile"] == token for c in p["cards"])
            for p in self.db.list_persons()
        )

    async def _check_schedules(self, rules: list[dict]) -> None:
        tokens = {s.get("token") for s in await self.controller.list_schedules()}
        if any(r["schedule"] not in tokens for r in rules):
            raise ServiceError("una fascia oraria scelta non esiste più sul controller")

    async def _write_profile(self, profile: dict) -> str:
        result = await self.controller.set_access_profile(profile)
        token = profile.get("token") or result
        if not token:
            raise ServiceError("il controller non ha restituito il token del profilo della tipologia")
        return token

    def check_place_unused(self, target: str, target_id: int) -> None:
        """Una porta, un'area o una struttura nelle righe di una tipologia non si elimina."""
        rules = self.db.all_category_rules()
        places = {(target, target_id)}
        if target == "site":
            # Con la struttura se ne vanno anche le sue aree
            site = self.db.get_site(target_id)
            places |= {("area", a["id"]) for a in (site or {}).get("areas", [])}
        names = [
            c["name"]
            for c in self.db.list_categories()
            if any((r["target"], r["target_id"]) in places for r in rules.get(c["id"], []))
        ]
        if names:
            raise ServiceError(f"è usata dalle tipologie {', '.join(names)}: toglila prima dalle loro righe di accesso")

    async def save_category(self, category_id: int | None, fields: dict, user: str, new_id: int | None = None) -> dict:
        """Crea o modifica una tipologia e il suo profilo sul controller. Se il profilo era in comune con altri,
        la tipologia ne riceve uno suo e le sue persone passano al nuovo: gli altri non cambiano.
        new_id: per una tipologia rimessa da un backup, il numero che aveva (se è libero)."""
        values = self._category_fields(fields)
        rules = self._check_access_rules(fields.get("rules"))
        other = self.db.category_by_name(values["name"])
        if other and other["id"] != category_id:
            raise ServiceError(f"esiste già la tipologia {other['name']}")
        old = self.db.get_category(category_id) if category_id else None
        if category_id and old is None:
            raise ServiceError("tipologia non trovata")
        old_token = old["group_token"] if old else ""
        async with self.lock:
            await self._check_schedules(rules)
            base = None
            if old_token:
                found = await self.controller.get_access_profiles([old_token])
                base = found[0] if found else None
            shared = bool(old_token) and self._profile_shared(old_token, category_id)
            if base is not None and shared:
                base = {**copy.deepcopy(base), "token": ""}
            profile = await self._build_profile(values, rules, base)
            token = await self._write_profile(profile)
            category_id = self.db.save_category(category_id, {**values, "group_token": token}, new_id)
            self.db.set_category_rules(category_id, rules)
            if self.default_category == category_id:
                self.db.set_setting("default_group", token)
            action = "tipologia modificata" if old else "tipologia ricreata da backup" if new_id else "tipologia creata"
            self.db.add_audit(user, action, values["name"])
        moved = 0
        if old_token and old_token != token:
            for person in self.db.persons_of_category(category_id):
                cards = person["cards"]
                if cards and cards[0]["access_profile"] == old_token:
                    await self.access.set_person_access(
                        person["id"], token, cards[0]["valid_from"], cards[0]["valid_to"], user
                    )
                    moved += 1
        return {"id": category_id, "token": token, "updated": moved}

    async def delete_category(self, category_id: int, user: str) -> None:
        category = self.db.get_category(category_id)
        if category is None:
            raise ServiceError("tipologia non trovata")
        if category_id == self.default_category:
            raise ServiceError("è la tipologia predefinita: scegline prima un'altra come predefinita")
        if self.db.persons_of_category(category_id):
            raise ServiceError("ci sono persone con questa tipologia: spostale prima in un'altra")
        token = category["group_token"]
        async with self.lock:
            if token and not self._profile_shared(token, category_id) and token != self.access.default_group:
                try:
                    await self.controller.remove_access_profile(token)
                except VapixError as err:
                    _LOGGER.warning("Profilo %s della tipologia non rimosso: %s", token, err)
            self.db.delete_category(category_id)
            self.db.add_audit(user, "tipologia eliminata", category["name"])

    def set_default_category(self, category_id: int, user: str) -> None:
        category = self.db.get_category(category_id)
        if category is None or not category["group_token"]:
            raise ServiceError("tipologia non trovata")
        self.db.set_setting("default_category", str(category_id))
        self.db.set_setting("default_group", category["group_token"])
        self.db.add_audit(user, "tipologia predefinita cambiata", category["name"])

    async def align_person(self, person_id: int, user: str) -> dict:
        """Riporta la persona all'accesso della sua tipologia."""
        person = self.db.get_person(person_id)
        if person is None:
            raise ServiceError("persona non trovata")
        category = self.db.get_category(person["category_id"]) if person["category_id"] else None
        if category is None or not category["group_token"]:
            raise ServiceError("la persona non ha una tipologia")
        cards = person["cards"]
        first = cards[0] if cards else {"valid_from": "", "valid_to": ""}
        return await self.access.set_person_access(
            person_id, category["group_token"], first["valid_from"], first["valid_to"], user
        )

    async def refresh_category_profiles(self) -> int:
        """Dopo una modifica a porte, aree o strutture: riscrive i profili delle tipologie che cambiano.
        Salta i profili in comune, che si separano alla prossima modifica della tipologia."""
        changed = 0
        rules = self.db.all_category_rules()
        async with self.lock:
            for category in self.db.list_categories():
                token = category["group_token"]
                if not token or not rules.get(category["id"]) or self._profile_shared(token, category["id"]):
                    continue
                found = await self.controller.get_access_profiles([token])
                if not found:
                    continue
                try:
                    profile = await self._build_profile(category, rules[category["id"]], found[0])
                except ServiceError as err:
                    _LOGGER.warning("Profilo della tipologia %s non aggiornato: %s", category["name"], err)
                    continue
                if _policy_sig(profile) == _policy_sig(found[0]):
                    continue
                await self.controller.set_access_profile(profile)
                self.db.add_audit("sistema", "tipologia aggiornata", f"{category['name']}: porte di aree e strutture")
                changed += 1
        return changed

    async def adopt_groups(self) -> int:
        """Dalle versioni con gruppi e tipologie separati, senza cambiare chi entra dove:
        - una tipologia senza gruppo prende quello predefinito, e le righe di accesso dal suo profilo;
        - chi non ha tipologia entra in quella del suo gruppo, creata con lo stesso nome se manca;
        - il gruppo predefinito diventa la tipologia predefinita.
        Restituisce quante persone o tipologie ha sistemato."""
        default = self.access.default_group
        profiles = {p.get("token"): p for p in await self.controller.list_access_profiles()}
        points = {p.get("token"): p.get("Entity") for p in await self.controller.list_access_points()}
        fixed = 0
        for category in self.db.list_categories():
            if not category["group_token"] and default:
                self.db.save_category(category["id"], {**category, "group_token": default})
                fixed += 1
        rules = self.db.all_category_rules()
        for category in self.db.list_categories():
            if category["id"] not in rules and category["group_token"] in profiles:
                self.db.set_category_rules(category["id"], self._rules_of(profiles[category["group_token"]], points))
                fixed += 1
        for person in self.db.list_persons():
            if person["category_id"]:
                continue
            token = person["cards"][0]["access_profile"] if person["cards"] else default
            if not token:
                continue
            category = self._category_for(token, profiles, points)
            self.db.set_person_category(person["id"], category["id"])
            fixed += 1
        if not self.default_category and default:
            category = self._category_for(default, profiles, points)
            self.db.set_setting("default_category", str(category["id"]))
            fixed += 1
        if fixed:
            self.db.add_audit("sistema", "gruppi passati alle tipologie", f"{fixed} modifiche")
        return fixed

    def _category_for(self, token: str, profiles: dict[str, dict], points: dict[str, str]) -> dict:
        """Tipologia che usa il profilo, creata con il nome del gruppo se non c'è."""
        profile = profiles.get(token) or {}
        name = normalize_name(profile.get("Name") or token)[:40]
        # Si cerca la tipologia nata da quel gruppo: un'altra con lo stesso profilo dice chi è un'altra persona
        names = {name.lower(), f"{name[:33]} (gruppo)".lower()}
        found = next(
            (c for c in self.db.list_categories() if c["group_token"] == token and c["name"].lower() in names), None
        )
        if found:
            return found
        if self.db.category_by_name(name):
            name = f"{name[:33]} (gruppo)"
        values = {
            "name": name,
            "color": COLORS[len(self.db.list_categories()) % len(COLORS)],
            "group_token": token,
            "valid_days": 0,
            "description": normalize_text(profile.get("Description"), 200, "descrizione"),
        }
        category_id = self.db.save_category(None, values)
        if profile:
            self.db.set_category_rules(category_id, self._rules_of(profile, points))
        return self.db.get_category(category_id)

    def _rules_of(self, profile: dict, points: dict[str, str]) -> list[dict]:
        """Righe di accesso lette da un profilo esistente: una per porta dell'app, con la sua fascia."""
        doors = {d["door_token"]: d["id"] for d in InstallationService(self.db).doors()}
        rules: list[dict] = []
        for policy in profile.get("AccessPolicy") or []:
            door = doors.get(points.get(policy.get("AccessPoint"), ""))
            schedule = next(iter(policy.get("Schedule") or profile.get("Schedule") or []), "")
            rule = {"target": "door", "target_id": door, "schedule": schedule}
            if door and schedule and rule not in rules:
                rules.append(rule)
        return rules


def _policy_sig(profile: dict) -> tuple:
    policies = profile.get("AccessPolicy") or []
    return (
        tuple(profile.get("Schedule") or []),
        tuple(sorted((p.get("AccessPoint"), tuple(p.get("Schedule") or [])) for p in policies)),
    )
