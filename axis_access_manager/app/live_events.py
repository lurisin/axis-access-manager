"""Eventi del controller in diretta con il WebSocket VAPIX (/vapix/ws-data-stream).

Il WebSocket è la fonte principale degli eventi: passaggi, dinieghi, allarmi, manomissioni, ingressi e coperchio
arrivano in diretta senza MQTT. Gli eventi che il controller pubblica anche sul broker MQTT (facoltativo) si contano
una volta sola (EventMonitor.apply_event). Quando il WebSocket cade si riprova; il controllo periodico dello stato e
il recupero dal registro del controller coprono il tempo in mezzo.

Prove al banco del 25/10: accesso con il gettone di /axis-cgi/wssession.cgi; i filtri vogliono il suffisso //.
(senza, il controller accetta ma non manda niente); appena configurato manda lo stato attuale di porte e lettori.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from .events import EventMonitor, event_from_ws
from .vapix import VapixError, _network_reason

_LOGGER = logging.getLogger(__name__)

# Tutto quello che l'app usa: passaggi e dinieghi, porte, lettori (tessere lette), coperchio, ingressi e avvio
TOPIC_FILTERS = ("tns1:AccessControl//.", "tns1:Door//.", "tns1:IdPoint//.", "tns1:Device//.")
# Secondi per aprire il collegamento e per la risposta ai filtri
OPEN_TIMEOUT = 10
# Appena configurato il controller manda lo stato di porte, lettori, ingressi e coperchio con l'ora dell'ultimo
# cambio: come i messaggi conservati dal broker aggiornano lo stato, ma non fanno righe nel registro. Passaggi e
# tessere lette non sono stati: quelli valgono sempre
INITIAL_STATE_SECONDS = 3.0
ALWAYS_LIVE = ("AccessControl/", "IdPoint/Request/")
# Senza risposta al ping entro questi secondi il collegamento è perso (cavo staccato, controller spento)
PING_INTERVAL = 20
PING_TIMEOUT = 20
# Attese tra un tentativo e l'altro: subito di nuovo quando il controller torna a rispondere
RETRY_DELAYS = (5, 10, 30, 60)
# Controller che non risponde o sostituito: si riguarda dopo questi secondi, o prima se lo dice il controllo periodico
IDLE_WAIT = 30


class WebSocketError(Exception):
    """Il WebSocket non si apre o si chiude: il messaggio è per l'utente."""


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def error_text(err: BaseException) -> str:
    """Perché il WebSocket non è collegato, in parole: il dettaglio tecnico resta nel log."""
    if isinstance(err, WebSocketError | VapixError):
        return str(err)
    if isinstance(err, InvalidStatus):
        code = err.response.status_code
        if code == 401:
            return "il controller non ha accettato l'accesso al WebSocket (risposta 401)"
        return f"il controller ha rifiutato il WebSocket (risposta {code})"
    if isinstance(err, ConnectionClosed):
        return "collegamento chiuso dal controller" if err.rcvd else "collegamento perso"
    if isinstance(err, InvalidURI):
        return "indirizzo del controller non valido"
    if isinstance(err, InvalidHandshake):
        return "il controller non ha risposto come un WebSocket"
    if isinstance(err, TimeoutError):
        return "nessuna risposta in tempo"
    return f"collegamento non riuscito ({_network_reason(err)})"


