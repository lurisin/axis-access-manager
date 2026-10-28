"""Tipologie di tessera, gruppi (profili di accesso Axis), fasce orarie e festività."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Collection

from .db import Database
from .installation import InstallationService
from .programming import REX_PROFILE_NAME, REX_ROLES, is_rex_profile
from .rules import (
    DAYS,
    ITALIAN_HOLIDAYS,
    STANDARD_PREFIX,
    RuleError,
    describe_rules,
    holiday_label,
    holidays_ical,
    italian_holidays,
    normalize_holiday,
    normalize_rules,
    parse_schedule_ical,
    schedule_ical,
)
from .service import AccessService, ServiceError, normalize_name, normalize_text, plural
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

STANDARD_NAMES = {
    "standard_always": "Sempre",
    "standard_office_hours": "Orario d'Ufficio",
    "standard_after_hours": "Fuori Orario",
    "standard_weekends": "Fine Settimana",
}

ALWAYS = "standard_always"
# Sempre fa da fascia del profilo quando le porte hanno fasce diverse: deve lasciar passare sempre
ALWAYS_RULES = [{"days": list(DAYS), "start": "00:00", "end": "24:00"}]
COLORS = ["blue", "green", "orange", "red", "purple", "teal", "pink", "gray"]
# Profili lasciati da una tipologia (+ id) con persone ancora da spostare: si riprende al salvataggio successivo
MOVING_PREFIX = "category_moving:"
# Il 4 ottobre (festa nazionale dal 2026) si aggiunge una volta sola a chi aveva già le festività nazionali
SETTING_OCT4 = "holidays_oct4"


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
        # Nomi dell'ultimo elenco di fasce e gruppi: set_hidden non legge il controller
        self._names: dict[tuple[str, str], str] = {}

    @property
    def lock(self):
        return self.access.lock

    # Fasce orarie

    async def list_schedules(self, profiles: list[dict] | None = None) -> list[dict]:
        schedules = await self.controller.list_schedules()
        if profiles is None:
            profiles = await self._profiles_or_empty()
        # Le regole di una fascia che il controller non ha più (dopo un reset) restano: tornano buone quando il
        # Ripristino Completo la ricrea con lo stesso token. Si tolgono solo eliminando la fascia dall'app.
        rules = self.db.schedule_rules()
        hidden = self.db.hidden("schedule")
        users = self._schedule_users(profiles)
        result = []
        for schedule in schedules:
            token = schedule.get("token", "")
            managed = rules.get(token)
            parsed = managed["rules"] if managed else parse_schedule_ical(schedule.get("ScheduleDefinition"))
            used = users.get(token) or _no_users()
            result.append(
                {
                    "token": token,
                    "name": _schedule_name(schedule, managed is not None),
                    "description": schedule.get("Description") or "",
                    "standard": token.startswith(STANDARD_PREFIX),
                    "managed": managed is not None,
                    # Fascia dei profili con fasce diverse per porta: resta sempre valida e non si elimina
                    "always": token == ALWAYS,
                    "hidden": token in hidden,
                    "rules": parsed or [],
                    "readable": parsed is not None,
                    "holidays": managed["holidays"] if managed else bool(schedule.get("ExceptionScheduleDefinition")),
                    "summary": describe_rules(parsed) if parsed else "",
                    # Chi la usa: le tipologie (righe di accesso o profilo), gli altri profili, gli ingressi
                    "groups": [
                        *used["categories"],
                        *used["profiles"],
                        *([REX_PROFILE_NAME] if used["inputs"] else []),
                    ],
                }
            )
            self._names["schedule", token] = result[-1]["name"]
        result.sort(key=lambda s: (s["hidden"], s["name"].lower()))
        return result

    def _schedule_users(self, profiles: list[dict]) -> dict[str, dict]:
        """Per ogni fascia: le tipologie che la citano nelle righe di accesso o nel profilo, gli altri profili del
        controller e se la usano gli ingressi (pulsanti e relè, nell'app o nel loro profilo)."""
        users: dict[str, dict] = {}

        def add(token: str, kind: str, name: str) -> None:
            names = users.setdefault(token, _no_users())[kind]
            if name not in names:
                names.append(name)

        categories = self.db.list_categories()
        rules = self.db.all_category_rules()
        owners: dict[str, list[str]] = {}
        for category in categories:
            owners.setdefault(category["group_token"], []).append(category["name"])
            for rule in rules.get(category["id"], []):
                add(rule["schedule"], "categories", category["name"])
        for profile in profiles:
            for token in profile_schedules(profile):
                if is_rex_profile(profile):
                    users.setdefault(token, _no_users())["inputs"] = True
                elif profile.get("token") in owners:
                    for name in owners[profile["token"]]:
                        add(token, "categories", name)
                else:
                    add(token, "profiles", profile.get("Name") or profile.get("token"))
        for row in self.db.all_controller_inputs():
            if row.get("schedule") and row.get("role") in REX_ROLES:
                users.setdefault(row["schedule"], _no_users())["inputs"] = True
        return users

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
        if token == ALWAYS and (rules != ALWAYS_RULES or holidays):
            # L'A1210 valuta la fascia del profilo insieme a quella della regola: ristretta, toglierebbe accessi
            # anche dove la riga della tipologia o l'ingresso dicono un'altra fascia
            raise ServiceError(
                "questa fascia deve restare valida tutti i giorni 00:00–24:00, anche nei festivi: l'app la usa "
                "nei profili delle tipologie con fasce diverse per porta e per i pulsanti. "
                "Per un altro orario crea una nuova fascia"
            )
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
            schedule = next((s for s in await self.controller.list_schedules() if s.get("token") == token), None)
            name = _schedule_name(schedule or {"token": token}, token in self.db.schedule_rules())
            if token == ALWAYS:
                raise ServiceError(
                    f"la fascia {name} non si può eliminare: l'app la usa nei profili delle tipologie con fasce "
                    "diverse per porta e per i pulsanti"
                )
            # In uso anche se è solo nelle righe di una tipologia (area ancora senza porte, lettore non programmato)
            users = self._schedule_users(await self.controller.list_access_profiles()).get(token)
            if users and (users["categories"] or users["profiles"] or users["inputs"]):
                raise ServiceError(_schedule_in_use(users))
            await self.controller.remove_schedule(token)
            self.db.delete_schedule_rules(token)
            self.db.set_hidden("schedule", token, False)
            self.db.add_audit(user, "fascia oraria eliminata", name)

    def set_hidden(self, kind: str, token: str, hidden: bool, user: str) -> None:
        """Nasconde dalle liste un gruppo o una fascia che non serve, senza toccare il controller."""
        if kind not in ("group", "schedule"):
            raise ServiceError("elemento sconosciuto")
        if hidden and kind == "group" and token == self.access.default_group:
            raise ServiceError("il gruppo predefinito non si può nascondere: scegline prima un altro")
        self.db.set_hidden(kind, token, hidden)
        if kind == "group":
            action = f"gruppo {'nascosto' if hidden else 'mostrato'}"
        else:
            action = f"fascia oraria {'nascosta' if hidden else 'mostrata'}"
        self.db.add_audit(user, action, self._names.get((kind, token)) or STANDARD_NAMES.get(token) or token)

    # Festività

    async def add_holidays(self, holidays: list[dict], user: str) -> None:
        cleaned = []
        for holiday in holidays:
            try:
                day, name = normalize_holiday(holiday.get("day", ""), holiday.get("name", ""))
            except RuleError as err:
                raise ServiceError(str(err)) from err
            cleaned.append((day, name, bool(holiday.get("yearly"))))
        async with self.lock:
            before = self.db.list_holidays()
            for day, name, yearly in cleaned:
                if yearly:
                    # Una sola festività annuale per giorno e mese (le nazionali ricliccate in un altro anno): resta
                    # la più vecchia, che vale anche per gli anni dopo
                    same = [h["day"] for h in self.db.list_holidays() if h["yearly"] and h["day"][5:] == day[5:]]
                    day = min([day, *same])
                    for other in same:
                        if other != day:
                            self.db.delete_holiday(other)
                self.db.add_holiday(day, name, yearly)
            await self._write_holidays(before)
        self.db.add_audit(user, "festività aggiunte", ", ".join(holiday_label(*holiday) for holiday in cleaned))

    async def add_italian_holidays(self, year: int, user: str) -> None:
        await self.add_holidays(italian_holidays([year, year + 1]), user)

    async def delete_holiday(self, day: str, user: str) -> None:
        async with self.lock:
            before = self.db.list_holidays()
            gone = next((h for h in before if h["day"] == day), None)
            yearly = bool(gone and gone["yearly"])
            for holiday in before:
                # Anche le copie annuali dello stesso giorno e mese: restando, la giornata resterebbe esclusa
                if holiday["day"] == day or (yearly and holiday["yearly"] and holiday["day"][5:] == day[5:]):
                    self.db.delete_holiday(holiday["day"])
            await self._write_holidays(before)
        self.db.add_audit(user, "festività eliminata", holiday_label(day, gone["name"] if gone else "", yearly))

    async def _write_holidays(self, before: list[dict]) -> None:
        """Porta sul controller le festività appena cambiate nell'app. Se non ci riesce rimette l'elenco di prima,
        così l'app non mostra festività che il controller non applica (o viceversa)."""
        try:
            await self._reapply_holidays()
        except (VapixError, ServiceError):
            for holiday in self.db.list_holidays():
                self.db.delete_holiday(holiday["day"])
            for holiday in before:
                self.db.add_holiday(holiday["day"], holiday["name"], holiday["yearly"])
            try:
                # Le fasce già scritte prima dell'errore tornano come prima
                await self._reapply_holidays(only_changed=True)
            except (VapixError, ServiceError) as err:
                _LOGGER.warning("Festività di prima non rimesse sul controller: %s", err)
            raise

    def upgrade_holidays(self) -> int:
        """All'avvio, sulle festività salvate dalle versioni precedenti: una sola festività annuale per giorno e mese,
        nomi delle nazionali come adesso e, una volta sola, il 4 ottobre a chi aveva già le nazionali.
        Restituisce quante righe ha cambiato: le porta sul controller reapply_holidays(only_changed=True)."""
        national = dict(ITALIAN_HOLIDAYS)
        yearly: dict[str, dict] = {}
        changed = 0
        # In ordine di mese e giorno, poi di anno: per ogni giorno resta la più vecchia
        for holiday in self.db.list_holidays():
            if not holiday["yearly"]:
                continue
            month_day = holiday["day"][5:]
            if month_day in yearly:
                self.db.delete_holiday(holiday["day"])
                changed += 1
                continue
            yearly[month_day] = holiday
            name = national.get(month_day, "")
            if holiday["name"] != name and holiday["name"].lower() == name.lower():
                self.db.add_holiday(holiday["day"], name, True)
                changed += 1
        if not self.db.get_setting(SETTING_OCT4):
            self.db.set_setting(SETTING_OCT4, "1")
            if "10-04" not in yearly and sum(1 for md in national if md in yearly) >= 5:
                year = max(2026, min(int(h["day"][:4]) for h in yearly.values()))
                self.db.add_holiday(f"{year}-10-04", national["10-04"], True)
                label = holiday_label(f"{year}-10-04", national["10-04"], True)
                self.db.add_audit("sistema", "festività aggiunte", f"{label}, festa nazionale dal 2026")
                changed += 1
        return changed

    async def reapply_holidays(self, only_changed: bool = False) -> int:
        """Riscrive le eccezioni delle fasce dell'add-on che escludono le festività.

        Con only_changed tocca solo le fasce il cui elenco di eccezioni sul controller non è quello atteso
        (ad esempio perché la scrittura era stata rifiutata da una versione precedente)."""
        async with self.lock:
            return await self._reapply_holidays(only_changed)

    async def _reapply_holidays(self, only_changed: bool = False) -> int:
        managed = {t: r for t, r in self.db.schedule_rules().items() if r["holidays"]}
        if not managed:
            return 0
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
            if is_rex_profile(profile):
                continue  # Il permesso di pulsanti e relè: si gestisce dagli ingressi, non è un gruppo di persone
            token = profile.get("token", "")
            schedule_tokens = profile_schedules(profile)
            self._names["group", token] = profile.get("Name") or token
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

    async def _check_schedule(self, schedule: str) -> str:
        """Nome della fascia scelta, se esiste sul controller."""
        if not schedule:
            raise ServiceError("scegli una fascia oraria")
        found = next((s for s in await self.controller.list_schedules() if s.get("token") == schedule), None)
        if found is None:
            raise ServiceError("la fascia oraria scelta non esiste più sul controller")
        return _schedule_name(found, schedule in self.db.schedule_rules())

    async def create_group(self, name: str, description: str, schedule: str, user: str) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        async with self.lock:
            schedule_name = await self._check_schedule(schedule)
            profile = await self._template_profile(schedule)
            profile["Name"] = name
            profile["Description"] = description
            token = await self.controller.set_access_profile(profile)
            if not token:
                raise ServiceError("il controller non ha restituito il token del nuovo gruppo")
            self.db.add_audit(user, "gruppo creato", f"{name} ({schedule_name})")
        return {"token": token}

    async def update_group(self, token: str, name: str, description: str, schedule: str, user: str) -> dict:
        name = normalize_name(name)
        description = normalize_text(description, 200, "descrizione")
        async with self.lock:
            schedule_name = await self._check_schedule(schedule)
            found = await self.controller.get_access_profiles([token])
            if not found:
                raise ServiceError("gruppo non trovato sul controller")
            profile = self._with_schedule(found[0], schedule)
            profile["Name"] = name
            profile["Description"] = description
            await self.controller.set_access_profile(profile)
            self.db.add_audit(user, "gruppo modificato", f"{name} ({schedule_name})")
        return {"token": token}

    async def delete_group(self, token: str, user: str) -> None:
        if token == self.access.default_group:
            raise ServiceError("è il gruppo predefinito: scegline prima un altro come predefinito")
        used_by = [c["name"] for c in self.db.list_categories() if c["group_token"] == token]
        if used_by:
            raise ServiceError(
                f"gruppo usato {_categories(used_by)}: cambia prima {'il suo' if len(used_by) == 1 else 'il loro'} "
                "gruppo"
            )
        persons = self.db.profile_usage().get(token, 0)
        if persons:
            raise ServiceError(
                f"gruppo assegnato a {plural(persons, 'persona', 'persone')}: "
                f"{'spostala' if persons == 1 else 'spostale'} prima in un altro gruppo"
            )
        async with self.lock:
            found = await self.controller.get_access_profiles([token])
            await self.controller.remove_access_profile(token)
            self.db.set_hidden("group", token, False)
            self.db.add_audit(user, "gruppo eliminato", (found[0].get("Name") if found else "") or token)

    async def set_default_group(self, token: str, user: str) -> None:
        found = next((p for p in await self.controller.list_access_profiles() if p.get("token") == token), None)
        if found is None:
            raise ServiceError("gruppo non trovato sul controller")
        self.db.set_setting("default_group", token)
        self.db.set_hidden("group", token, False)
        self.db.add_audit(user, "gruppo predefinito cambiato", found.get("Name") or token)

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

    def _places(self, deleted_areas: Collection[int] = ()) -> tuple[list[dict], dict[int, int], dict[int, int]]:
        """Porte del controller in uso, area di ogni porta e struttura di ogni area.
        deleted_areas: come sarebbero dopo l'eliminazione di quelle aree (porte e controller restano senza)."""
        installation = InstallationService(self.db)
        controllers = {
            c["id"]: {**c, "area_id": 0} if c["area_id"] in deleted_areas else c for c in self.db.list_controllers()
        }
        doors = [{**d, "area_id": 0} if d["area_id"] in deleted_areas else d for d in installation.doors()]
        door_area = {d["id"]: installation.door_area(d, controllers) for d in doors}
        area_site = {a["id"]: s["id"] for s in self.db.list_sites() for a in s["areas"] if a["id"] not in deleted_areas}
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

    def _door_schedules(self, rules: list[dict], layout: tuple | None = None) -> dict[str, list[str]]:
        """Porte coperte dalle righe, con le fasce di ognuna: area e struttura valgono per tutte le loro porte,
        anche quelle aggiunte dopo. layout: il risultato di _places, se già letto."""
        doors, door_area, area_site = layout or self._places()
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

    async def _build_profile(self, values: dict, rules: list[dict], base: dict | None, refresh: bool = False) -> dict:
        """Profilo della tipologia: una regola per ogni punto di accesso con lettore delle porte coperte.
        Le regole su porte che l'app non gestisce restano come sono.
        refresh: dopo una modifica a porte, aree o strutture le righe possono non coprire più nessuna porta
        dell'app: il profilo perde le sue regole su quelle porte (e si spegne se non gliene restano)."""
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
        if not refresh and not any(point_door.get(p["AccessPoint"]) in app_doors for p in policies):
            raise ServiceError("nelle porte scelte non c'è nessun lettore programmato sul controller")
        used = {t for p in policies for t in p["Schedule"]}
        profile = copy.deepcopy(base) if base else {}
        profile.setdefault("token", "")
        profile.update(
            {
                "Name": values["name"],
                "Description": values["description"] or f"Tipologia {values['name']} (Axis Access Manager)",
                # Senza nessuna regola non deve aprire niente
                "Enabled": bool(policies),
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
        missing = next((r for r in rules if r["schedule"] not in tokens), None)
        if missing is not None:
            raise ServiceError(
                f"la fascia oraria della riga {self._place_name(missing)} non esiste più sul controller: "
                "scegline un'altra"
            )
        if len({r["schedule"] for r in rules}) > 1 and ALWAYS not in tokens:
            # Con fasce diverse per porta il profilo usa Sempre (vedi _build_profile)
            raise ServiceError("manca la fascia Sempre sul controller: serve alle tipologie con fasce diverse")

    def _place_name(self, rule: dict) -> str:
        """Nome della porta, dell'area o della struttura di una riga di accesso."""
        if rule["target"] == "door":
            door = self.db.get_door(rule["target_id"])
            return door["name"] if door else "?"
        for site in self.db.list_sites():
            if rule["target"] == "site" and site["id"] == rule["target_id"]:
                return site["name"]
            for area in site["areas"]:
                if rule["target"] == "area" and area["id"] == rule["target_id"]:
                    return f"{site['name']} › {area['name']}"
        return "?"

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
        categories = self.db.list_categories()
        names = [
            c["name"]
            for c in categories
            if any((r["target"], r["target_id"]) in places for r in rules.get(c["id"], []))
        ]
        if names:
            raise ServiceError(
                f"è usata {_categories(names)}: toglila prima {'dalle sue' if len(names) == 1 else 'dalle loro'} "
                "righe di accesso"
            )
        if target == "door":
            return
        # Porte e controller delle aree eliminate restano senza area: una porta con un'area sua passa a quella del
        # controller (ed entra nelle righe sulla sua area o struttura), le altre escono dalla struttura.
        # Nessuna tipologia deve aprire porte in più, né perderne, senza che lo si decida
        before = self._places()
        after = self._places(deleted_areas={place_id for kind, place_id in places if kind == "area"})
        door_names = {d["door_token"]: d["name"] for d in before[0]}
        gaining: list[str] = []
        losing: list[str] = []
        gained: set[str] = set()
        lost: set[str] = set()
        for category in categories:
            if not rules.get(category["id"]):
                continue
            old = self._door_schedules(rules[category["id"]], before)
            new = self._door_schedules(rules[category["id"]], after)
            more = {door for door, schedules in new.items() if not set(schedules) <= set(old.get(door, []))}
            less = {door for door, schedules in old.items() if not set(schedules) <= set(new.get(door, []))}
            if more:
                gaining.append(category["name"])
                gained |= more
            if less:
                losing.append(category["name"])
                lost |= less
        if gained:
            one = len(gained) == 1
            doors = ", ".join(sorted(door_names.get(d) or d for d in gained))
            raise ServiceError(
                f"{'la porta' if one else 'le porte'} {doors} {'passerebbe' if one else 'passerebbero'} all'area "
                f"del controller ed {'entrerebbe' if one else 'entrerebbero'} nelle righe di accesso "
                f"{_categories(gaining, 'della', 'delle')}: sposta prima {'la porta' if one else 'le porte'} "
                "in un'altra area"
            )
        if lost:
            one = len(lost) == 1
            doors = ", ".join(sorted(door_names.get(d) or d for d in lost))
            raise ServiceError(
                f"{'la porta' if one else 'le porte'} {doors} {'uscirebbe' if one else 'uscirebbero'} dalla "
                f"struttura scelta nelle righe di accesso {_categories(losing, 'della', 'delle')}: "
                "sposta prima porte e controller in un'altra area"
            )

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
            # Il profilo lasciato si ricorda finché tutte le persone non sono passate al nuovo: se il controller
            # si ferma a metà, salvando di nuovo (Riprova) si spostano quelle rimaste
            moving = self._moving(category_id)
            if old_token and old_token != token and old_token not in moving:
                moving.append(old_token)
                self.db.set_setting(f"{MOVING_PREFIX}{category_id}", json.dumps(moving))
            action = "tipologia modificata" if old else "tipologia ricreata da backup" if new_id else "tipologia creata"
            self.db.add_audit(user, action, values["name"])
        moved, failed = 0, []
        if moving:
            for person in self.db.persons_of_category(category_id):
                cards = person["cards"]
                if cards and cards[0]["access_profile"] in moving and cards[0]["access_profile"] != token:
                    try:
                        await self.access.set_person_access(
                            person["id"], token, cards[0]["valid_from"], cards[0]["valid_to"], user
                        )
                    except ServiceError as err:
                        # Un problema di una persona (tessera non sul controller) non ferma le altre
                        failed.append((person["name"], str(err)))
                        continue
                    moved += 1
            if failed:
                names, error = ", ".join(n for n, _ in failed), failed[0][1]
                raise ServiceError(
                    f"tipologia salvata, ma {names} {'ha' if len(failed) == 1 else 'hanno'} ancora l'accesso di prima. "
                    f"{error[:1].upper()}{error[1:]}, poi salva di nuovo la tipologia"
                )
            self.db.set_setting(f"{MOVING_PREFIX}{category_id}", "")
        return {"id": category_id, "token": token, "updated": moved}

    def _moving(self, category_id: int) -> list[str]:
        """Profili lasciati dalla tipologia con persone ancora da spostare sul suo."""
        try:
            return list(json.loads(self.db.get_setting(f"{MOVING_PREFIX}{category_id}") or "[]"))
        except (TypeError, ValueError):
            return []

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
            self.db.set_setting(f"{MOVING_PREFIX}{category_id}", "")
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
                # Anche se le righe non coprono più nessuna porta: il profilo vecchio la farebbe aprire ancora
                profile = await self._build_profile(category, rules[category["id"]], found[0], refresh=True)
                if _policy_sig(profile) == _policy_sig(found[0]):
                    continue
                await self.controller.set_access_profile(profile)
                if self._door_schedules(rules[category["id"]]):
                    detail = f"{category['name']}: porte di aree e strutture"
                else:
                    detail = f"{category['name']}: le righe di accesso non comprendono più nessuna porta, accesso tolto"
                    _LOGGER.warning("La tipologia %s non comprende più nessuna porta", category["name"])
                self.db.add_audit("sistema", "tipologia aggiornata", detail)
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
                # Profilo solo su porte che l'app non gestisce: nessuna riga, e non è una modifica
                if adopted := self._rules_of(profiles[category["group_token"]], points):
                    self.db.set_category_rules(category["id"], adopted)
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
            self.db.add_audit("sistema", "gruppi passati alle tipologie", plural(fixed, "modifica", "modifiche"))
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


def _categories(names: list[str], one: str = "dalla", many: str = "dalle") -> str:
    """«dalla tipologia X» o «dalle tipologie X, Y», secondo quante sono."""
    return f"{one} tipologia {names[0]}" if len(names) == 1 else f"{many} tipologie {', '.join(names)}"


def _no_users() -> dict:
    return {"categories": [], "profiles": [], "inputs": False}


def _schedule_name(schedule: dict, managed: bool) -> str:
    """Nome della fascia: quelle predefinite hanno il nome italiano finché l'app non le modifica."""
    token = schedule.get("token") or ""
    return (None if managed else STANDARD_NAMES.get(token)) or schedule.get("Name") or token


def _schedule_in_use(users: dict) -> str:
    """Messaggio per una fascia che non si può eliminare: chi la usa e dove cambiarla."""
    found = []
    if names := users["categories"]:
        where = "nelle sue righe di accesso" if len(names) == 1 else "nelle loro righe di accesso"
        found.append((f"{'dalla tipologia' if len(names) == 1 else 'dalle tipologie'} {', '.join(names)}", where))
    if names := users["profiles"]:
        who = f"{'dal profilo' if len(names) == 1 else 'dai profili'} {', '.join(names)} del controller"
        found.append((who, "nel sito del controller"))
    if users["inputs"]:
        found.append(("dagli ingressi del controller", "negli ingressi"))
    if len(found) > 1 and users["categories"]:
        found[0] = (found[0][0], "nelle righe di accesso")
    return (
        f"fascia usata {' e '.join(who for who, _ in found)}: cambiala prima {' e '.join(where for _, where in found)}"
    )
