"""Logica di gestione: anagrafica sincronizzata con l'A1210, gruppi e validità, eventi, apertura porta."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import unicodedata
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from .backup_copy import error_reason
from .config import Settings
from .db import Database, now_iso
from .events import Event, EventMonitor, door_of_topic, event_from_log
from .rules import RuleError, check_validity, normalize_validity
from .vapix import A1210Client, ControllerUnreachable, VapixError

_LOGGER = logging.getLogger(__name__)
# Oltre questo scarto l'ora del controller non è credibile neanche se dice di essere sincronizzato
MAX_CLOCK_CORRECTION = 24 * 3600
# Eventi del registro letti per trovare l'ultimo consenso a una porta (rete di sicurezza senza eventi in diretta)
GRANT_LOG_TAIL = 30

CARD_PATTERN = re.compile(r"^[0-9A-Fa-f]{2,64}$")

# Ruoli degli ingressi del controller che aprono la porta: tipo di apertura per il registro
INPUT_SOURCES = {"rex": "Pulsante Uscita", "intercom": "Relè Citofono"}
CASING_OPEN = "coperchio del controller aperto"
TILTED = "controller inclinato"
# Eventi uguali e ravvicinati (rimbalzi di un contatto, coperchio aperto e chiuso più volte) in una riga sola:
# finestra in secondi dalla riga già nel registro e parola aggiunta al dettaglio
REPEAT_WINDOWS = {"uscita": 10, "apertura_remota": 10, "negato": 10, "manomissione": 300}
REPEATED = {
    "uscita": "più pressioni",
    "apertura_remota": "più pressioni",
    "negato": "più pressioni",
    "manomissione": "ripetuta",
}
INPUT_TERMINALS = {"REX": "REX", "IO1": "AUX 1", "IO2": "AUX 2"}
DENIED_REASONS = {
    "CredentialNotEnabled": "tessera sospesa",
    "CredentialNotActive": "tessera non ancora valida",
    "CredentialExpired": "tessera scaduta",
    "Unauthorized": "non autorizzata su questa porta o in questo orario",
    "InvalidAccessProfile": "profilo di accesso non valido",
    "CredentialNotFound": "tessera non registrata",
    "NotPermittedAtThisTime": "fuori fascia oraria",
    "InvalidPIN": "PIN errato",
    "AntipassbackViolation": "passaggio ripetuto (antipassback)",
    "Other": "negato dal controller",
}

DOOR_ALARMS = {
    "DoorForcedOpen": "porta forzata",
    "DoorOpenTooLong": "porta aperta troppo a lungo",
}


# Eventi registrati dal controller: non si registrano due volte (MQTT e recupero dal registro interno)
CONTROLLER_KINDS = ("lettura", "sconosciuta", "concesso", "negato", "uscita", "allarme", "manomissione")
# Allarmi e manomissioni sono anche stato: il controller li rimanda con la loro ora quando il WebSocket si ricollega.
# Uno già nel registro con la stessa impronta e lo stesso secondo è lo stesso, anche se arriva come in diretta
STATE_KINDS = ("allarme", "manomissione")
# Eventi del registro interno del controller che finiscono nel Registro dell'app
RECOVER_TOPICS = (
    "IdPoint/Request/IdData",
    "AccessControl/",
    "Door/State/DoorAlarm/",
    "Door/State/DoorTamper/",
    "Device/Casing/Open",
    "Device/Tampering/",
)
RECOVER_PAGE = 1000
RECOVER_MAX = 30000
# Scrittura di prova con il registro segnato non scrivibile (vedi AccessService.check_writable)
SETTING_WRITE_PROBE = "write_probe"


# Eventi che cambiano solo lo stato mostrato in Home Assistant (oltre a quanto va nel registro)
STATE_TOPICS = (
    "Door/State/DoorAlarm/",
    "Door/State/DoorWarning/",
    "Door/State/DoorTamper/",
    "Device/Casing/Open",
    "Device/Status/SystemReady",
    "Device/IO/Port",
    "Door/State/DoorPhysicalState",
)


# Tessere sparite dal controller in un colpo solo: oltre questa quota è quasi certamente un reset o un cambio
MASS_MISSING_SHARE = 0.1
MASS_MISSING_MIN = 3


def mass_missing(total: int, missing: int) -> bool:
    """Controller vuoto, o sparita più di una piccola quota delle tessere."""
    if not total or not missing:
        return False
    return missing == total or (missing >= MASS_MISSING_MIN and missing > total * MASS_MISSING_SHARE)


def plural(count: int, one: str, many: str) -> str:
    """ "1 tessera", "3 tessere"."""
    return f"{count} {one if count == 1 else many}"


class ServiceError(Exception):
    """Richiesta non valida o non eseguibile: il messaggio va mostrato all'utente."""


class PersonChanged(ServiceError):
    """La persona è cambiata dopo che la richiesta è stata preparata (un'altra scheda, un'importazione, la pagina Da
    Registrare): niente è stato modificato, e va riletta prima di riprovare."""


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
    """Stessa convenzione degli script di Home Assistant: utente_<nome>_<aammgghhmmss>.
    Non è per forza unico: per un utente nuovo AccessService lo controlla e, se serve, aggiunge _2, _3..."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")[:30] or "senza_nome"
    return f"utente_{slug}_{(now or datetime.now()).strftime('%y%m%d%H%M%S')}"


USER_TOKEN = re.compile(r"utente_([a-z0-9_]+)_\d{12}(?:_\d+)?")


def local_time(value: str) -> str:
    """Data e ora di Axis (in UTC) nell'ora locale, per i testi: 31/12/2026 23:59."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return value


def input_detail(io: str, role: str) -> str:
    """Dettaglio nel registro delle pressioni di un ingresso: ruolo e morsetto ("Pulsante Uscita · AUX 1")."""
    return f"{INPUT_SOURCES[role]} · {INPUT_TERMINALS.get(io, io)}"


def event_ref(kind: str, door: str, card: str = "", detail: str = "") -> str:
    """Impronta di un evento del controller, uguale via MQTT e dal registro interno.

    Non dipende dall'anagrafica (una tessera tolta dall'app trasforma una lettura in "sconosciuta" e lascia un
    concesso senza numero) né dai dati che i due canali riportano in modo diverso: un esito per porta e secondo,
    una lettura per porta, tessera e secondo. Stessa formula della migrazione 3 in db.py.
    """
    if kind in ("lettura", "sconosciuta"):
        return f"lettura|{door}|{card.lower()}"
    if kind in ("concesso", "negato"):
        return f"{kind}|{door}"
    return f"{kind}|{door}|{detail}"


