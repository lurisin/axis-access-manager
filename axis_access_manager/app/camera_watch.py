"""Telecamere che non rispondono: l'app le ricontrolla da sola.

Fino alla 0.28.3 una foto non riuscita apriva il guasto «Telecamera Non Disponibile», che si chiudeva solo alla foto
buona successiva: finché nessuno suonava o apriva la telecamera risultava guasta, anche se era tornata. Il 13/11, dopo
il riavvio di Home Assistant per un aggiornamento, l'add-on è ripartito alle 20:02:58, la foto di un'apertura delle
20:03:04 non è riuscita (risposta 404: la telecamera non era ancora caricata), la telecamera è tornata alle 20:03:22
e il guasto è rimasto aperto.

- Tolleranza. Nei primi TOLERANCE_SECONDS dall'avvio dell'app o dal ritorno dell'API di Home Assistant, e dopo una foto
  non riuscita perché l'API non risponde, la foto si riprova ogni RETRY_SECONDS e il guasto si apre solo se alla fine la
  telecamera è ancora giù. Il 13/11 la telecamera è tornata 24 s dopo l'avvio e 18 s dopo la foto non riuscita: con 60 s
  il guasto non si apre, con più del doppio di margine per le telecamere che Home Assistant carica più lentamente.
- Ricontrollo. Con il guasto aperto la telecamera si riprova da sola dopo 5, 15, 30 e 60 s e poi ogni 5 minuti (ogni
  30 s finché l'API di Home Assistant non risponde, per accorgersi subito del suo ritorno). Quando risponde il guasto si
  chiude, come dopo una foto buona. Un solo ricontrollo per telecamera, fermato allo spegnimento; nel log una riga
  quando il guasto si apre e una quando si chiude, i tentativi solo in debug.
- Il ricontrollo è una foto di prova non salvata (HomeAssistant.probe_camera), non lo stato dell'entità: passa dalla
  stessa strada delle foto degli eventi, quindi il guasto si chiude solo quando una foto riesce davvero. Lo stato può
  dire «idle» con la foto che non arriva, e costa la lettura di tutti gli stati di Home Assistant.
- Foto dell'evento. Un nuovo tentativo si aggancia all'evento solo se è entro EVENT_PHOTO_SECONDS dalla richiesta, come
  la foto di sempre: più tardi mostrerebbe un ingresso vuoto e confonderebbe. Dopo serve solo a chiudere il guasto.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .ha import LOW_SPACE

_LOGGER = logging.getLogger(__name__)

TOLERANCE_SECONDS = 60.0
RETRY_SECONDS = 3.0
EVENT_PHOTO_SECONDS = 5.0
RECHECK_DELAYS = (5.0, 15.0, 30.0, 60.0)
RECHECK_EVERY = 300.0
# Con l'API di Home Assistant che non risponde l'attesa non cresce oltre questo: al ritorno la telecamera si riprova
# entro mezzo minuto, e da lì ogni RETRY_SECONDS per la tolleranza
API_DOWN_RECHECK = 30.0


@dataclass
class _Photo:
    """Foto di un evento da riprovare: nome del file, come agganciarla all'evento e quando è stata chiesta."""

    name: str
    attach: Callable[[str], None]
    requested_at: float


@dataclass
class _Down:
    """Telecamera con l'ultima foto non riuscita."""

    ts: str
    error: str
    # Fine della tolleranza per questa foto (oltre a quella dell'avvio e del ritorno dell'API)
    grace_until: float
    photo: _Photo | None = None


