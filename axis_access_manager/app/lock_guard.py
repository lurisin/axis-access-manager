"""Rete di sicurezza della serratura: alimentata solo per il tempo di apertura, mai di più.

Senza contatto porta sul controller la serratura si riblocca sempre a fine tempo. Con il contatto porta (DoorMonitor)
il controller tiene la porta Aperta a Tempo finché non si richiude, e in loco il 3/11 un contatto che segnava sempre
"aperta" ha tenuto la serratura alimentata 5 minuti.

Qui l'app guarda le porte Aperte a Tempo e le blocca (tdc:LockDoor, mai uno sblocco):
- solo con cut_on_open (per ora spento, vedi LockGuard): quando il contatto porta vede la porta aprirsi durante
  l'apertura a tempo (prima chiusa, poi aperta): la serratura non serve più. Una porta già aperta al consenso
  (materiale che entra, porta che si sta chiudendo) non si blocca per questo: chi ha passato la tessera deve poterla
  spingere, e la serratura si riblocca a fine tempo;
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
from collections.abc import Awaitable, Callable
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
# Dopo l'allarme "riblocco non riuscito" si riprova più piano, finché la porta resta Aperta a Tempo
SLOW_RETRY = 30.0
# Consenso (tessera, pulsante, app) visto prima della modalità Aperta a Tempo: vale per questi secondi
GRANT_WINDOW = 5.0
# Senza eventi in diretta i consensi (tessere, pulsanti) si leggono dal registro del controller. Un consenso più
# vecchio di LOG_GRANT_MAX non fa partire un'apertura appena vista (può venire da altro); uno più nuovo di LOG_SAME
# rispetto all'inizio dell'apertura è un consenso nuovo; uno nel futuro oltre LOG_SKEW (orologio del controller
# avanti) non si usa
LOG_GRANT_MAX = 15.0
LOG_SAME = 1.0
LOG_SKEW = 2.0
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
    # Bloccata dall'app: si aspetta che la porta esca da Aperta a Tempo
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


def _no_contact(door: str) -> bool | None:
    return None


class LockGuard:
    def __init__(self, clock: Callable[[], float] = time.monotonic, cut_on_open: bool = False) -> None:
        self.clock = clock
        # Blocco quando il contatto vede la porta aprirsi: spento finché non è provato in loco che tdc:LockDoor
        # interrompe l'apertura a tempo senza effetti sulle aperture dopo. Resta la rete di sicurezza sul tempo
        self.cut_on_open = cut_on_open
        self.wake = asyncio.Event()
        self.accesses: dict[str, _Access] = {}
        self.grants: dict[str, _Grant] = {}
        # Tempi di apertura per porta in secondi: (normale, esteso); None se non letti
        self.times: dict[str, tuple[float | None, float | None]] = {}
        self.times_at: float | None = None
        # Cresce a ogni invalidate_times: una lettura partita prima non vale
        self._times_gen = 0
        # Ultimo consenso del registro del controller già usato, per porta (senza eventi in diretta)
        self.log_grants: dict[str, str] = {}

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
        self._times_gen += 1

    async def _read_times(self, controller) -> None:
        now = self.clock()
        if self.times_at is not None and now - self.times_at < TIMES_MAX_AGE:
            return
        gen = self._times_gen
        try:
            doors = await controller.list_doors()
        except VapixError as err:
            _LOGGER.debug("Tempi delle porte non letti: %s", err)
            return
        if gen != self._times_gen:
            # Tempi cambiati mentre si leggevano: si rileggono al prossimo giro
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

    def _track(
        self, door: str, contact_open: bool | None, live: bool, now: float, log_alarm: Callable[[str, str], None]
    ) -> _Access:
        """Apertura a tempo in corso, nuova o fatta ripartire da un altro consenso."""
        access = self.accesses.get(door)
        grant = self.grants.get(door)
        if grant is not None and now - grant.at > GRANT_WINDOW:
            grant = None
        if access is None or (grant is not None and grant.at > access.started):
            self.grants.pop(door, None)
            if access is not None:
                # Quella di prima è finita: un blocco per il tempo scaduto resta nel registro
                self._drop(door, log_alarm)
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
        last_grant: Callable[[str], Awaitable[tuple[str, float] | None]] | None = None,
    ) -> float:
        """Un giro sulle porte Aperte a Tempo. Restituisce tra quanti secondi serve il prossimo.

        contact_open(porta): il contatto porta segna aperta (True), chiusa (False), non si sa (None).
        log_alarm(porta, dettaglio): allarme nel registro. changed(): stato della porta aggiornato.
        live: eventi in diretta (WebSocket o MQTT); senza, lo stato in memoria può essere vecchio di 10 s e i consensi
        di tessere e pulsanti non arrivano: last_grant(porta) dà l'ultimo dal registro del controller, come
        (impronta, secondi fa)."""
        from_log = last_grant if not live else None
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
        # Senza il blocco all'apertura il contatto non serve
        read_contact = contact_open if self.cut_on_open else _no_contact
        for door in accessed:
            if not live and not await self._still_accessed(door, monitor, controller, log_alarm, changed):
                continue
            if from_log is not None and door not in self.accesses and not self._new_grant(door, None):
                # Apertura vista dalla lettura ogni 10 s: parte dal consenso nel registro, se è recente
                await self._grant_from_log(door, from_log, log_alarm)
            access = self._track(door, read_contact(door), live, self.clock(), log_alarm)
            reason = self._reason(door, access, self.clock())
            if reason == "tempo" and from_log is not None and await self._grant_from_log(door, from_log, log_alarm):
                # Una tessera o un pulsante ha fatto ripartire il tempo: si rifà il conto
                access = self.accesses[door]
                reason = self._reason(door, access, self.clock())
            if reason:
                async with door_lock(door):
                    # Mentre si aspettava (apertura dall'app in corso) può essere arrivato un consenso nuovo
                    access = self._track(door, read_contact(door), live, self.clock(), log_alarm)
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

    def _new_grant(self, door: str, access: _Access | None) -> bool:
        """Consenso visto dall'app dopo l'inizio dell'apertura (con access None: negli ultimi GRANT_WINDOW secondi)."""
        grant = self.grants.get(door)
        if access is None:
            return grant is not None and self.clock() - grant.at <= GRANT_WINDOW
        return grant is not None and grant.at > access.started

    async def _grant_from_log(
        self,
        door: str,
        last_grant: Callable[[str], Awaitable[tuple[str, float] | None]],
        log_alarm: Callable[[str, str], None],
    ) -> bool:
        """Senza eventi in diretta: un consenso nel registro del controller più nuovo dell'apertura che l'app conosce
        la fa ripartire (o partire, se appena vista). Ogni consenso del registro si usa una volta sola: con l'orologio
        del controller avanti un consenso vecchio non fa ripartire il tempo a ogni giro.
        True se l'apertura è cambiata."""
        try:
            found = await last_grant(door)
        except Exception:  # noqa: BLE001 - senza registro vale l'apertura che l'app conosce
            _LOGGER.debug("Registro della porta %s non letto", door, exc_info=True)
            return False
        if found is None:
            return False
        ident, age = found
        if ident == self.log_grants.get(door) or age < -LOG_SKEW:
            return False
        self.log_grants[door] = ident
        at = self.clock() - max(age, 0.0)
        access = self.accesses.get(door)
        if access is None:
            if age > LOG_GRANT_MAX:
                return False
        elif at <= access.started + LOG_SAME:
            return False
        else:
            self._drop(door, log_alarm)
        self.accesses[door] = _Access(started=at)
        return True

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
            # Un allarme solo; poi si riprova più piano, finché la porta resta Aperta a Tempo
            if not access.logged:
                access.logged = True
                _LOGGER.error("Porta %s ancora aperta a tempo dopo %s blocchi", door, access.attempts)
                log_alarm(door, FAILED)
            access.next_try = self.clock() + SLOW_RETRY
            return
        access.next_try = self.clock() + RETRY_AFTER