def normalize_text(value: str | None, limit: int, label: str) -> str:
    text = (value or "").strip()
    if len(text) > limit:
        # Senza accordo con l'etichetta, che può essere maschile o femminile ("descrizione")
        raise ServiceError(f"{label} oltre il limite di {limit} caratteri")
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
        # Controller di cui questo servizio riceve gli eventi (oggi il principale, lo imposta main): va in ogni evento
        # salvato. None (nessun controller) resta NULL nel registro, che vuol dire il principale
        self.controller_id: int | None = None
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
        # Esiti arrivati in diretta e non salvati (registro non scrivibile): valgono per gli altri topic dello stesso
        # passaggio, non per il recupero, che li deve rimettere nel registro
        self._unsaved_decisions: deque[tuple] = deque(maxlen=20)
        # Registro non scrivibile (disco pieno): {"since", "error"} dal primo evento non salvato, None se si scrive
        self.write_error: dict | None = None
        # Eventi non salvati finora: il recupero dal registro del controller li rimette (vedi main._recover_if_needed)
        self.write_failures = 0
        # Righe dell'app non salvate (Riblocco Forzato, aperture a distanza, citofono): il registro del controller non
        # le ha e il recupero non le rimette. Si salvano appena il registro torna scrivibile, senza mandarle di nuovo a
        # Home Assistant
        self._unsaved_rows: deque[dict] = deque(maxlen=100)
        # Dentro record_event: le righe sono eventi del controller
        self._controller_event = False
        # Punto di accesso → porta e lettore → porta, per sapere a quale porta si riferisce un evento
        self.access_point_doors: dict[str, str] = {}
        self.idpoint_doors: dict[str, str] = {}
        # Un'apertura per volta per porta: il controllo "bloccata" e il comando non si accavallano (anche con il
        # blocco della rete di sicurezza, vedi lock_guard)
        self._door_locks: dict[str, asyncio.Lock] = {}
        # Rete di sicurezza della serratura (lock_guard.LockGuard): consensi ed eventi della porta, None senza
        self.lock_guard = None
        # Durante il recupero dal registro del controller: eventi segnati come recuperati, e quanti
        self._recovering = False
        self._recovered = 0
        # Ora (del controller) dell'evento più recente letto dall'ultimo recupero, None se non ne ha letti
        self.recover_newest: datetime | None = None
        # L'ultimo recupero si è fermato a RECOVER_MAX righe con altre ancora da leggere: si continua al giro dopo
        self.recover_more = False
        # Secondi di cui l'orologio del controller (sincronizzato via NTP) è avanti rispetto a Home Assistant;
        # None se non si sa o se il controller non è sincronizzato
        self.clock_offset: Callable[[], float | None] | None = None
        # Scarto misurato, anche senza NTP: per l'età dei consensi letti dal registro (rete di sicurezza)
        self.measured_clock_offset: Callable[[], float | None] | None = None

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

    def notify_change(self) -> None:
        """Qualcosa da aggiornare in Home Assistant (per esempio il controller è andato offline)."""
        self._changed()

    def _door_open(self, door: str) -> bool | None:
        """Porta aperta secondo il contatto porta (sul controller o su un AUX); None se non si sa."""
        state = self.monitor.door_contact(door) if self.monitor else None
        return {"Open": True, "Closed": False}.get(state or "")

    def door_lock(self, door: str) -> asyncio.Lock:
        """Un comando alla porta per volta: apertura dall'app e blocco della rete di sicurezza."""
        return self._door_locks.setdefault(door, asyncio.Lock())

    async def last_grant(self, door: str) -> tuple[str, float | None] | None:
        """Ultimo consenso (tessera, pulsante) alla porta nel registro del controller: (impronta, secondi fa), None se
        non c'è tra gli ultimi eventi. Per la rete di sicurezza della serratura: senza eventi in diretta, e con gli
        eventi in diretta prima di bloccare per il tempo scaduto."""
        entries = await self.controller.latest_events(GRANT_LOG_TAIL)
        # Scarto misurato dell'orologio del controller, anche senza NTP: senza misura i secondi non si sanno (None)
        offset = self.measured_clock_offset() if self.measured_clock_offset else None
        moment = datetime.now(tz=UTC) + timedelta(seconds=offset or 0)
        for entry in entries:
            event = event_from_log(entry)
            if event and event.topic.startswith("AccessControl/AccessGranted") and self._door_of(event) == door:
                ident = str(entry.get("rowid") or "") or f"{event.timestamp.isoformat()}|{event.topic}"
                age = (moment - event.timestamp).total_seconds() if offset is not None else None
                return ident, age
        return None

    def log_lock_alarm(self, door: str, detail: str) -> None:
        """Allarme della rete di sicurezza della serratura (vedi lock_guard) nel registro e in Home Assistant."""
        self.log_event(
            ts=self.event_now(),
            kind="allarme",
            detail=detail,
            source="App",
            door=door,
            ref=event_ref("allarme", door, detail=detail),
        )

    def log_event(self, **fields) -> dict | None:
        """Salva un evento e avvisa chi lo deve elaborare. None se era già nel registro.

        Un evento recuperato dal registro del controller può essere già arrivato via MQTT, e un evento MQTT
        in ritardo può essere già stato recuperato: in quei casi non si registra due volte.
        """
        ref = fields.get("ref") or ""
        if ref and self._known(fields["kind"], fields["ts"], ref):
            return None
        fields.setdefault("controller_id", self.controller_id)
        if self._recovering:
            fields["recovered"] = True
            self._recovered += 1
        try:
            event_id = self.db.add_event(**fields)
        except sqlite3.Error as err:
            self.note_write_error(err)
            if self._recovering:
                # Il recupero si ferma qui e riparte al giro dopo, dallo stesso punto
                raise
            # In diretta l'evento arriva lo stesso a Home Assistant (allarmi, accessi, apertura riuscita); nel
            # registro lo rimette il recupero, se è del controller, se no lo salva la prima scrittura che riesce.
            # Prima si perdeva in silenzio
            _LOGGER.warning("Evento non salvato nel registro: %s %s", fields.get("kind"), fields.get("door") or "")
            event_id = None
            if not self._controller_event:
                self._unsaved_rows.append(dict(fields))
        else:
            # Nel recupero la riga è salvata solo al commit della pagina (vedi recover_events)
            if not self._recovering:
                self.writable_again()
                self.check_writable()
        row = {**fields, "id": event_id}
        row.pop("raw", None)
        if self.after_event:
            self._spawn(self.after_event(row))
        if not self._recovering:
            # Nel recupero Home Assistant si aggiorna una volta per pagina, non per ognuna delle migliaia di righe
            self._changed()
        return row

    def writable_again(self) -> None:
        """Una scrittura nel registro è riuscita: il guasto «Registro Non Scrivibile» si toglie."""
        if self.write_error is not None:
            _LOGGER.info("Registro di nuovo scrivibile")
            self.write_error = None
            self._changed()

    def check_writable(self) -> None:
        """Righe dell'app rimaste fuori dal registro: si salvano con la loro ora, senza mandarle di nuovo a Home
        Assistant. Con il registro segnato non scrivibile e niente da salvare si prova una scrittura piccola: prima il
        guasto restava finché non si salvava un evento nuovo, anche con lo spazio di nuovo libero."""
        if self.write_error is None and not self._unsaved_rows:
            return
        try:
            if not self._unsaved_rows:
                self.db.set_setting(SETTING_WRITE_PROBE, now_iso())
            while self._unsaved_rows:
                self.db.add_event(**self._unsaved_rows[0])
                self._unsaved_rows.popleft()
        except sqlite3.Error as err:
            _LOGGER.debug("Registro ancora non scrivibile: %s", err)
            return
        self.writable_again()

    def note_write_error(self, err: sqlite3.Error) -> None:
        """Registro non scrivibile (disco pieno): guasto finché una scrittura non riesce, e recupero da rifare."""
        self.write_failures += 1
        if self.write_error is None:
            _LOGGER.error("Registro non scrivibile: %s", err)
            self.write_error = {"since": now_iso(), "error": error_reason(err)}
            self._changed()

    def _known(self, kind: str, ts: str, ref: str) -> bool:
        """Evento del controller già nel registro: arrivato in diretta e recuperato (o il contrario), oppure allarme o
        manomissione rimandati come stato con la loro ora."""
        if self.db.similar_event(ts=ts, ref=ref, only_recovered=not self._recovering):
            return True
        return kind in STATE_KINDS and self.db.similar_event(ts=ts, ref=ref, window=0)

    # Sincronizzazione

    async def sync_from_controller(self) -> dict:
        """Allinea il database a utenti e tessere presenti sul controller, che resta la fonte di verità."""
        paused = getattr(self.controller, "paused", "")
        if paused:
            raise ServiceError(f"sincronizzazione sospesa: {paused}")
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
            # Una scrittura per tessera cambiata e un solo commit: con centinaia di tessere su una scheda SD
            # un commit per riga fermava l'add-on (comandi di apertura compresi) per secondi
            with self.db.batch():
                for credential in credentials:
                    token = credential.get("token") or ""
                    number = card_of_credential(credential)
                    if not token or not number:
                        continue
                    existing = self.db.card_by_token(token)
                    holder = str(credential.get("UserToken") or "")
                    user_token = holder or f"credenziale:{token}"
                    person = self.db.person_by_user_token(user_token)
                    if person is None and existing is not None and (not holder or holder.startswith("credenziale:")):
                        # Tessera senza utente già nota: resta alla sua persona. Succede a chi era stato letto da
                        # una tessera senza utente e ne ha ricevuto uno dall'app, se la tessera non è ancora passata
                        # all'utente nuovo: senza, diventava una persona nuova e l'originale spariva
                        person_id = existing["person_id"]
                    else:
                        name = names.get(user_token) or (credential.get("Description") or "").strip() or "(Senza nome)"
                        if person is None:
                            person_id = self.db.add_person(name, user_token)
                        else:
                            person_id = person["id"]
                            if person["name"] != name:
                                self.db.rename_person(person_id, name)
                    enabled = bool(credential.get("Enabled", True))
                    access = access_of_credential(credential)
                    if existing is None:
                        other = self.db.card_by_number(number)
                        if other is not None:
                            self.db.delete_card(other["id"])
                        self.db.add_card(person_id, number, token, enabled, access)
                        added += 1
                    elif self._card_changed(existing, person_id, number, enabled, access):
                        if number != existing["number"]:
                            # Numero passato a questa tessera da un'altra (tessera riassegnata sul controller):
                            # l'altra, rimasta nell'app come non presente, se ne va come per una tessera nuova
                            other = self.db.card_by_number(number)
                            if other is not None and other["id"] != existing["id"]:
                                self.db.delete_card(other["id"])
                            self.db.forget_unknown_card(number)
                        self.db.update_card(
                            existing["id"], person_id=person_id, number=number, enabled=enabled, access=access
                        )
                    seen_cards.add(token)
                # Tessere sparite dal controller: mai cancellate da sole. Si segnano e decide l'utente
                # (riscriverle o toglierle dall'app): un controller resettato o sostituito non svuota l'anagrafica.
                # Neanche le persone rimaste senza tessere (passate a un altro utente) se ne vanno: solo a mano
                cards = self.db.all_cards()
                missing = [c for c in cards if c["axis_credential_token"] not in seen_cards]
                new_missing = [c["id"] for c in missing if not c["missing_since"]]
                if new_missing:
                    self.db.mark_cards_missing(new_missing, now_iso())
                    _LOGGER.warning("%s tessere non sono più sul controller: segnate, non cancellate", len(new_missing))
            try:
                await self.load_access_points()
            except VapixError as err:
                _LOGGER.debug("Punti di accesso non leggibili: %s", err)
            self._changed()
            return {
                "cards": len(seen_cards),
                "added": added,
                "removed": 0,
                "missing": len(missing),
                "new_missing": len(new_missing),
                "mass_missing": mass_missing(len(cards), len(missing)),
            }

    @staticmethod
    def _card_changed(card: dict, person_id: int, number: str, enabled: bool, access: dict) -> bool:
        """La tessera nel database è diversa da come la legge il controller (o era segnata come non presente)."""
        return (
            card["person_id"] != person_id
            or card["number"] != number
            or bool(card["enabled"]) != enabled
            or bool(card["missing_since"])
            or any((card[key] or "") != value for key, value in access.items())
        )

    async def load_access_points(self) -> None:
        """Porta di ogni punto di accesso e di ogni lettore, per assegnare gli eventi alla porta giusta."""
        points = await self.controller.list_access_points()
        self.access_point_doors = {p.get("token"): p.get("Entity") or "" for p in points if p.get("token")}
        readers: dict[str, set[str]] = {}
        for point in points:
            for device in point.get("IdPointDevice") or []:
                if device.get("IdPoint") and point.get("Entity"):
                    readers.setdefault(device["IdPoint"], set()).add(point["Entity"])
        # Un lettore di due porte non dice quale: resta la porta principale
        self.idpoint_doors = {reader: next(iter(doors)) for reader, doors in readers.items() if len(doors) == 1}

    async def restore_missing(self, user: str, card_ids: list[int] | None = None) -> dict:
        """Riscrive sul controller le tessere non presenti (tutte o quelle indicate), con gruppo e validità salvati."""
        restored, failed = 0, []
        async with self.lock:
            cards = [c for c in self.db.missing_cards() if card_ids is None or c["id"] in card_ids]
            known_users: set[str] = set()
            for card in cards:
                person = self.db.get_person(card["person_id"])
                try:
                    # Tessera importata senza utente: se ne crea uno, che prende anche le altre tessere della persona
                    user_token = await self._ensure_user(person, "Riscritto da Axis Access Manager")
                    if user_token != person["axis_user_token"]:
                        known_users.add(user_token)
                    if user_token not in known_users and not await self.controller.get_users([user_token]):
                        await self.controller.set_user(
                            {
                                "token": user_token,
                                "Name": person["name"],
                                "Description": "Riscritto da Axis Access Manager",
                                "Attribute": [],
                            }
                        )
                    known_users.add(user_token)
                    access = {k: card[k] for k in ("access_profile", "valid_from", "valid_to")}
                    if not access["access_profile"]:
                        access["access_profile"] = self.default_group
                    credential = self._credential(user_token, person["name"], card["number"], access, card["enabled"])
                    token = await self.controller.set_credential(credential)
                except VapixError as err:
                    failed.append({"card": card["number"], "person": person["name"], "error": str(err)})
                    continue
                self.db.set_card_token(card["id"], token)
                restored += 1
            if restored:
                self.db.add_audit(user, "tessere riscritte sul controller", plural(restored, "tessera", "tessere"))
                self._changed()
        return {"restored": restored, "failed": failed}

    def forget_missing(self, user: str, card_ids: list[int] | None = None) -> dict:
        """Toglie dall'app le tessere non presenti sul controller. Le persone restano, anche senza tessere: si
        eliminano solo a mano."""
        if self.lock.locked():
            # Per esempio "Riscrivi" in corso: toglierle a metà le lascerebbe scritte sul controller senza persona
            raise ServiceError("è in corso un'altra operazione sul controller: riprova tra qualche secondo")
        cards = [c for c in self.db.missing_cards() if card_ids is None or c["id"] in card_ids]
        with self.db.batch():
            for card in cards:
                self.db.delete_card(card["id"])
        if cards:
            numbers = ", ".join(c["number"] for c in cards[:20]) + ("…" if len(cards) > 20 else "")
            self._audit(
                user, "tessere non presenti tolte dall'app", f"{plural(len(cards), 'tessera', 'tessere')}: {numbers}"
            )
            self._changed()
        return {"cards": len(cards)}

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
            raise ServiceError("nessuna tipologia scelta e nessuna predefinita: impostala in Persone → Tipologie")
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

    def _credential(self, user_token: str, name: str, number: str, access: dict, enabled: bool = True) -> dict:
        return self._apply_access(
            {
                "token": "",
                "UserToken": user_token,
                "Description": name,
                "Enabled": bool(enabled),
                "Status": "Enabled" if enabled else "Disabled",
                "IdData": [{"Name": "Card", "Value": number}],
                "Attribute": [],
                "AuthenticationProfile": [],
            },
            access,
        )

    def _category_access(
        self, category: dict | None, group: str = "", valid_from: str = "", valid_to: str = ""
    ) -> dict:
        """Gruppo e validità di una tessera nuova: la tipologia decide gruppo e durata, se non sono stati scelti a
        mano."""
        if category:
            group = group or category["group_token"]
            if category["valid_days"] and not valid_to:
                start = datetime.now(tz=UTC).replace(microsecond=0)
                valid_from = valid_from or start.isoformat()
                valid_to = (start + timedelta(days=category["valid_days"])).isoformat()
        return self._access(group, valid_from, valid_to)

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
        enabled: bool = True,
    ) -> dict:
        """card vuota: persona senza tessera, solo nell'app. enabled False: la tessera si scrive già sospesa
        (importazione con stato «sospesa»)."""
        name = normalize_name(name)
        number = normalize_card(card) if (card or "").strip() else ""
        company = normalize_text(company, 100, "ditta o reparto")
        notes = normalize_text(notes, 1000, "testo delle note")
        # Senza una scelta vale la tipologia predefinita
        category = self._category(category_id or int(self.db.get_setting("default_category") or 0))
        if not number:
            if group or valid_from or valid_to:
                raise ServiceError("gruppo e validità valgono per le tessere: senza tessera non si possono salvare")
            return self._create_without_card(name, user, company, notes, category)
        access = self._category_access(category, group, valid_from, valid_to)
        async with self.lock:
            self._check_card_free(number)
            user_token = await self._new_user_token(name)
            await self.controller.set_user(
                {"token": user_token, "Name": name, "Description": "Registrato da Axis Access Manager", "Attribute": []}
            )
            try:
                credential_token = await self.controller.set_credential(
                    self._credential(user_token, name, number, access, enabled)
                )
            except VapixError:
                # Senza tessera l'utente resterebbe orfano sul controller
                try:
                    await self.controller.remove_user(user_token)
                except VapixError:
                    _LOGGER.warning("Utente %s rimasto sul controller dopo un errore", user_token)
                raise
            try:
                # Tutto o niente: una persona senza la sua tessera non resta nell'app
                with self.db.batch():
                    person_id = self.db.add_person(name, user_token)
                    if company or notes:
                        self.db.update_person_details(person_id, company, notes)
                    if category:
                        self.db.set_person_category(person_id, category["id"])
                    self.db.add_card(person_id, number, credential_token, enabled, access)
            except sqlite3.Error as err:
                # Disco pieno: la tessera appena scritta non resta attiva sul controller senza essere nell'app. Prima
                # restava, e riprovando se ne aggiungeva un'altra con lo stesso numero
                await self._remove_new(credential_token, user_token)
                raise ServiceError(f"{error_reason(err)}: la persona non è stata registrata") from err
            detail = f"{name}, tessera {number}" + ("" if enabled else " sospesa")
            detail += f", tipologia {category['name']}" if category else ""
            self._audit(user, "persona creata", detail)
            self._changed()
            return self.db.get_person(person_id)

    def _create_without_card(self, name: str, user: str, company: str, notes: str, category: dict | None) -> dict:
        """Persona senza tessera: solo nel database, nessuna chiamata al controller (anche spento o con le scritture
        sospese). Il suo utente del controller nasce con la prima tessera, e la durata della tipologia vale da lì."""
        try:
            with self.db.batch():
                person_id = self.db.add_person(name, None)
                if company or notes:
                    self.db.update_person_details(person_id, company, notes)
                if category:
                    self.db.set_person_category(person_id, category["id"])
        except sqlite3.Error as err:
            raise ServiceError(f"{error_reason(err)}: la persona non è stata registrata") from err
        detail = f"{name}, senza tessera" + (f", tipologia {category['name']}" if category else "")
        self._audit(user, "persona creata", detail)
        self._changed()
        return self.db.get_person(person_id)

    async def _remove_new(self, credential_token: str, user_token: str = "") -> None:
        """Tessera (e utente) appena scritti sul controller e non registrati nell'app: si tolgono. Se non si può, la
        sincronizzazione la porta nell'app come persona."""
        try:
            await self.controller.remove_credential(credential_token)
            if user_token:
                await self.controller.remove_user(user_token)
        except VapixError as err:
            _LOGGER.warning("Tessera %s rimasta sul controller dopo un errore del database: %s", credential_token, err)

    def _audit(self, user: str, action: str, detail: str) -> None:
        """Riga di Attività di una modifica già fatta: con il disco pieno la modifica resta, e non risulta un errore."""
        try:
            self.db.add_audit(user, action, detail)
        except sqlite3.Error as err:
            _LOGGER.warning("Attività non salvata (%s: %s): %s", action, detail, err)

    async def add_card(
        self,
        person_id: int,
        card: str,
        user: str,
        enabled: bool = True,
        *,
        group: str = "",
        valid_from: str = "",
        valid_to: str = "",
        category_id: int = 0,
    ) -> dict:
        """enabled False: la tessera si scrive già sospesa (importazione con stato «sospesa»).

        Gruppo, validità e tipologia (0: quella della persona) valgono per la prima tessera della persona, come per
        una persona nuova con gli stessi valori (importazione): la tipologia decide gruppo e durata se non sono stati
        scelti, e la persona passa alla tipologia scelta. Le tessere dopo la prima ereditano l'accesso di quelle che la
        persona ha già: se nel frattempo ne ha avuta una, con valori scelti la tessera non si aggiunge."""
        number = normalize_card(card)
        chosen = self._category(category_id)
        async with self.lock:
            person = self._person(person_id)
            self._check_card_free(number)
            if person["cards"]:
                if group or valid_from or valid_to or chosen:
                    # Prenderebbe l'accesso delle altre tessere, non quello chiesto: niente tessera
                    raise PersonChanged(
                        f"{person['name']} ha già {plural(len(person['cards']), 'tessera', 'tessere')}: la nuova ne "
                        "prenderebbe l'accesso e non quello chiesto, riprova"
                    )
                # La nuova tessera eredita gruppo e validità di quelle che la persona ha già
                first = person["cards"][0]
                access = self._access(first.get("access_profile"), first.get("valid_from"), first.get("valid_to"))
            else:
                # La prima: gruppo e durata della tipologia (scelta, altrimenti quella della persona), da adesso
                category = chosen or (self.db.get_category(person["category_id"]) if person["category_id"] else None)
                access = self._category_access(category, group, valid_from, valid_to)
            moved = chosen is not None and chosen["id"] != person["category_id"]
            user_token, first_card = await self._user_for_card(person)
            credential_token = await self.controller.set_credential(
                self._credential(user_token, person["name"], number, access, enabled)
            )
            try:
                # Tutto o niente: la tipologia cambia insieme alla tessera che ne ha l'accesso
                with self.db.batch():
                    self.db.add_card(person_id, number, credential_token, enabled, access)
                    if moved:
                        self.db.set_person_category(person_id, chosen["id"])
            except sqlite3.Error as err:
                # Come per una persona nuova: la tessera non resta attiva sul controller senza essere nell'app, e
                # senza tessere neanche l'utente
                await self._remove_new(credential_token, user_token if first_card else "")
                raise ServiceError(f"{error_reason(err)}: la tessera non è stata aggiunta") from err
            self._changed()
            detail = f"{person['name']}, tessera {number}" + ("" if enabled else " sospesa")
            self._audit(user, "tessera aggiunta", detail)
            if moved:
                self._audit(user, "tipologia cambiata", f"{person['name']}: {chosen['name']}")
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
                # Senza tessere l'utente può non esserci più (tolto con l'ultima): non si ricrea per il nome
                if users or person["cards"]:
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
        with_validity: bool = True,
    ) -> dict:
        """Gruppo e validità per tutte le tessere della persona; category_id None lascia la tipologia com'è.

        with_validity False: la richiesta non dice la validità (il modulo di una persona senza tessere). Se intanto la
        persona ne ha avuta una non si cambia niente: le date vuote le toglierebbero la scadenza.

        Senza tessere gruppo e validità non hanno dove stare: vale solo la tipologia, e la prima tessera ne prenderà
        gruppo e durata."""
        requested = (group or "").strip()
        category = self._category(category_id)
        if category and not group:
            group = category["group_token"]
        access = self._access(group, valid_from, valid_to)
        async with self.lock:
            person = self._person(person_id)
            if person["cards"] and not with_validity:
                # Scheda aperta quando non ne aveva: la tessera arrivata nel frattempo resterebbe senza scadenza
                raise PersonChanged(
                    f"{person['name']} ha ora {plural(len(person['cards']), 'tessera', 'tessere')}: la modifica era "
                    "stata preparata quando non ne aveva, rileggi e riprova"
                )
            if not person["cards"] and (access["valid_from"] or access["valid_to"]):
                # La validità sta sulle tessere: la prima prende la durata della tipologia
                raise ServiceError("senza tessere la validità non si può salvare: aggiungi prima una tessera")
            if not person["cards"] and requested:
                # Anche il gruppo: la prima prende quello della tipologia (scelta o quella di adesso) o il predefinito.
                # Un altro risulterebbe scelto in Attività senza essere salvato da nessuna parte
                own = self.db.get_category(person["category_id"]) if person["category_id"] else None
                target = category if category_id is not None else own
                if requested != ((target["group_token"] if target else "") or self.default_group):
                    raise ServiceError(
                        "senza tessere il gruppo non si può salvare: la prima tessera prende quello della tipologia, "
                        "aggiungi prima una tessera"
                    )
            # Prima si leggono tutte le tessere presenti: se ne manca una non si cambia niente. Quelle già segnate
            # come non presenti prendono il nuovo accesso solo nell'app, e "Riscrivi" le riscrive con quello
            present = [card for card in person["cards"] if not card["missing_since"]]
            previous: list[dict] = []
            for card in present:
                credentials = await self.controller.get_credentials([card["axis_credential_token"]])
                if not credentials:
                    raise ServiceError(
                        f"la tessera {card['number']} non è più sul controller: riscrivila o toglila dall'app"
                    )
                previous.append(credentials[0])
            # Da riportare com'erano se una scrittura non riesce, compresa quella in corso: senza una risposta in
            # tempo il controller può averla applicata lo stesso
            attempted: list[dict] = []
            try:
                for credential in previous:
                    attempted.append(credential)
                    await self.controller.set_credential(self._apply_access(dict(credential), access))
            except VapixError:
                # Tutto o niente: le tessere già scritte tornano all'accesso di prima
                for credential in attempted:
                    try:
                        await self.controller.set_credential(credential)
                    except VapixError:
                        _LOGGER.warning("Accesso della tessera %s non riportato com'era", credential.get("token"))
                raise
            for card in person["cards"]:
                self.db.set_card_access(card["id"], access)
            if category_id is not None and category_id != person["category_id"]:
                self.db.set_person_category(person_id, category_id)
                self.db.add_audit(
                    user, "tipologia cambiata", f"{person['name']}: {category['name'] if category else 'nessuna'}"
                )
            if not person["cards"]:
                # Nessun accesso scritto da nessuna parte: in Attività solo la tipologia, se è cambiata
                return self.db.get_person(person_id)
            detail = f"{person['name']}: {await self._profile_label(access['access_profile'])}"
            if access["valid_from"] and access["valid_to"]:
                detail += f", valida dal {local_time(access['valid_from'])} al {local_time(access['valid_to'])}"
            elif access["valid_from"]:
                detail += f", valida dal {local_time(access['valid_from'])}"
            elif access["valid_to"]:
                detail += f", valida fino al {local_time(access['valid_to'])}"
            self.db.add_audit(user, "accesso modificato", detail)
            return self.db.get_person(person_id)

    async def _profile_label(self, token: str) -> str:
        """Accesso da mostrare nelle attività: la tipologia che usa il profilo, o il suo nome sul controller."""
        category = next((c for c in self.db.list_categories() if c["group_token"] == token), None)
        if category:
            return f"tipologia {category['name']}"
        try:
            profiles = await self.controller.get_access_profiles([token])
        except VapixError:
            profiles = []
        name = (profiles[0].get("Name") or "") if profiles else ""
        return f"profilo {name}" if name else "accesso personalizzato"

    async def set_card_enabled(self, card_id: int, enabled: bool, user: str) -> dict:
        async with self.lock:
            card = self._card(card_id)
            if enabled:
                await self.controller.enable_credential(card["axis_credential_token"])
            else:
                await self.controller.disable_credential(card["axis_credential_token"])
            try:
                self.db.set_card_enabled(card_id, enabled)
            except sqlite3.Error as err:
                # Disco pieno. Una tessera riattivata torna sospesa sul controller, come la mostra l'app; una sospesa
                # resta sospesa, e l'app la rilegge alla sincronizzazione. Prima l'errore era senza motivo
                reason = error_reason(err)
                if not enabled:
                    raise ServiceError(
                        f"{reason}: la tessera è sospesa sul controller, l'app lo mostrerà quando lo spazio torna"
                    ) from err
                try:
                    await self.controller.disable_credential(card["axis_credential_token"])
                except VapixError:
                    _LOGGER.warning("Tessera %s non riportata sospesa sul controller", card["number"])
                raise ServiceError(f"{reason}: la tessera non è stata riattivata") from err
            self._changed()
            person = self.db.get_person(card["person_id"])
            self._audit(
                user,
                "tessera riattivata" if enabled else "tessera sospesa",
                f"{person['name']}, tessera {card['number']}",
            )
            return person

    async def delete_card(self, card_id: int, user: str) -> dict:
        """Elimina la tessera e restituisce la persona, che resta in anagrafica anche senza tessere. Con l'ultima si
        toglie dal controller anche il suo utente, e la tessera aggiunta dopo ne crea uno nuovo."""
        async with self.lock:
            card = self._card(card_id)
            person = self.db.get_person(card["person_id"])
            if not card["missing_since"]:
                await self.controller.remove_credential(card["axis_credential_token"])
            self.db.delete_card(card_id)
            self._audit(user, "tessera eliminata", f"{person['name']}, tessera {card['number']}")
            # Un utente non tolto (controller che non risponde più) resta legato alla persona: lo riusa la prossima
            # tessera, e lo toglie «Elimina Persona»
            if len(person["cards"]) == 1 and await self._remove_axis_user(person):
                try:
                    self.db.set_person_user_token(person["id"], None)
                except sqlite3.Error as err:
                    # Disco pieno: il token resta, e la prossima tessera riscrive lo stesso utente
                    _LOGGER.warning("Utente di %s tolto dal controller, token non azzerato: %s", person["name"], err)
            self._changed()
            return self.db.get_person(person["id"])

    async def delete_person(self, person_id: int, user: str, cards: set[int] | None = None) -> None:
        """cards: le tessere (id) che ha visto chi chiede di eliminarla, cioè quelle elencate nella conferma; se intanto
        ne ha avuta una nuova non si elimina niente. Una tessera già tolta (un'eliminazione rimasta a metà da
        riprovare) non blocca. None: tutte quelle che ha."""
        async with self.lock:
            person = self._person(person_id)
            new = [card for card in person["cards"] if cards is not None and card["id"] not in cards]
            if new:
                # Per esempio «Non ha tessere» su una scheda vecchia, e intanto una tessera assegnata
                raise PersonChanged(
                    f"{person['name']} ha ora {plural(len(person['cards']), 'tessera', 'tessere')}: conferma "
                    "l'eliminazione dalla scheda aggiornata"
                )
            for card in person["cards"]:
                if not card["missing_since"]:
                    await self.controller.remove_credential(card["axis_credential_token"])
                self.db.delete_card(card["id"])
            await self._remove_axis_user(person)
            self.db.delete_person(person_id)
            numbers = ", ".join(card["number"] for card in person["cards"])
            detail = f"{person['name']} ({numbers})" if numbers else f"{person['name']}, senza tessere"
            # Con il disco pieno la persona è già eliminata: non risulta un errore
            self._audit(user, "persona eliminata", detail)
            self._changed()

    # Ripristino da backup

    async def restore_person(self, saved: dict, cards: list[dict], category_id: int, user: str) -> dict:
        """Riporta una persona com'era in un backup: nome, ditta, note, tipologia e tessere con accesso e stato.
        Se non c'è più si ricrea, con lo stesso numero e la stessa data di creazione se il numero è libero.
        cards: tessere del backup già controllate (numero libero o già della persona, profilo che esiste); le altre
        tessere della persona restano come sono."""
        current = self.db.get_person(saved["id"])
        if current is not None and current["created_at"] != saved["created_at"]:
            current = None
        if current is None:
            return await self._recreate_person(saved, cards, category_id, user)
        person_id = current["id"]
        await self.update_person(
            person_id, user, name=saved["name"], company=saved["company"] or "", notes=saved["notes"] or ""
        )
        if category_id != current["category_id"]:
            self.db.set_person_category(person_id, category_id)
        wanted = {c["number"].upper(): c for c in cards}
        async with self.lock:
            person = self._person(person_id)
            # L'utente del controller solo con una tessera da rimettere: senza tessere la persona resta nell'app
            user_token = ""
            have = {c["number"].upper(): c for c in person["cards"]}
            for number, card in wanted.items():
                access = {k: card[k] for k in ("access_profile", "valid_from", "valid_to")}
                mine = have.get(number)
                if mine is None:
                    self._check_card_free(card["number"])
                    if not user_token:
                        user_token, _ = await self._user_for_card(person)
                    token = await self.controller.set_credential(
                        self._credential(user_token, person["name"], card["number"], access)
                    )
                    if not card["enabled"]:
                        try:
                            await self.controller.disable_credential(token)
                        except VapixError:
                            # Come in _recreate_person: una tessera sospesa nel backup non resta attiva sul controller
                            try:
                                await self.controller.remove_credential(token)
                            except VapixError:
                                _LOGGER.warning("Tessera %s rimasta sul controller dopo un errore", token)
                            raise
                    self.db.add_card(person_id, card["number"], token, card["enabled"], access)
                    continue
                if {k: mine[k] for k in access} != access:
                    credentials = await self.controller.get_credentials([mine["axis_credential_token"]])
                    if not credentials:
                        raise ServiceError(
                            f"la tessera {mine['number']} non è più sul controller: riscrivila prima da Persone"
                        )
                    await self.controller.set_credential(self._apply_access(credentials[0], access))
                    self.db.set_card_access(mine["id"], access)
                if bool(mine["enabled"]) != bool(card["enabled"]):
                    if card["enabled"]:
                        await self.controller.enable_credential(mine["axis_credential_token"])
                    else:
                        await self.controller.disable_credential(mine["axis_credential_token"])
                    self.db.set_card_enabled(mine["id"], card["enabled"])
            # Le tessere che non sono in cards (aggiunte dopo il backup) restano: il ripristino le elimina solo
            # con la loro riga, scelta a parte nell'anteprima
            self.db.add_audit(user, "persona ripristinata da backup", person["name"])
            self._changed()
            return self.db.get_person(person_id)

    async def _new_user_token(self, name: str) -> str:
        """Token per un utente nuovo del controller che nessuno ha: due persone con lo stesso nome (o lo stesso
        inizio di un nome lungo) nello stesso secondo finivano sullo stesso utente, e con lui le tessere."""
        base = user_token_for(name)
        token, n = base, 1
        while self.db.person_by_user_token(token) or await self.controller.get_users([token]):
            n += 1
            token = f"{base}_{n}"
        return token

    async def _user_for_card(self, person: dict) -> tuple[str, bool]:
        """Utente del controller per una tessera nuova della persona; True se è la sua prima tessera.

        Chi non ha tessere non ha l'utente sul controller, o può non averlo più (registrato senza tessera, tolto con
        l'ultima, controller resettato): si scrive con il nome di adesso. Il token si salva prima: se la risposta si
        perde, la sincronizzazione porta la tessera a questa persona e non a una nuova."""
        if person["cards"]:
            return await self._ensure_user(person), False
        token = person["axis_user_token"] or ""
        if not token or token.startswith("credenziale:"):
            token = await self._new_user_token(person["name"])
            self.db.set_person_user_token(person["id"], token)
        await self.controller.set_user(
            {
                "token": token,
                "Name": person["name"],
                "Description": "Registrato da Axis Access Manager",
                "Attribute": [],
            }
        )
        return token, True

    async def _ensure_user(self, person: dict, description: str = "Registrato da Axis Access Manager") -> str:
        """Utente del controller della persona; chi è stato letto da una tessera senza utente ne riceve uno."""
        token = person["axis_user_token"] or ""
        if token and not token.startswith("credenziale:"):
            return token
        token = await self._new_user_token(person["name"])
        await self.controller.set_user(
            {
                "token": token,
                "Name": person["name"],
                "Description": description,
                "Attribute": [],
            }
        )
        self.db.set_person_user_token(person["id"], token)
        # Le tessere che ha già sul controller passano al nuovo utente, come quelle registrate dall'app.
        # Se una scrittura non riesce la persona non si spezza: la sincronizzazione lascia le tessere senza utente
        # alla persona che le ha, e l'operazione chiesta va avanti
        for card in person["cards"]:
            if card["missing_since"]:
                continue
            try:
                for credential in await self.controller.get_credentials([card["axis_credential_token"]]):
                    credential["UserToken"] = token
                    await self.controller.set_credential(credential)
            except VapixError as err:
                _LOGGER.warning("Tessera %s non passata all'utente %s: %s", card["number"], token, err)
        return token

    async def _recreate_person(self, saved: dict, cards: list[dict], category_id: int, user: str) -> dict:
        name = normalize_name(saved["name"])
        if not cards:
            # Senza tessere (nel backup, o passate ad altri): solo nell'app, come una persona nuova senza tessera
            try:
                with self.db.batch():
                    person_id = self._add_saved_person(saved, name, None, category_id)
            except sqlite3.Error as err:
                raise ServiceError(f"{error_reason(err)}: la persona non è stata ricreata") from err
            self._audit(user, "persona ricreata da backup", f"{name}, senza tessere")
            self._changed()
            return self.db.get_person(person_id)
        async with self.lock:
            for card in cards:
                self._check_card_free(card["number"])
            user_token = await self._new_user_token(name)
            await self.controller.set_user(
                {"token": user_token, "Name": name, "Description": "Registrato da Axis Access Manager", "Attribute": []}
            )
            written: list[tuple[dict, str, dict]] = []
            try:
                for card in cards:
                    access = {k: card[k] for k in ("access_profile", "valid_from", "valid_to")}
                    token = await self.controller.set_credential(
                        self._credential(user_token, name, card["number"], access)
                    )
                    written.append((card, token, access))
                    if not card["enabled"]:
                        await self.controller.disable_credential(token)
            except VapixError:
                # Tutto o niente: sul controller non restano tessere o utenti a metà
                for _, token, _ in written:
                    try:
                        await self.controller.remove_credential(token)
                    except VapixError:
                        _LOGGER.warning("Tessera %s rimasta sul controller dopo un errore", token)
                await self._remove_axis_user({"axis_user_token": user_token})
                raise
            person_id = self._add_saved_person(saved, name, user_token, category_id)
            for card, token, access in written:
                self.db.add_card(person_id, card["number"], token, card["enabled"], access)
            numbers = ", ".join(card["number"] for card, _, _ in written)
            self.db.add_audit(user, "persona ricreata da backup", f"{name} ({numbers})")
            self._changed()
            return self.db.get_person(person_id)

    def _add_saved_person(self, saved: dict, name: str, user_token: str | None, category_id: int) -> int:
        """Persona del backup di nuovo nell'app, con lo stesso numero se è libero e la stessa data di creazione."""
        free = self.db.get_person(saved["id"]) is None
        person_id = self.db.add_person(
            name, user_token, person_id=saved["id"] if free else None, created_at=saved["created_at"]
        )
        if saved.get("company") or saved.get("notes"):
            self.db.update_person_details(person_id, saved.get("company") or "", saved.get("notes") or "")
        if category_id:
            self.db.set_person_category(person_id, category_id)
        return person_id

    async def _remove_axis_user(self, person: dict) -> bool:
        """False se l'utente del controller doveva essere tolto e non si è potuto."""
        token = person["axis_user_token"] or ""
        if not token or token.startswith("credenziale:"):
            return True
        try:
            await self.controller.remove_user(token)
        except VapixError as err:
            _LOGGER.warning("Utente %s non rimosso dal controller: %s", token, err)
            return False
        return True

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

    async def open_door(self, extended: bool, user: str, door: str = "", note: str = "") -> bool:
        """Sblocco temporaneo, solo a serratura bloccata: la serratura non regge l'alimentazione continua.

        False se la porta era già aperta a tempo: non si manda un altro comando (farebbe ripartire il tempo) e non è
        un rifiuto, chi ha chiesto di aprire trova la porta aperta."""
        door = door or self.settings.door_token
        if not door:
            raise ServiceError("nessuna porta configurata")
        # Due comandi ravvicinati (doppio tocco, Home Assistant e citofono insieme): il secondo aspetta il primo
        # e trova la porta già aperta, invece di passare anche lui il controllo e far ripartire il tempo
        async with self.door_lock(door):
            stored = self.monitor.door_modes.get(door) if self.monitor else None
            # Senza eventi in diretta (WebSocket non collegato, e broker giù o controller che non pubblica sul broker)
            # la modalità in memoria può essere vecchia anche quando dice "Bloccata": decide sempre il controller
            monitor = self.monitor
            live = bool(monitor and (monitor.ws_live or (monitor.connected and monitor.controller_online is True)))
            mode = stored if live else None
            if mode != "Locked":
                # In memoria può restare l'"Aperta a Tempo" di un'apertura il cui "Bloccata" via MQTT è andato perso
                # (broker riavviato durante l'apertura): prima di rifiutare decide il controller
                try:
                    read = await self.controller.door_mode(door)
                except VapixError as err:
                    _LOGGER.warning("Stato della porta non leggibile: %s", err)
                else:
                    current = self.monitor.door_modes.get(door) if self.monitor else stored
                    if live and current != stored:
                        # Arrivata in diretta (WebSocket o MQTT) mentre si leggeva: è più recente della lettura
                        mode = current
                    elif read:
                        if self.monitor and read != current:
                            self.monitor.set_door_mode(door, read)
                            self._changed()
                        mode = read
            if mode is None:
                raise ServiceError("stato della porta sconosciuto: riprova tra qualche secondo")
            if mode == "Accessed":
                # Al collaudo: un secondo comando da Home Assistant un secondo dopo il primo finiva nel Registro come
                # accesso negato, con la porta aperta
                _LOGGER.info("Porta %s già aperta a tempo: comando di %s non ripetuto", door, user)
                return False
            if mode != "Locked":
                # Import qui: ha_mqtt importa programming, che importa questo modulo
                from .ha_mqtt import DOOR_MODES

                label = DOOR_MODES.get(mode, mode)
                raise ServiceError(f"la porta non è bloccata ({label}): attendi che si richiuda")
            before = self.monitor.door_modes.get(door) if self.monitor else None
            guard, door_open = self.lock_guard, self._door_open(door)
            # Consenso alla rete di sicurezza già prima del comando: con la risposta persa il controller può avere
            # aperto lo stesso, con il tempo esteso se chiesto. Un consenso senza apertura scade da solo
            grant = guard.granted(door, extended, door_open) if guard is not None else None
            try:
                await self.controller.access_door(door, extended)
            except ControllerUnreachable:
                raise
            except VapixError:
                # Rifiutato dal controller: nessuna apertura
                if guard is not None:
                    guard.withdraw(door, grant)
                raise
            if guard is not None:
                # Il tempo della rete di sicurezza parte da qui, con il tempo esteso se chiesto
                guard.granted(door, extended, door_open)
            if self.monitor and self.monitor.door_modes.get(door) == before:
                # Evita un secondo comando finché la porta non si richiude (via MQTT, o dal controller al prossimo
                # comando o controllo); non copre un cambio arrivato via MQTT durante il comando
                self.monitor.set_door_mode(door, "Accessed")
        detail = ", ".join(x for x in ("tempo esteso" if extended else "", note) if x)
        self.log_event(ts=self.event_now(), kind="apertura_remota", detail=detail, source=user, door=door)
        try:
            self.db.add_audit(user, "porta aperta", detail)
        except sqlite3.Error as err:
            # La porta è aperta: con il disco pieno l'apertura risultava non eseguita
            _LOGGER.warning("Apertura della porta non scritta in Attività: %s", err)
        return True

    def recent_extended_openings(self, seconds: float) -> dict[str, float]:
        """Porte aperte dall'app con il tempo esteso negli ultimi seconds secondi, se è la loro ultima apertura a
        distanza: porta → secondi fa. Per la rete di sicurezza della serratura dopo un riavvio dell'add-on."""
        now = datetime.fromisoformat(self.event_now())
        since = (now - timedelta(seconds=seconds)).isoformat(timespec="seconds")
        found: dict[str, float] = {}
        seen: set[str] = set()
        for row in self.db.list_events(kind="apertura_remota", since=since):
            door = row["door"] or ""
            if door in seen:
                continue
            seen.add(door)
            try:
                age = (now - datetime.fromisoformat(row["ts"])).total_seconds()
            except (TypeError, ValueError):
                # Ora non leggibile (database ripristinato da una versione vecchia): l'avvio non si ferma per questo
                continue
            # Una riga nel futuro (orologio spostato) non si sa quando è: non conta
            if door and age >= 0 and (row["detail"] == "tempo esteso" or row["detail"].startswith("tempo esteso, ")):
                found[door] = age
        return found

    # Eventi

    def event_now(self) -> str:
        """Ora degli eventi creati dall'app (aperture remote, citofono) sull'orologio del controller, lo stesso dei
        passaggi con tessera: con Home Assistant indietro un'apertura remota non finisce nel registro prima del
        passaggio che l'ha preceduta. Solo con il controller sincronizzato via NTP, altrimenti l'ora di HA."""
        moment = datetime.now(tz=UTC)
        offset = self.clock_offset() if self.clock_offset else None
        if offset and abs(offset) < MAX_CLOCK_CORRECTION:
            moment += timedelta(seconds=offset)
        return moment.isoformat(timespec="seconds")

    def record_event(self, event: Event, retained: bool = False) -> None:
        """Salva nel log gli eventi utili. Lo stato mandato dal WebSocket appena collegato (retained) è già stato
        registrato."""
        self._controller_event = True
        try:
            self._record_event(event, retained)
        finally:
            self._controller_event = False

    def _record_event(self, event: Event, retained: bool) -> None:
        if self.lock_guard is not None and not retained and not self._recovering:
            if event.topic.startswith("AccessControl/AccessGranted"):
                door = self._door_of(event)
                self.lock_guard.granted(door, door_open=self._door_open(door))
            elif event.topic.startswith(("Door/State/", "Device/IO/Port")):
                self.lock_guard.poke()
        if event.topic.startswith("Door/State/DoorMode/"):
            # Anche lo stato mandato dal WebSocket appena collegato aggiorna Home Assistant
            self._changed()
            return
        if event.topic.startswith(STATE_TOPICS) and not self._recovering:
            # Sensori di Home Assistant: allarmi della porta, coperchio, avvio e ingressi del controller
            self._changed()
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
            ref = event_ref("lettura", door, number)
            if card is None:
                if self.log_event(
                    ts=ts, kind="sconosciuta", card=number, source="lettore", raw=data, door=door, ref=ref
                ):
                    self.db.note_unknown_card(number, ts)
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
                    ref=ref,
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
            pushed = self._input_source(str(data.get("AccessPointToken") or "")) if card is None else None
            # Stesso esito, stesso punto di accesso, stesso secondo: è lo stesso passaggio
            key = (granted, data.get("AccessPointToken") or door, ts)
            if key in self._recent_decisions or (key in self._unsaved_decisions and not self._recovering):
                return
            self._recent_decisions.append(key)
            kind = "concesso" if granted else "negato"
            if pushed:
                # Pulsante di uscita o relè del citofono su un ingresso del controller: un'apertura senza tessera,
                # o una pressione che il controller non fa aprire. Nel registro con il nome dell'ingresso: il
                # pulsante come Uscita, il relè come apertura a distanza dal citofono
                logged = kind if not granted else "uscita" if pushed[2] == "rex" else "apertura_remota"
                detail = pushed[1] if granted else f"{pushed[1]} · {DENIED_REASONS.get(reason, reason) or 'negato'}"
                ref = event_ref(kind, door)
                if not self._repeated(logged, pushed[0], door, ts, detail, ref):
                    row = self.log_event(
                        ts=ts, kind=logged, source=pushed[0], detail=detail, raw=data, door=door, ref=ref
                    )
                    self._unsaved_decision(key, row)
                return
            row = self.log_event(
                ts=ts,
                kind=kind,
                card=card["number"] if card else number,
                person_id=card["person_id"] if card else None,
                person_name=card["person_name"] if card else self._holder_name(data),
                detail="" if granted else DENIED_REASONS.get(reason, reason),
                source="controller",
                raw=data,
                door=door,
                ref=event_ref(kind, door),
            )
            self._unsaved_decision(key, row)
        elif topic.startswith("Door/State/DoorAlarm/") and data.get("State") not in (None, "Normal"):
            state = str(data["State"])
            detail = DOOR_ALARMS.get(state, state)
            self.log_event(
                ts=ts,
                kind="allarme",
                detail=detail,
                source="porta",
                raw=data,
                door=door,
                ref=event_ref("allarme", door, detail=detail),
            )
        elif topic.startswith("Door/State/DoorTamper/") and data.get("State") == "TamperDetected":
            detail = "manomissione porta"
            self.log_event(
                ts=ts,
                kind="manomissione",
                detail=detail,
                source="porta",
                raw=data,
                door=door,
                ref=event_ref("manomissione", door, detail=detail),
            )
        elif (topic.startswith("Device/Casing/Open") and str(data.get("Open")) == "1") or topic.startswith(
            "Device/Tampering/TiltDetected"
        ):
            # Coperchio aperto o controller inclinato (staccato dal muro?): non riguarda una porta
            detail = CASING_OPEN if topic.startswith("Device/Casing") else TILTED
            source = self.controller_name()
            ref = event_ref("manomissione", "", detail=detail)
            if not self._repeated("manomissione", source, "", ts, detail, ref):
                self.log_event(ts=ts, kind="manomissione", detail=detail, source=source, ref=ref)

    def _unsaved_decision(self, key: tuple, row: dict | None) -> None:
        """Esito arrivato in diretta e non salvato: il recupero non lo deve saltare come già registrato."""
        if row is not None and row["id"] is None and key in self._recent_decisions:
            self._recent_decisions.remove(key)
            self._unsaved_decisions.append(key)

    def _repeated(self, kind: str, source: str, door: str, ts: str, detail: str, ref: str) -> bool:
        """Stesso evento poco prima (rimbalzi, coperchio aperto e chiuso di continuo): non fa una riga nuova, segna
        quella già nel registro. Vale anche per gli eventi recuperati dal registro del controller, che così non
        rifanno le righe accorpate. Lo stesso evento arrivato via MQTT e dal registro del controller non è una
        ripetizione: al banco il recupero all'avvio aveva segnato "più pressioni" su una pressione sola."""
        if self._known(kind, ts, ref):
            return True
        row = self.db.recent_event(kind, source, door, detail, ts, REPEAT_WINDOWS[kind])
        if row is None:
            return False
        mark = f" · {REPEATED[kind]}"
        if not row["detail"].endswith(mark):
            self.db.set_event_detail(row["id"], row["detail"] + mark)
            self._changed()
        return True

    def _holder_name(self, data: dict) -> str:
        """Chi ha usato una tessera che l'app non ha più: l'A1210 manda il token dell'utente, non il nome.
        Se la persona c'è ancora si usa il suo nome, altrimenti il nome ricavato dal token."""
        holder = str(data.get("CredentialHolderName") or "")
        person = self.db.person_by_user_token(holder) if holder else None
        if person:
            return person["name"]
        # Il nome com'era, accenti compresi, da un suo accesso già nel registro
        known = self.db.holder_name(holder) if holder else ""
        if known:
            return f"{known} (tolta dall'app)"
        match = USER_TOKEN.fullmatch(holder)
        if match:
            return f"{match[1].replace('_', ' ').title()} (tolta dall'app)"
        return holder

    async def recover_events(self, since: datetime) -> int:
        """Eventi persi (MQTT o add-on fermi) letti dal registro interno del controller; restituisce quanti."""
        start = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not self.access_point_doors:
            # All'avvio il recupero viene prima della sincronizzazione: senza la mappa dei punti di accesso gli esiti
            # delle altre porte finivano sulla principale (e due volte, con l'impronta diversa da quella live)
            await self.load_access_points()
        self._recovered = 0
        self.recover_newest = None
        self.recover_more = False
        row_id, read = None, 0
        while True:
            entries, more = await self.controller.fetch_events(start, RECOVER_PAGE, row_id)
            read += len(entries)
            before = self._recovered
            # Esiti già visti prima della pagina: se la pagina non si salva i suoi passaggi non sono «già registrati».
            # Prima, con il disco ancora pieno, il recupero dopo li saltava e si perdevano per sempre
            decisions = list(self._recent_decisions)
            self._recovering = True
            try:
                # Una pagina, un commit
                with self.db.batch():
                    for entry in entries:
                        event = event_from_log(entry)
                        if event and (self.recover_newest is None or event.timestamp > self.recover_newest):
                            self.recover_newest = event.timestamp
                        if event and event.topic.startswith(RECOVER_TOPICS):
                            self.record_event(event)
            except sqlite3.Error:
                self._recent_decisions = deque(decisions, maxlen=self._recent_decisions.maxlen)
                raise
            finally:
                self._recovering = False
            if self._recovered > before:
                self.writable_again()
                self._changed()
            if not more or not entries:
                break
            if read >= RECOVER_MAX:
                # L'A1210 tiene fino a 250.000 eventi: il resto al prossimo giro, dall'ultimo letto (recover_newest)
                _LOGGER.warning("Recupero fermato a %s eventi letti dal registro del controller: si continua", read)
                self.recover_more = True
                break
            row_id = entries[-1].get("rowid")
        return self._recovered

    def controller_name(self) -> str:
        controllers = self.db.list_controllers()
        return (controllers[0]["name"] if controllers else "") or "Controller"

    def _input_source(self, access_point: str) -> tuple[str, str, str] | None:
        """Sorgente, dettaglio e ruolo per il registro dell'ingresso del controller che usa questo punto di accesso:
        il nome scelto nelle impostazioni ("Pulsante Portineria"), ruolo e morsetto ("Pulsante Uscita · AUX 1")."""
        if not access_point:
            return None
        for row in self.db.all_controller_inputs():
            if row.get("access_point_token") != access_point:
                continue
            if row["role"] not in INPUT_SOURCES:
                return None
            return row["name"] or INPUT_SOURCES[row["role"]], input_detail(row["io"], row["role"]), row["role"]
        return None

    def _door_of(self, event: Event) -> str:
        """Porta dell'evento: dal topic, dai dati, dal punto di accesso o dal lettore; se no la porta principale."""
        token = door_of_topic(event.topic) or str(event.data.get("DoorToken") or "")
        if not token and event.data.get("AccessPointToken"):
            token = self.access_point_doors.get(str(event.data["AccessPointToken"]), "")
        if not token and event.data.get("IdPointToken"):
            # Le letture delle tessere portano solo il lettore
            token = self.idpoint_doors.get(str(event.data["IdPointToken"]), "")
        return token or self.settings.door_token

    def record_intercom(self, entity_id: str, name: str) -> dict:
        """Chiamata dal citofono (entità di Home Assistant)."""
        return self.log_event(ts=self.event_now(), kind="citofono", detail=name, source=entity_id)
