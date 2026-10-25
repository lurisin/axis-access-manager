"""Logica di gestione: anagrafica sincronizzata con l'A1210, gruppi e validità, eventi, apertura porta."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from .config import Settings
from .db import Database, now_iso
from .events import Event, EventMonitor, door_of_topic
from .rules import RuleError, check_validity, normalize_validity
from .vapix import A1210Client, VapixError

_LOGGER = logging.getLogger(__name__)

CARD_PATTERN = re.compile(r"^[0-9A-Fa-f]{2,64}$")

DENIED_REASONS = {
    "CredentialNotEnabled": "tessera sospesa",
    "CredentialNotActive": "tessera non ancora valida",
    "CredentialExpired": "tessera scaduta",
    "Unauthorized": "non autorizzata su questa porta o in questo orario",
    "InvalidAccessProfile": "profilo di accesso non valido",
    "CredentialNotFound": "tessera non registrata",
}

DOOR_ALARMS = {
    "DoorForcedOpen": "porta forzata",
    "DoorOpenTooLong": "porta aperta troppo a lungo",
}


class ServiceError(Exception):
    """Richiesta non valida o non eseguibile: il messaggio va mostrato all'utente."""


def normalize_card(value: str) -> str:
    number = re.sub(r"[\s:]", "", value or "")
    if not CARD_PATTERN.match(number):
        raise ServiceError(f"numero tessera non valido ({value}): servono cifre esadecimali come le legge il lettore")
    return number


def normalize_name(value: str) -> str:
    name = " ".join((value or "").split())
    if not name:
        raise ServiceError("manca il nome")
    if len(name) > 100:
        raise ServiceError("nome troppo lungo (massimo 100 caratteri)")
    return name