class CameraWatch:
    """Guasti delle telecamere (errors: telecamera → {"ts", "error"}, lo stesso dizionario delle notifiche) e
    ricontrollo delle telecamere giù. wanted(telecamera): la telecamera è ancora di una porta o del controller."""

    def __init__(
        self,
        ha,
        errors: dict[str, dict],
        wanted: Callable[[str], bool] = lambda camera: True,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.ha = ha
        self.errors = errors
        self._wanted = wanted
        self._clock = clock
        self._sleep = sleep
        self.started_at = clock()
        self._back_at = float("-inf")
        self._down: dict[str, _Down] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._wake: dict[str, asyncio.Event] = {}
        if hasattr(ha, "on_api_back"):
            ha.on_api_back = self.api_back

    def now(self) -> float:
        return self._clock()

    def tolerance_until(self) -> float:
        """Fine della tolleranza dell'avvio e del ritorno dell'API di Home Assistant."""
        return max(self.started_at, self._back_at) + TOLERANCE_SECONDS

    def api_back(self) -> None:
        """L'API di Home Assistant risponde di nuovo: le telecamere giù si riprovano subito, ogni RETRY_SECONDS."""
        self._back_at = self._clock()
        for event in self._wake.values():
            event.set()

    def ok(self, camera: str) -> None:
        """Foto di un evento riuscita: il guasto si chiude e il ricontrollo si ferma."""
        if camera in self.errors:
            _LOGGER.info("Telecamera %s di nuovo disponibile", camera)
        self.errors.pop(camera, None)
        self._down.pop(camera, None)
        task = self._tasks.pop(camera, None)
        self._wake.pop(camera, None)
        if task is not None:
            task.cancel()

    def failed(
        self,
        camera: str,
        error: str,
        ts: str,
        photo: tuple[str, Callable[[str], None], float] | None = None,
    ) -> None:
        """Foto di un evento non riuscita. ts: ora dell'evento; photo: nome del file, come agganciarla all'evento e
        quando è stata chiesta (vedi now), per riprovarla entro EVENT_PHOTO_SECONDS."""
        now = self._clock()
        down = self._down.get(camera)
        # Con l'API di Home Assistant giù (riavvio) anche la foto ha la sua tolleranza: il ritorno la allunga
        grace = now + TOLERANCE_SECONDS if getattr(self.ha, "api_up", None) is False else float("-inf")
        if down is None:
            down = self._down[camera] = _Down(ts=ts, error=error, grace_until=grace)
        else:
            down.error = error
            down.grace_until = max(down.grace_until, grace)
        if photo is not None:
            down.photo = _Photo(*photo)
        if camera in self.errors:
            # Guasto già aperto: resta l'ora da cui dura, con il motivo di adesso
            self.errors[camera] = {**self.errors[camera], "error": error}
        elif now >= self._grace_end(down):
            self._open(camera, down)
        self._start(camera)

    async def stop(self) -> None:
        """Allo spegnimento (o a una nuova configurazione dell'app): i ricontrolli si fermano."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        self._wake.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @property
    def watching(self) -> list[str]:
        return sorted(self._tasks)

    def _grace_end(self, down: _Down) -> float:
        return max(down.grace_until, self.tolerance_until())

    def _open(self, camera: str, down: _Down) -> None:
        self.errors[camera] = {"ts": down.ts, "error": down.error}
        _LOGGER.warning("Telecamera %s: %s; l'app la ricontrolla da sola", camera, down.error)

    def _start(self, camera: str) -> None:
        task = self._tasks.get(camera)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._wake[camera] = asyncio.Event()
        self._tasks[camera] = loop.create_task(self._watch(camera))

    async def _watch(self, camera: str) -> None:
        delays = list(RECHECK_DELAYS)
        logged = False
        try:
            while True:
                down = self._down.get(camera)
                if down is None:
                    return
                if self._clock() < self._grace_end(down):
                    wait = RETRY_SECONDS
                    # Dopo la tolleranza le attese ripartono da quella breve
                    delays = list(RECHECK_DELAYS)
                else:
                    wait = delays.pop(0) if delays else RECHECK_EVERY
                    if getattr(self.ha, "api_up", None) is False:
                        wait = min(wait, API_DOWN_RECHECK)
                await self._pause(camera, wait)
                down = self._down.get(camera)
                if down is None:
                    return
                if not self._wanted(camera):
                    # Telecamera tolta dalla porta: niente più guasto né ricontrollo
                    self.errors.pop(camera, None)
                    self._down.pop(camera, None)
                    return
                try:
                    error = await self._check(camera, down)
                except Exception:  # noqa: BLE001 - il ricontrollo continua: il guasto non resta senza fine
                    if not logged:
                        _LOGGER.exception("Ricontrollo della telecamera %s non riuscito", camera)
                    logged = True
                    error = "errore imprevisto, dettagli nel log dell'add-on"
                if (wake := self._wake.get(camera)) is not None:
                    # Il ritorno dell'API visto da questo stesso tentativo: l'attesa dopo ne tiene già conto
                    wake.clear()
                if not error:
                    if camera in self.errors:
                        _LOGGER.info("Telecamera %s di nuovo disponibile", camera)
                    self.errors.pop(camera, None)
                    self._down.pop(camera, None)
                    return
                down.error = error
                if camera in self.errors:
                    self.errors[camera] = {**self.errors[camera], "error": error}
                elif self._clock() >= self._grace_end(down):
                    self._open(camera, down)
        finally:
            if self._tasks.get(camera) is asyncio.current_task():
                self._tasks.pop(camera, None)
                self._wake.pop(camera, None)

    async def _pause(self, camera: str, seconds: float) -> None:
        """Attesa, interrotta dal ritorno dell'API di Home Assistant."""
        wake = self._wake.get(camera)
        if wake is None:
            await self._sleep(seconds)
            return
        if not wake.is_set():
            sleeper = asyncio.ensure_future(self._sleep(seconds))
            waiter = asyncio.ensure_future(wake.wait())
            try:
                await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for future in (sleeper, waiter):
                    future.cancel()
        wake.clear()

    async def _check(self, camera: str, down: _Down) -> str:
        """Nuovo tentativo: la foto dell'evento se è ancora in tempo (agganciata all'evento), se no una foto di prova
        non salvata. "" se la telecamera ha risposto."""
        photo = down.photo
        if photo is not None and self._clock() - photo.requested_at <= EVENT_PHOTO_SECONDS:
            filename, error = await self.ha.save_snapshot(camera, photo.name)
            if filename:
                down.photo = None
                photo.attach(filename)
                return ""
            if not error.startswith(LOW_SPACE):
                return error or "foto non riuscita"
        # Troppo tardi per l'evento (o disco quasi pieno): solo la prova
        down.photo = None
        return await self.ha.probe_camera(camera)
