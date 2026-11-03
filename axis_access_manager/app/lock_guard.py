"""Rete di sicurezza della serratura: alimentata solo per il tempo di apertura, mai di più.

Senza contatto porta sul controller la serratura si riblocca sempre a fine tempo. Con il contatto porta (DoorMonitor)
il controller la tiene alimentata finché la porta non si richiude, anche con "riblocca all'apertura" (prova in loco
del 3/11: 25 s con la porta tenuta aperta), e un contatto che segna sempre "aperta" l'ha tenuta alimentata 5 minuti.

Qui l'app guarda le porte Aperte a Tempo e le blocca (tdc:LockDoor, mai uno sblocco):
- appena il contatto porta segna la porta aperta: la serratura non serve più, e se la porta era già aperta al
  consenso al più non si alimenta;
- se la porta resta Aperta a Tempo oltre il suo tempo più MARGIN secondi: è un guasto (contatto, configurazione,
  firmware), e finisce nel registro come allarme "riblocco forzato".
Prima di bloccare si rilegge la porta dal controller: un'apertura già finita non si tocca.
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
# Porta aperta (contatto) durante l'apertura a tempo: si blocca dopo questi secondi. Un contatto che segna aperta per
# un istante mentre la porta si chiude (porta richiusa al consenso) non conta
OPEN_GRACE = 0.5
# Controllo periodico; gli eventi della porta lo anticipano (vedi poke)
CHECK_EVERY = 1.0
# Blocco non riuscito o porta ancora aperta a tempo: nuovo tentativo dopo questi secondi, al più MAX_ATTEMPTS volte
RETRY_AFTER = 5.0
MAX_ATTEMPTS = 3
# Consenso (tessera, pulsante, app) visto prima della modalità Aperta a Tempo: vale per questi secondi
GRANT_WINDOW = 5.0
# Tempi delle porte letti dal controller: si rileggono dopo questi secondi
TIMES_MAX_AGE = 60.0
# Tempo di apertura che non si sa (porte non lette): il massimo che l'app permette per l'apertura estesa
UNKNOWN_LIMIT = float(DOOR_TIMES["ExtendedAccessTime"][3])
FORCED = "riblocco forzato"
FAILED = "riblocco non riuscito"


@dataclass
class _Access:
    """Un'apertura a tempo in corso, vista dall'app."""

    started: float
    extended: bool = False
    # Da quando il contatto segna la porta aperta durante questa apertura; None se chiusa o senza contatto
    open_since: float | None = None
    attempts: int = 0
    next_try: float = 0.0
    # Bloccata dall'app o rinuncia dopo MAX_ATTEMPTS: si aspetta che la porta esca da Aperta a Tempo
    done: bool = False
    # Il blocco è per il tempo scaduto (allarme), non per la porta aperta
    overtime: bool = False


class LockGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.wake = asyncio.Event()
        self.accesses: dict[str, _Access] = {}
        # Consensi visti per porta: (quando, apertura estesa)
        self.grants: dict[str, tuple[float, bool]] = {}
        # Tempi di apertura per porta in secondi: (normale, esteso); None se non letti
        self.times: dict[str, tuple[float | None, float | None]] = {}
        self.times_at: float | None = None

    def poke(self) -> None:
        """Un evento che può cambiare lo stato di una porta: controllo subito."""
        self.wake.set()

    def granted(self, door: str, extended: bool = False) -> None:
        """Consenso alla porta (tessera, pulsante, apertura dall'app): il controller fa ripartire il tempo."""
        if door:
            self.grants[door] = (self.clock(), extended)
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
        normal, extended = self.times.get(door, (None, None))
        value = extended if access.extended else normal
        if access.extended and value is None:
            value = normal
        return float(value) if value else UNKNOWN_LIMIT

    def _track(self, door: str, contact_open: bool | None, now: float) -> _Access:
        """Apertura a tempo in corso, nuova o fatta ripartire da un altro consenso."""
        access = self.accesses.get(door)
        grant = self.grants.pop(door, None)
        if grant is not None and now - grant[0] > GRANT_WINDOW:
            grant = None
        if access is None or (grant is not None and grant[0] >= access.started):
            started = grant[0] if grant else now
            access = _Access(started=started, extended=bool(grant and grant[1]))
            self.accesses[door] = access
        if contact_open:
            if access.open_since is None:
                access.open_since = max(now, access.started)
        else:
            access.open_since = None
        return access

    def _due(self, door: str, access: _Access, now: float) -> str:
        """Perché bloccare adesso: "aperta" (contatto), "tempo" (oltre il tempo), "" se non serve."""
        if access.done or now < access.next_try:
            return ""
        if access.open_since is not None and now >= access.open_since + OPEN_GRACE:
            return "aperta"
        if now >= access.started + self._limit(door, access) + MARGIN:
            return "tempo"
        return ""

    async def tick(
        self,
        monitor,
        controller,
        *,
        door_lock: Callable[[str], asyncio.Lock],
        contact_open: Callable[[str], bool | None],
        log_alarm: Callable[[str, str], None],
        changed: Callable[[], None],
    ) -> None:
        """Un giro sulle porte Aperte a Tempo.

        contact_open(porta): il contatto porta segna aperta (True), chiusa (False), non si sa (None).
        log_alarm(porta, dettaglio): allarme nel registro. changed(): stato della porta aggiornato."""
        now = self.clock()
        modes = dict(monitor.door_modes)
        for door in [d for d in self.accesses if modes.get(d) != ACCESSED]:
            del self.accesses[door]
        for door in [d for d, (at, _) in self.grants.items() if now - at > GRANT_WINDOW]:
            del self.grants[door]
        accessed = [door for door, mode in modes.items() if mode == ACCESSED]
        if not accessed:
            return
        await self._read_times(controller)
        for door in accessed:
            access = self._track(door, contact_open(door), now)
            reason = self._due(door, access, self.clock())
            if reason:
                async with door_lock(door):
                    await self._lock(door, access, reason, monitor, controller, log_alarm, changed)

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
            self.accesses.pop(door, None)
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
            if access.overtime:
                _LOGGER.warning("Porta %s ancora aperta a tempo oltre il suo tempo: bloccata", door)
                log_alarm(door, FORCED)
            else:
                _LOGGER.info("Porta %s aperta durante l'apertura a tempo: serratura bloccata", door)
            return
        if access.attempts >= MAX_ATTEMPTS:
            access.done = True
            _LOGGER.error("Porta %s ancora aperta a tempo dopo %s blocchi", door, access.attempts)
            log_alarm(door, FAILED)
            return
        access.next_try = self.clock() + RETRY_AFTER