def user_token_for(name: str, now: datetime | None = None) -> str:
    """Stessa convenzione degli script di Home Assistant: utente_<nome>_<aammgghhmmss>."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")[:30] or "senza_nome"
    return f"utente_{slug}_{(now or datetime.now()).strftime('%y%m%d%H%M%S')}"


def normalize_text(value: str | None, limit: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        raise ServiceError(f"{label} troppo lungo (massimo {limit} caratteri)")
    return text


def access_of_credential(credential: dict) -> dict:
    """Gruppo e validità come li tiene il controller."""
    profiles = credential.get("CredentialAccessProfile") or []
    return {
        "access_profile": str(profiles[0].get("AccessProfile") or "") if profiles else "",
        "valid_from": str(credential.get("ValidFrom") or ""),
        "valid_to": str(credential.get("ValidTo") or ""),
    }


def card_of_credential(credential: dict) -> str:
    for item in credential.get("IdData") or []:
        if item.get("Name") == "Card":
            return str(item.get("Value") or "")
    return ""


class AccessService:
    def __init__(
        self, db: Database, controller: A1210Client, settings: Settings, monitor: EventMonitor | None = None
    ) -> None:
        self.db = db
        self.controller = controller
        self.settings = settings
        self.monitor = monitor
        self.lock = asyncio.Lock()
        # Chiamata dopo ogni evento salvato nel log (istantanee, Home Assistant)
        self.after_event: Callable[[dict], Awaitable[None]] | None = None
        # Chiamata quando cambia qualcosa che Home Assistant mostra (porta, contatori)
        self.on_change: Callable[[], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task] = set()
        # Ultimi esiti del controller: lo stesso diniego arriva su più topic e va registrato una volta
        self._recent_decisions: deque[tuple] = deque(maxlen=20)
        # Punto di accesso → porta, per sapere a quale porta si riferisce un evento
        self.access_point_doors: dict[str, str] = {}

    def _spawn(self, coro: Awaitable[None]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _changed(self) -> None:
        if self.on_change:
            self._spawn(self.on_change())

    def log_event(self, **fields) -> dict:
        """Salva un evento e avvisa chi lo deve elaborare."""
        event_id = self.db.add_event(**fields)
        row = {**fields, "id": event_id}
        row.pop("raw", None)
        if self.after_event:
            self._spawn(self.after_event(row))
        self._changed()
        return row

    # Sincronizzazione

    async def sync_from_controller(self) -> dict:
        """Allinea il database a utenti e tessere presenti sul controller, che resta la fonte di verità."""
        async with self.lock:
            credentials = await self.controller.list_credentials()
            try:
                users = await self.controller.list_users()
            except VapixError as err:
                # Senza elenco utenti i nomi si prendono dalla descrizione delle tessere
                _LOGGER.warning("Elenco utenti non leggibile: %s", err)
                users = []
            names = {u.get("token"): (u.get("Name") or "").strip() for u in users}
            seen_cards: set[str] = set()
            added = 0
            for credential in credentials:
                token = credential.get("token") or ""
                number = card_of_credential(credential)
                if not token or not number:
                    continue
                user_token = credential.get("UserToken") or f"credenziale:{token}"
                name = names.get(user_token) or (credential.get("Description") or "").strip() or "(Senza nome)"
                person = self.db.person_by_user_token(user_token)
                if person is None:
                    person_id = self.db.add_person(name, user_token)
                else:
                    person_id = person["id"]
                    if person["name"] != name:
                        self.db.rename_person(person_id, name)
                enabled = bool(credential.get("Enabled", True))
                access = access_of_credential(credential)
                existing = self.db.card_by_token(token)
                if existing is None:
                    other = self.db.card_by_number(number)
                    if other is not None:
                        self.db.delete_card(other["id"])
                    self.db.add_card(person_id, number, token, enabled, access)
                    added += 1
                else:
                    self.db.update_card(
                        existing["id"], person_id=person_id, number=number, enabled=enabled, access=access
                    )
                seen_cards.add(token)
            removed = 0
            for card in self.db.all_cards():
                if card["axis_credential_token"] not in seen_cards:
                    self.db.delete_card(card["id"])
                    removed += 1
            for person in self.db.all_persons():
                if not self.db.cards_of(person["id"]):
                    self.db.delete_person(person["id"])
            try:
                points = await self.controller.list_access_points()
                self.access_point_doors = {p.get("token"): p.get("Entity") or "" for p in points if p.get("token")}
            except VapixError as err:
                _LOGGER.debug("Punti di accesso non leggibili: %s", err)
            self._changed()
            return {"cards": len(seen_cards), "added": added, "removed": removed}

    # Anagrafica

    @property
    def default_group(self) -> str:
        """Gruppo delle nuove tessere: scelto nell'app, altrimenti quello delle opzioni."""
        return self.db.get_setting("default_group") or self.settings.access_profile

    def _category(self, category_id: int | None) -> dict | None:
        if not category_id:
            return None
        category = self.db.get_category(category_id)
        if category is None:
            raise ServiceError("tipologia non trovata")
        return category

    def _access(self, group: str | None, valid_from: str | None, valid_to: str | None) -> dict:
        """Gruppo (vuoto = quello predefinito) e validità controllati."""
        profile = (group or "").strip() or self.default_group
        if not profile:
            raise ServiceError("nessun gruppo scelto e nessun gruppo predefinito: impostalo in Regole → Gruppi")
        try:
            access = {
                "access_profile": profile,
                "valid_from": normalize_validity(valid_from),
                "valid_to": normalize_validity(valid_to),
            }
            check_validity(access["valid_from"], access["valid_to"])
        except RuleError as err:
            raise ServiceError(str(err)) from err
        return access

    @staticmethod
    def _apply_access(credential: dict, access: dict) -> dict:
        credential["ValidFrom"] = access["valid_from"]
        credential["ValidTo"] = access["valid_to"]
        credential["CredentialAccessProfile"] = [
            {"AccessProfile": access["access_profile"], "ValidFrom": "", "ValidTo": ""}
        ]
        return credential

    def _credential(self, user_token: str, name: str, number: str, access: dict) -> dict:
        return self._apply_access(
            {
                "token": "",
                "UserToken": user_token,
                "Description": name,
                "Enabled": True,
                "Status": "Enabled",
                "IdData": [{"Name": "Card", "Value": number}],
                "Attribute": [],
                "AuthenticationProfile": [],
            },
            access,
        )

    def _check_card_free(self, number: str) -> None:
        existing = self.db.card_by_number(number)
        if existing:
            raise ServiceError(f"la tessera {number} è già registrata a nome {existing['person_name']}")

    async def create_person(
        self,
        name: str,
        card: str,
        user: str,
        group: str = "",
        valid_from: str = "",
        valid_to: str = "",
        company: str = "",
        notes: str = "",
        category_id: int = 0,
    ) -> dict:
        name, number = normalize_name(name), normalize_card(card)
        company = normalize_text(company, 100, "ditta o reparto")
        notes = normalize_text(notes, 1000, "testo delle note")
        category = self._category(category_id)
        if category:
            # La tipologia decide gruppo e durata, se non sono stati scelti a mano
            group = group or category["group_token"]
            if category["valid_days"] and not valid_to:
                start = datetime.now(tz=UTC).replace(microsecond=0)
                valid_from = valid_from or start.isoformat()
                valid_to = (start + timedelta(days=category["valid_days"])).isoformat()
        access = self._access(group, valid_from, valid_to)
        async with self.lock:
            self._check_card_free(number)
            user_token = user_token_for(name)
            await self.controller.set_user(
                {"token": user_token, "Name": name, "Description": "Registrato da Axis Access Manager", "Attribute": []}
            )
            try:
                credential_token = await self.controller.set_credential(
                    self._credential(user_token, name, number, access)
                )
            except VapixError:
                # Senza tessera l'utente resterebbe orfano sul controller
                try:
                    await self.controller.remove_user(user_token)
                except VapixError:
                    _LOGGER.warning("Utente %s rimasto sul controller dopo un errore", user_token)
                raise
            person_id = self.db.add_person(name, user_token)
            if company or notes:
                self.db.update_person_details(person_id, company, notes)
            if category:
                self.db.set_person_category(person_id, category["id"])
            self.db.add_card(person_id, number, credential_token, access=access)
            detail = f"{name}, tessera {number}" + (f", tipologia {category['name']}" if category else "")
            self.db.add_audit(user, "persona creata", detail)
            self._changed()
            return self.db.get_person(person_id)

    async def add_card(self, person_id: int, card: str, user: str) -> dict:
        number = normalize_card(card)
        async with self.lock:
            person = self._person(person_id)
            self._check_card_free(number)
            # La nuova tessera eredita gruppo e validità di quelle che la persona ha già
            first = person["cards"][0] if person["cards"] else {}
            access = self._access(first.get("access_profile"), first.get("valid_from"), first.get("valid_to"))
            credential_token = await self.controller.set_credential(
                self._credential(person["axis_user_token"], person["name"], number, access)
            )
            self.db.add_card(person_id, number, credential_token, access=access)
            self._changed()
            self.db.add_audit(user, "tessera aggiunta", f"{person['name']}, tessera {number}")
            return self.db.get_person(person_id)

    async def update_person(
        self,
        person_id: int,
        user: str,
        name: str | None = None,
        company: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Nome (scritto anche sul controller), ditta e note (solo nell'add-on)."""
        person = self._person(person_id)
        if company is not None or notes is not None:
            new_company = normalize_text(company, 100, "ditta o reparto") if company is not None else person["company"]
            new_notes = normalize_text(notes, 1000, "testo delle note") if notes is not None else person["notes"]
            if (new_company, new_notes) != (person["company"], person["notes"]):
                self.db.update_person_details(person_id, new_company, new_notes)
                self.db.add_audit(user, "dati persona modificati", person["name"])
        if name is not None and normalize_name(name) != person["name"]:
            return await self.rename_person(person_id, name, user)
        return self.db.get_person(person_id)

    async def rename_person(self, person_id: int, name: str, user: str) -> dict:
        name = normalize_name(name)
        async with self.lock:
            person = self._person(person_id)
            token = person["axis_user_token"] or ""
            if token and not token.startswith("credenziale:"):
                users = await self.controller.get_users([token])
                axis_user = users[0] if users else {"token": token, "Description": "", "Attribute": []}
                axis_user["Name"] = name
                await self.controller.set_user(axis_user)
            for card in person["cards"]:
                for credential in await self.controller.get_credentials([card["axis_credential_token"]]):
                    credential["Description"] = name
                    await self.controller.set_credential(credential)
            self.db.rename_person(person_id, name)
            self.db.add_audit(user, "persona rinominata", f"{person['name']} → {name}")
            return self.db.get_person(person_id)

    async def set_person_access(
        self,
        person_id: int,
        group: str,
        valid_from: str,
        valid_to: str,
        user: str,
        category_id: int | None = None,
    ) -> dict:
        """Gruppo e validità per tutte le tessere della persona; category_id None lascia la tipologia com'è."""
        category = self._category(category_id)
        if category and not group:
            group = category["group_token"]
        access = self._access(group, valid_from, valid_to)
        async with self.lock:
            person = self._person(person_id)
            if category_id is not None and category_id != person["category_id"]:
                self.db.set_person_category(person_id, category_id)
                self.db.add_audit(
                    user, "tipologia cambiata", f"{person['name']}: {category['name'] if category else 'nessuna'}"
                )
            for card in person["cards"]:
                credentials = await self.controller.get_credentials([card["axis_credential_token"]])
                if not credentials:
                    raise ServiceError(f"la tessera {card['number']} non è più sul controller: rileggi dal controller")
                await self.controller.set_credential(self._apply_access(credentials[0], access))
                self.db.set_card_access(card["id"], access)
            detail = f"{person['name']}: gruppo {access['access_profile']}"
            if access["valid_from"] or access["valid_to"]:
                detail += f", valida {access['valid_from'] or '…'} → {access['valid_to'] or '…'}"
            self.db.add_audit(user, "accesso modificato", detail)
            return self.db.get_person(person_id)

    async def set_card_enabled(self, card_id: int, enabled: bool, user: str) -> dict:
        async with self.lock:
            card = self._card(card_id)
            if enabled:
                await self.controller.enable_credential(card["axis_credential_token"])
            else:
                await self.controller.disable_credential(card["axis_credential_token"])
            self.db.set_card_enabled(card_id, enabled)
            self._changed()
            person = self.db.get_person(card["person_id"])
            self.db.add_audit(
                user,
                "tessera riattivata" if enabled else "tessera sospesa",
                f"{person['name']}, tessera {card['number']}",
            )
            return person

    async def delete_card(self, card_id: int, user: str) -> dict | None:
        """Elimina la tessera; se era l'ultima elimina anche la persona. Restituisce la persona rimasta."""
        async with self.lock:
            card = self._card(card_id)
            person = self.db.get_person(card["person_id"])
            await self.controller.remove_credential(card["axis_credential_token"])
            self.db.delete_card(card_id)
            self.db.add_audit(user, "tessera eliminata", f"{person['name']}, tessera {card['number']}")
            self._changed()
            if len(person["cards"]) > 1:
                return self.db.get_person(person["id"])
            await self._remove_axis_user(person)
            self.db.delete_person(person["id"])
            return None

    async def delete_person(self, person_id: int, user: str) -> None:
        async with self.lock:
            person = self._person(person_id)
            for card in person["cards"]:
                await self.controller.remove_credential(card["axis_credential_token"])
                self.db.delete_card(card["id"])
            await self._remove_axis_user(person)
            self.db.delete_person(person_id)
            numbers = ", ".join(card["number"] for card in person["cards"])
            self.db.add_audit(user, "persona eliminata", f"{person['name']} ({numbers})")
            self._changed()

    async def _remove_axis_user(self, person: dict) -> None:
        token = person["axis_user_token"] or ""
        if not token or token.startswith("credenziale:"):
            return
        try:
            await self.controller.remove_user(token)
        except VapixError as err:
            _LOGGER.warning("Utente %s non rimosso dal controller: %s", token, err)

    def _person(self, person_id: int) -> dict:
        person = self.db.get_person(person_id)
        if person is None:
            raise ServiceError("persona non trovata")
        return person

    def _card(self, card_id: int) -> dict:
        card = self.db.get_card(card_id)
        if card is None:
            raise ServiceError("tessera non trovata")
        return card

    # Porta

    async def open_door(self, extended: bool, user: str, door: str = "") -> None:
        """Sblocco temporaneo, solo a serratura bloccata: la serratura non regge l'alimentazione continua."""
        door = door or self.settings.door_token
        if not door:
            raise ServiceError("nessuna porta configurata")
        mode = self.monitor.door_modes.get(door) if self.monitor else None
        if mode is None:
            try:
                mode = await self.controller.door_mode(door)
            except VapixError as err:
                _LOGGER.warning("Stato della porta non leggibile: %s", err)
        if mode is None:
            raise ServiceError("stato della porta sconosciuto: riprova tra qualche secondo")
        if mode != "Locked":
            raise ServiceError(f"la porta non è bloccata (stato {mode}): attendi che si richiuda")
        await self.controller.access_door(door, extended)
        if self.monitor and self.monitor.connected:
            # Evita un secondo comando finché MQTT non segnala che la porta si è richiusa
            self.monitor.door_modes[door] = "Accessed"
        detail = "tempo esteso" if extended else ""
        self.log_event(ts=now_iso(), kind="apertura_remota", detail=detail, source=user, door=door)
        self.db.add_audit(user, "porta aperta", detail)

    # Eventi

    def record_event(self, event: Event, retained: bool = False) -> None:
        """Salva nel log gli eventi utili. I messaggi conservati dal broker sono già stati registrati."""
        if event.topic.startswith("Door/State/DoorMode/"):
            # Anche il valore conservato dal broker aggiorna lo stato in Home Assistant
            self._changed()
            return
        if retained:
            return
        ts = event.timestamp.isoformat(timespec="seconds")
        data = event.data
        topic = event.topic
        door = self._door_of(event)
        if topic.startswith("IdPoint/Request/IdData"):
            number = str(data.get("Card") or "")
            if not number:
                return
            card = self.db.card_by_number(number)
            if card is None:
                self.db.note_unknown_card(number, ts)
                self.log_event(ts=ts, kind="sconosciuta", card=number, source="lettore", raw=data, door=door)
            else:
                self.log_event(
                    ts=ts,
                    kind="lettura",
                    card=card["number"],
                    person_id=card["person_id"],
                    person_name=card["person_name"],
                    detail="" if card["enabled"] else "tessera sospesa",
                    source="lettore",
                    raw=data,
                    door=door,
                )
        elif topic.startswith("AccessControl/AccessGranted") or topic.startswith("AccessControl/Denied"):
            granted = topic.startswith("AccessControl/AccessGranted")
            card = None
            if data.get("CredentialToken"):
                card = self.db.card_by_token(str(data["CredentialToken"]))
            number = str(data.get("Card") or data.get("IdData") or "")
            if card is None and number:
                card = self.db.card_by_number(number)
            reason = str(data.get("Reason") or ("CredentialNotFound" if "CredentialNotFound" in topic else ""))
            # Stesso esito, stesso punto di accesso, stesso secondo: è lo stesso passaggio
            key = (granted, data.get("AccessPointToken") or door, ts)
            if key in self._recent_decisions:
                return
            self._recent_decisions.append(key)
            self.log_event(
                ts=ts,
                kind="concesso" if granted else "negato",
                card=card["number"] if card else number,
                person_id=card["person_id"] if card else None,
                person_name=card["person_name"] if card else str(data.get("CredentialHolderName") or ""),
                detail="" if granted else DENIED_REASONS.get(reason, reason),
                source="controller",
                raw=data,
                door=door,
            )
        elif topic.startswith("Door/State/DoorAlarm/") and data.get("State") not in (None, "Normal"):
            state = str(data["State"])
            self.log_event(
                ts=ts, kind="allarme", detail=DOOR_ALARMS.get(state, state), source="porta", raw=data, door=door
            )
        elif topic.startswith("Door/State/DoorTamper/") and data.get("State") == "TamperDetected":
            self.log_event(ts=ts, kind="manomissione", detail="manomissione porta", source="porta", raw=data, door=door)
        elif topic.startswith("Device/Casing/Open") and str(data.get("Open")) == "1":
            self.log_event(ts=ts, kind="manomissione", detail="alloggiamento controller aperto", source="controller")

    def _door_of(self, event: Event) -> str:
        """Porta dell'evento: dal topic, dai dati o dal punto di accesso; altrimenti la porta principale."""
        token = door_of_topic(event.topic) or str(event.data.get("DoorToken") or "")
        if not token and event.data.get("AccessPointToken"):
            token = self.access_point_doors.get(str(event.data["AccessPointToken"]), "")
        return token or self.settings.door_token

    def record_intercom(self, entity_id: str, name: str) -> dict:
        """Chiamata dal citofono (entità di Home Assistant)."""
        return self.log_event(ts=now_iso(), kind="citofono", detail=name, source=entity_id)
