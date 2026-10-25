"""Logica di gestione: anagrafica sincronizzata con l'A1210, eventi, apertura porta."""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from datetime import datetime

from .config import Settings
from .db import Database, now_iso
from .events import Event, EventMonitor
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
        self._lock = asyncio.Lock()

    # Sincronizzazione

    async def sync_from_controller(self) -> dict:
        """Allinea il database a utenti e tessere presenti sul controller, che resta la fonte di verità."""
        async with self._lock:
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
                existing = self.db.card_by_token(token)
                if existing is None:
                    other = self.db.card_by_number(number)
                    if other is not None:
                        self.db.delete_card(other["id"])
                    self.db.add_card(person_id, number, token, enabled)
                    added += 1
                else:
                    self.db.update_card(existing["id"], person_id=person_id, number=number, enabled=enabled)
                seen_cards.add(token)
            removed = 0
            for card in self.db.all_cards():
                if card["axis_credential_token"] not in seen_cards:
                    self.db.delete_card(card["id"])
                    removed += 1
            for person in self.db.all_persons():
                if not self.db.cards_of(person["id"]):
                    self.db.delete_person(person["id"])
            return {"cards": len(seen_cards), "added": added, "removed": removed}

    # Anagrafica

    def _credential(self, user_token: str, name: str, number: str) -> dict:
        if not self.settings.access_profile:
            raise ServiceError("profilo di accesso non configurato: impostalo nelle opzioni dell'add-on")
        return {
            "token": "",
            "UserToken": user_token,
            "Description": name,
            "ValidFrom": "",
            "ValidTo": "",
            "Enabled": True,
            "Status": "Enabled",
            "IdData": [{"Name": "Card", "Value": number}],
            "Attribute": [],
            "AuthenticationProfile": [],
            "CredentialAccessProfile": [
                {"AccessProfile": self.settings.access_profile, "ValidFrom": "", "ValidTo": ""}
            ],
        }

    def _check_card_free(self, number: str) -> None:
        existing = self.db.card_by_number(number)
        if existing:
            raise ServiceError(f"la tessera {number} è già registrata a nome {existing['person_name']}")

    async def create_person(self, name: str, card: str, user: str) -> dict:
        name, number = normalize_name(name), normalize_card(card)
        async with self._lock:
            self._check_card_free(number)
            user_token = user_token_for(name)
            await self.controller.set_user(
                {"token": user_token, "Name": name, "Description": "Registrato da Axis Access Manager", "Attribute": []}
            )
            try:
                credential_token = await self.controller.set_credential(self._credential(user_token, name, number))
            except VapixError:
                # Senza tessera l'utente resterebbe orfano sul controller
                try:
                    await self.controller.remove_user(user_token)
                except VapixError:
                    _LOGGER.warning("Utente %s rimasto sul controller dopo un errore", user_token)
                raise
            person_id = self.db.add_person(name, user_token)
            self.db.add_card(person_id, number, credential_token)
            self.db.add_audit(user, "persona creata", f"{name}, tessera {number}")
            return self.db.get_person(person_id)

    async def add_card(self, person_id: int, card: str, user: str) -> dict:
        number = normalize_card(card)
        async with self._lock:
            person = self._person(person_id)
            self._check_card_free(number)
            credential_token = await self.controller.set_credential(
                self._credential(person["axis_user_token"], person["name"], number)
            )
            self.db.add_card(person_id, number, credential_token)
            self.db.add_audit(user, "tessera aggiunta", f"{person['name']}, tessera {number}")
            return self.db.get_person(person_id)

    async def rename_person(self, person_id: int, name: str, user: str) -> dict:
        name = normalize_name(name)
        async with self._lock:
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

    async def set_card_enabled(self, card_id: int, enabled: bool, user: str) -> dict:
        async with self._lock:
            card = self._card(card_id)
            if enabled:
                await self.controller.enable_credential(card["axis_credential_token"])
            else:
                await self.controller.disable_credential(card["axis_credential_token"])
            self.db.set_card_enabled(card_id, enabled)
            person = self.db.get_person(card["person_id"])
            self.db.add_audit(
                user,
                "tessera riattivata" if enabled else "tessera sospesa",
                f"{person['name']}, tessera {card['number']}",
            )
            return person

    async def delete_card(self, card_id: int, user: str) -> dict | None:
        """Elimina la tessera; se era l'ultima elimina anche la persona. Restituisce la persona rimasta."""
        async with self._lock:
            card = self._card(card_id)
            person = self.db.get_person(card["person_id"])
            await self.controller.remove_credential(card["axis_credential_token"])
            self.db.delete_card(card_id)
            self.db.add_audit(user, "tessera eliminata", f"{person['name']}, tessera {card['number']}")
            if len(person["cards"]) > 1:
                return self.db.get_person(person["id"])
            await self._remove_axis_user(person)
            self.db.delete_person(person["id"])
            return None

    async def delete_person(self, person_id: int, user: str) -> None:
        async with self._lock:
            person = self._person(person_id)
            for card in person["cards"]:
                await self.controller.remove_credential(card["axis_credential_token"])
                self.db.delete_card(card["id"])
            await self._remove_axis_user(person)
            self.db.delete_person(person_id)
            numbers = ", ".join(card["number"] for card in person["cards"])
            self.db.add_audit(user, "persona eliminata", f"{person['name']} ({numbers})")

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

    async def open_door(self, extended: bool, user: str) -> None:
        """Sblocco temporaneo, solo a serratura bloccata: la serratura non regge l'alimentazione continua."""
        door = self.settings.door_token
        mode = self.monitor.door_mode if self.monitor else None
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
            self.monitor.door_mode = "Accessed"
        detail = "tempo esteso" if extended else ""
        self.db.add_event(ts=now_iso(), kind="apertura_remota", detail=detail, source=user)
        self.db.add_audit(user, "porta aperta", detail)

    # Eventi

    def record_event(self, event: Event, retained: bool = False) -> None:
        """Salva nel log gli eventi utili. I messaggi conservati dal broker sono già stati registrati."""
        if retained:
            return
        ts = event.timestamp.isoformat(timespec="seconds")
        data = event.data
        topic = event.topic
        if topic.startswith("IdPoint/Request/IdData"):
            number = str(data.get("Card") or "")
            if not number:
                return
            card = self.db.card_by_number(number)
            if card is None:
                self.db.note_unknown_card(number, ts)
                self.db.add_event(ts=ts, kind="sconosciuta", card=number, source="lettore", raw=data)
            else:
                self.db.add_event(
                    ts=ts,
                    kind="lettura",
                    card=card["number"],
                    person_id=card["person_id"],
                    person_name=card["person_name"],
                    detail="" if card["enabled"] else "tessera sospesa",
                    source="lettore",
                    raw=data,
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
            self.db.add_event(
                ts=ts,
                kind="concesso" if granted else "negato",
                card=card["number"] if card else number,
                person_id=card["person_id"] if card else None,
                person_name=card["person_name"] if card else str(data.get("CredentialHolderName") or ""),
                detail="" if granted else DENIED_REASONS.get(reason, reason),
                source="controller",
                raw=data,
            )
        elif topic.startswith("Door/State/DoorAlarm/") and data.get("State") not in (None, "Normal"):
            state = str(data["State"])
            self.db.add_event(ts=ts, kind="allarme", detail=DOOR_ALARMS.get(state, state), source="porta", raw=data)
        elif topic.startswith("Door/State/DoorTamper/") and data.get("State") == "TamperDetected":
            self.db.add_event(ts=ts, kind="manomissione", detail="manomissione porta", source="porta", raw=data)
        elif topic.startswith("Device/Casing/Open") and str(data.get("Open")) == "1":
            self.db.add_event(ts=ts, kind="manomissione", detail="alloggiamento controller aperto", source="controller")