class LiveEvents:
    """Tiene aperto il WebSocket degli eventi del controller principale e passa gli eventi al monitor."""

    def __init__(
        self,
        monitor: EventMonitor,
        client,
        responding: Callable[[], bool | None],
        connect=ws_connect,
    ) -> None:
        self.monitor = monitor
        self.client = client
        # Il controller risponde all'app (controllo periodico): solo allora si prova ad aprire il WebSocket
        self.responding = responding
        self.connect = connect
        # Il controllo periodico lo segnala quando il controller torna a rispondere: si riprova subito
        self.wake = asyncio.Event()
        self.connected = False
        # Da quando è collegato (ISO, UTC)
        self.connected_since = ""
        # Da quando non riesce a collegarsi, con il controller che risponde: primo tentativo fallito o collegamento
        # perso. Vuoto se collegato o non ancora provato
        self.down_since = ""
        self.last_error = ""
        self.events = 0
        self.last_event_at = ""
        self._logged_error = ""

    async def run(self) -> None:
        attempt = 0
        try:
            while True:
                if self.responding() is not True:
                    # Lo dice già il controllo periodico; un WebSocket verso un controller diverso non si apre. Da
                    # quando non si collega conta di nuovo da quando il controller risponde
                    self.down_since = ""
                    await self._wait(IDLE_WAIT)
                    continue
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - si risulta scollegati e si riprova
                    attempt = 0 if self.connected else attempt
                    self._lost(err)
                delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                attempt += 1
                await self._wait(delay)
        finally:
            self._set_live(False)

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.wake.wait(), seconds)
        except TimeoutError:
            pass
        self.wake.clear()

    async def _session(self) -> None:
        token = await self.client.ws_session()
        loop = asyncio.get_running_loop()
        async with self.connect(
            self.client.ws_url(token),
            ssl=self.client.ws_ssl(),
            open_timeout=OPEN_TIMEOUT,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            configure = {
                "apiVersion": "1.0",
                "context": "aam",
                "method": "events:configure",
                "params": {"eventFilterList": [{"topicFilter": f} for f in TOPIC_FILTERS]},
            }
            await ws.send(json.dumps(configure))
            initial_until = loop.time() + OPEN_TIMEOUT + INITIAL_STATE_SECONDS
            configured = False
            while not configured:
                raw = await asyncio.wait_for(ws.recv(), OPEN_TIMEOUT)
                message = _json(raw)
                if message.get("method") == "events:configure":
                    error = message.get("error")
                    if isinstance(error, dict):
                        _LOGGER.info("Filtri del WebSocket rifiutati: %s", error)
                        raise WebSocketError(f"il controller ha rifiutato i filtri degli eventi ({error.get('code')})")
                    configured = True
                    initial_until = loop.time() + INITIAL_STATE_SECONDS
                else:
                    self._notify(message, initial=True)
            self._connected()
            async for raw in ws:
                self._notify(_json(raw), initial=loop.time() < initial_until)
            raise WebSocketError("collegamento chiuso dal controller")

    def _notify(self, message: dict, initial: bool) -> None:
        if message.get("method") != "events:notify":
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        event = event_from_ws(params.get("notification"))
        if event is None:
            return
        self.events += 1
        self.last_event_at = _now()
        retained = initial and not event.topic.startswith(ALWAYS_LIVE)
        self.monitor.apply_event(event, retained)

    def _connected(self) -> None:
        self.connected = True
        self.connected_since = _now()
        self.down_since = ""
        self.last_error = ""
        self._logged_error = ""
        _LOGGER.info("Eventi del controller in diretta con il WebSocket")
        self._set_live(True)

    def _lost(self, err: BaseException) -> None:
        text = error_text(err)
        if self.connected:
            # Eventi interrotti: lo stato si rilegge per intero e gli eventi persi si recuperano dal registro
            self.monitor.outages += 1
            self.down_since = _now()
        elif not self.down_since:
            self.down_since = _now()
        self.connected = False
        self.connected_since = ""
        self.last_error = text
        if text != self._logged_error:
            # Una volta per motivo: con il WebSocket che non si apre il log non si riempie a ogni tentativo
            self._logged_error = text
            _LOGGER.warning("WebSocket degli eventi non collegato: %s (%r)", text, err)
        else:
            _LOGGER.debug("WebSocket degli eventi non collegato: %r", err)
        self._set_live(False)

    def _set_live(self, live: bool) -> None:
        if self.monitor.ws_live == live:
            return
        self.monitor.ws_live = live
        if not live:
            self.connected = False
            self.connected_since = ""
        if self.monitor.on_status:
            self.monitor.on_status()

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "since": self.connected_since or None,
            "down_since": self.down_since or None,
            "error": self.last_error,
            "events": self.events,
            "last_event_at": self.last_event_at or None,
        }


def _json(raw) -> dict:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return message if isinstance(message, dict) else {}
