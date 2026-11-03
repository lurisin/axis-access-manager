"""Rete di sicurezza della serratura: alimentata solo per il tempo di apertura, mai di più.

Senza contatto porta sul controller la serratura si riblocca sempre a fine tempo. Con il contatto porta (DoorMonitor)
il controller tiene la porta Aperta a Tempo finché non si richiude, e in loco il 3/11 un contatto che segnava sempre
"aperta" ha tenuto la serratura alimentata 5 minuti.

Qui l'app guarda le porte Aperte a Tempo e le blocca (tdc:LockDoor, mai uno sblocco):
- quando il contatto porta vede la porta aprirsi durante l'apertura a tempo (prima chiusa, poi aperta): la serratura
  non serve più. Una porta già aperta al consenso (materiale che entra, porta che si sta chiudendo) non si blocca
  per questo: chi ha passato la tessera deve poterla spingere, e la serratura si riblocca a fine tempo;
- se la porta resta Aperta a Tempo oltre il suo tempo più MARGIN secondi: è un guasto (contatto, configurazione,
  controller), e finisce nel registro come allarme "riblocco forzato".
Senza eventi in diretta (solo letture ogni 10 s) lo stato in memoria può essere vecchio: per le porte Aperte a Tempo
si rilegge il controller a ogni giro, e il contatto non si usa.
Prima di bloccare si rilegge la porta dal controller: un'apertura già finita, o una nuova, non si tocca.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from .programming import DOOR_TIMES, parse_duration
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

ACCESSED = "Accessed"
# Secondi oltre il tempo di apertura prima di bloccare: l'evento "Bloccata" del controller arriva in meno di uno
MARGIN = 3.0
# Porta che si apre durante l'apertura a tempo: si blocca dopo questi secondi (un contatto che rimbalza non conta)
OPEN_GRACE = 0.5
# Controllo periodico; gli eventi della porta lo anticipano (vedi poke), e il giro dice quando serve il prossimo
CHECK_EVERY = 1.0
MIN_WAIT = 0.05
# Blocco non riuscito o porta ancora aperta a tempo: nuovo tentativo dopo questi secondi, al più MAX_ATTEMPTS volte
RETRY_AFTER = 5.0
MAX_ATTEMPTS = 3
# Consenso (tessera, pulsante, app) visto prima della modalità Aperta a Tempo: vale per questi secondi
GRANT_WINDOW = 5.0
# Tempi delle porte letti dal controller: si rileggono dopo questi secondi
TIMES_MAX_AGE = 60.0
# Tempi massimi che l'app permette (la serratura non regge l'alimentazione continua): valgono anche per un tempo più
# lungo messo sul controller fuori dall'app, e per una porta i cui tempi non si sono letti
MAX_ACCESS = float(DOOR_TIMES["AccessTime"][3])
MAX_EXTENDED = float(DOOR_TIMES["ExtendedAccessTime"][3])
FORCED = "riblocco forzato"
FAILED = "riblocco non riuscito"


@dataclass
class _Access:
    """Un'apertura a tempo in corso, vista dall'app."""

    started: float
    extended: bool = False
    # Il contatto ha segnato la porta chiusa durante questa apertura (o al consenso): solo dopo un'apertura conta
    closed_seen: bool = False
    # Da quando il contatto segna la porta aperta dopo averla vista chiusa; None se chiusa, senza contatto o non si sa
    open_since: float | None = None
    attempts: int = 0
    next_try: float = 0.0
    # Bloccata dall'app, o rinuncia dopo MAX_ATTEMPTS: si aspetta che la porta esca da Aperta a Tempo
    done: bool = False
    # Il blocco è per il tempo scaduto (allarme), non per la porta aperta
    overtime: bool = False
    logged: bool = False


@dataclass
class _Grant:
    at: float
    extended: bool
    # Porta aperta secondo il contatto al momento del consenso (True, False, None se non si sa)
    door_open: bool | None


class LockGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.wake = asyncio.Event()
        self.accesses: dict[str, _Access] = {}
        self.grants: dict[str, _Grant] = {}
        # Tempi di apertura per porta in secondi: (normale, esteso); None se non letti
        self.times: dict[str, tuple[float | None, float | None]] = {}
        self.times_at: float | None = None

    def poke(self) -> None:
        """Un evento che può cambiare lo stato di una porta: controllo subito."""
        self.wake.set()

    def granted(self, door: str, extended: bool = False, door_open: bool | None = None) -> None:
        """Consenso alla porta (tessera, pulsante, apertura dall'app): il controller fa ripartire il tempo.
        door_open: il contatto porta in quel momento, prima che la porta si muova."""
        if door:
            self.grants[door] = _Grant(self.clock(), extended, door_open)
            self.poke()

    def invalidate_times(self) -> None:
        """Tempi delle porte cambiati dall'app: si rileggono alla prossima apertura."""
        self.times_at = None

    async def _read_times(self, controller) -> None:
        now = self.clock()
        if self.times_at is not None and now - self.times_at < TIMES_MAX_AGE:
            return
        try:
            doors = await controller.list_doors()
        except VapixError as err:
            _LOGGER.debug("Tempi delle porte non letti: %s", err)
            return
        self.times = {
            str(d.get("token")): (parse_duration(d.get("AccessTime")), parse_duration(d.get("ExtendedAccessTime")))
            for d in doors
            if d.get("token")
        }
        self.times_at = now

    def _limit(self, door: str, access: _Access) -> float:
        """Secondi di apertura a tempo previsti, entro i massimi dell'app."""
        normal, extended = self.times.get(door, (None, None))
        if access.extended:
            value, cap = extended or normal, MAX_EXTENDED
        else:
            value, cap = normal, MAX_ACCESS
        return min(float(value), cap) if value else cap

    def _track(self, door: str, contact_open: bool | None, live: bool, now: float) -> _Access:
        """Apertura a tempo in corso, nuova o fatta ripartire da un altro consenso."""
        access = self.accesses.get(door)
        grant = self.grants.get(door)
        if grant is not None and now - grant.at > GRANT_WINDOW:
            grant = None
        if access is None or (grant is not None and grant.at > access.started):
            self.grants.pop(door, None)
            started = grant.at if grant else now
            access = _Access(started=started, extended=bool(grant and grant.extended))
            if grant is not None and grant.door_open is False:
                access.closed_seen = True
            self.accesses[door] = access
        if not live:
            # Contatto letto ogni 10 s: non dice quando la porta si è aperta
            access.open_since = None
        elif contact_open is False:
            access.closed_seen = True
            access.open_since = None
        elif contact_open is True and access.closed_seen:
            if access.open_since is None:
                access.open_since = max(now, access.started)
        else:
            access.open_since = None
        return access

    def _deadline(self, door: str, access: _Access) -> float | None:
        """Quando bloccare questa apertura, se niente cambia; None se non c'è più niente da fare."""
        if access.done:
            return None
        due = access.started + self._limit(door, access) + MARGIN
        if access.open_since is not None:
            due = min(due, access.open_since + OPEN_GRACE)
        return max(due, access.next_try)

    def _reason(self, door: str, access: _Access, now: float) -> str:
        """Perché bloccare adesso: "aperta" (porta aperta), "tempo" (oltre il tempo), "" se non serve."""
        due = self._deadline(door, access)
        if due is None or now < due:
            return ""
        if access.open_since is not None and now >= access.open_since + OPEN_GRACE:
            return "aperta"
        return "tempo"

    def _drop(self, door: str, log_alarm: Callable[[str, str], None]) -> None:
        """Apertura finita. Un blocco per il tempo scaduto riuscito senza conferma (risposta persa, lettura
        fallita) finisce comunque nel registro."""
        access = self.accesses.pop(door, None)
        if access is not None and access.overtime and access.attempts and not access.logged:
            access.logged = True
            _LOGGER.warning("Porta %s rimasta aperta a tempo oltre il suo tempo: bloccata", door)
            log_alarm(door, FORCED)

    async def tick(
        self,
        monitor,
        controller,
        *,
        door_lock: Callable[[str], asyncio.Lock],
        contact_open: Callable[[str], bool | None],
        log_alarm: Callable[[str, str], None],
        changed: Callable[[], None],
        live: bool = True,
    ) -> float:
        """Un giro sulle porte Aperte a Tempo. Restituisce tra quanti secondi serve il prossimo.

        contact_open(porta): il contatto porta segna aperta (True), chiusa (False), non si sa (None).
        log_alarm(porta, dettaglio): allarme nel registro. changed(): stato della porta aggiornato.
        live: eventi in diretta (WebSocket o MQTT); senza, lo stato in memoria può essere vecchio di 10 s."""
        modes = dict(monitor.door_modes)
        for door in [d for d in self.accesses if modes.get(d) != ACCESSED]:
            self._drop(door, log_alarm)
        now = self.clock()
        for door in [d for d, g in self.grants.items() if now - g.at > GRANT_WINDOW]:
            del self.grants[door]
        accessed = [door for door, mode in modes.items() if mode == ACCESSED]
        if not accessed:
            return CHECK_EVERY
        await self._read_times(controller)
        wait = CHECK_EVERY
        for door in accessed:
            if not live and not await self._still_accessed(door, monitor, controller, log_alarm, changed):
                continue
            access = self._track(door, contact_open(door), live, self.clock())
            if self._reason(door, access, self.clock()):
                async with door_lock(door):
                    # Mentre si aspettava (apertura dall'app in corso) può essere arrivato un consenso nuovo
                    access = self._track(door, contact_open(door), live, self.clock())
                    reason = self._reason(door, access, self.clock())
                    if reason:
                        await self._lock(door, access, reason, monitor, controller, log_alarm, changed)
            access = self.accesses.get(door)
            due = self._deadline(door, access) if access else None
            if due is not None:
                wait = min(wait, max(MIN_WAIT, due - self.clock()))
        return wait

    async def _still_accessed(self, door, monitor, controller, log_alarm, changed) -> bool:
        """Senza eventi in diretta: la porta è ancora Aperta a Tempo sul controller? Se no l'apertura è finita, e la
        prossima Aperta a Tempo è un'altra."""
        try:
            mode = await controller.door_mode(door)
        except VapixError as err:
            _LOGGER.debug("Porta %s non letta: %s", door, err)
            return door in self.accesses
        if mode == ACCESSED:
            return True
        if mode and monitor.door_modes.get(door) == ACCESSED:
            monitor.set_door_mode(door, mode)
            changed()
        self._drop(door, log_alarm)
        return False

    def _new_grant(self, door: str, access: _Access) -> bool:
        grant = self.grants.get(door)
        return grant is not None and grant.at > access.started

    async def _lock(self, door, access: _Access, reason: str, monitor, controller, log_alarm, changed) -> None:
        # Prima si rilegge: un'apertura finita mentre l'evento "Bloccata" era in viaggio non si tocca
        try:
            mode = await controller.door_mode(door)
        except VapixError as err:
            _LOGGER.warning("Porta %s non letta prima del blocco: %s", door, err)
            access.next_try = self.clock() + RETRY_AFTER
            return
        if mode != ACCESSED:
            if mode and monitor.door_modes.get(door) == ACCESSED:
                monitor.set_door_mode(door, mode)
                changed()
            self._drop(door, log_alarm)
            return
        if self._new_grant(door, access):
            # Consenso arrivato durante la lettura: è un'apertura nuova, con il suo tempo
            return
        access.attempts += 1
        access.overtime = access.overtime or reason == "tempo"
        try:
            await controller.lock_door(door)
            mode = await controller.door_mode(door)
        except VapixError as err:
            _LOGGER.warning("Blocco della porta %s non riuscito: %s", door, err)
            mode = ACCESSED
        if mode != ACCESSED:
            access.done = True
            if mode and monitor.door_modes.get(door) == ACCESSED:
                monitor.set_door_mode(door, mode)
                changed()
            if access.overtime and not access.logged:
                access.logged = True
                _LOGGER.warning("Porta %s rimasta aperta a tempo oltre il suo tempo: bloccata", door)
                log_alarm(door, FORCED)
            elif not access.overtime:
                _LOGGER.info("Porta %s aperta durante l'apertura a tempo: serratura bloccata", door)
            return
        if access.attempts >= MAX_ATTEMPTS:
            access.done = True
            access.logged = True
            _LOGGER.error("Porta %s ancora aperta a tempo dopo %s blocchi", door, access.attempts)
            log_alarm(door, FAILED)
            return
        access.next_try = self.clock() + RETRY_AFTER
