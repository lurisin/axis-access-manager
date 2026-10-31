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
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from .events import EventMonitor, event_from_ws
from .vapix import VapixError, _network_reason, cert_fingerprint

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
# Un collegamento caduto prima di questi secondi non conta come ripreso: attese sempre più lunghe, stesso avviso nel
# log una volta sola e il guasto conta da quando era caduto la prima volta (controller che chiude appena collegato)
STABLE_AFTER = 60
# Secondi senza WebSocket, con il controller che risponde, prima che diventi un guasto (notifiche e interfaccia): un
# tentativo fallito e riuscito al successivo non lo è
FAULT_AFTER = 60


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
        fingerprint: Callable[[], str] | None = None,
    ) -> None:
        self.monitor = monitor
        self.client = client
        # Il controller risponde all'app (controllo periodico): solo allora si prova ad aprire il WebSocket, e il
        # WebSocket aperto si chiude quando non risponde più o è cambiato (scritture sospese)
        self.responding = responding
        self.connect = connect
        # Impronta del certificato del controller memorizzato ("" se non ancora riconosciuto): un controller diverso
        # all'indirizzo non manda eventi nemmeno prima che il controllo periodico se ne accorga
        self.fingerprint = fingerprint
        # Il controllo periodico lo segnala quando il controller smette o torna a rispondere, o le scritture si
        # sospendono: si riprova subito, o si chiude il collegamento aperto
        self.wake = asyncio.Event()
        self.connected = False
        # Da quando è collegato (ISO, UTC)
        self.connected_since = ""
        # Da quando non riesce a collegarsi, con il controller che risponde: primo tentativo fallito o collegamento
        # perso. Vuoto se non ancora provato o se il controller non risponde; mentre è collegato non conta
        self.down_since = ""
        self.last_error = ""
        self.events = 0
        self.last_event_at = ""
        self._attempt = 0
        self._up_at = 0.0
        self._logged_error = ""
        self._announced = False
        # Perché si sta chiudendo il collegamento aperto (vedi _watch): gli eventi che arrivano ancora non contano
        self._closing = ""

    async def run(self) -> None:
        try:
            while True:
                try:
                    responding = self.responding()
                except Exception:  # noqa: BLE001 - come gli altri cicli: si riprova al giro dopo
                    _LOGGER.exception("WebSocket degli eventi: stato del controller non letto")
                    responding = None
                if responding is not True:
                    # Lo dice già il controllo periodico; un WebSocket verso un controller diverso non si apre. Al
                    # ritorno si riparte dalle attese brevi, e da quando non si collega conta da quel momento
                    self.down_since = ""
                    self._attempt = 0
                    await self._wait(IDLE_WAIT)
                    continue
                try:
                    await self._session()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - si risulta scollegati e si riprova
                    self._lost(err)
                delay = RETRY_DELAYS[min(self._attempt, len(RETRY_DELAYS) - 1)]
                self._attempt += 1
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
        self._closing = ""
        async with self.connect(
            self.client.ws_url(token),
            ssl=self.client.ws_ssl(),
            open_timeout=OPEN_TIMEOUT,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            try:
                self._check_certificate(ws)
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
                            _LOGGER.debug("Filtri del WebSocket rifiutati: %s", error)
                            raise WebSocketError(
                                f"il controller ha rifiutato i filtri degli eventi ({error.get('code')})"
                            )
                        configured = True
                        initial_until = loop.time() + INITIAL_STATE_SECONDS
                    else:
                        self._notify(message, initial=True)
                # Sospeso o non più raggiungibile mentre si collegava: niente eventi da un controller non confermato
                if self.responding() is not True:
                    raise WebSocketError("il controller non risponde più o è cambiato")
                self._connected()
                watcher = asyncio.create_task(self._watch(ws))
                try:
                    async for raw in ws:
                        if self._closing:
                            break
                        self._notify(_json(raw), initial=loop.time() < initial_until)
                finally:
                    watcher.cancel()
                raise WebSocketError(self._closing or "collegamento chiuso dal controller")
            except asyncio.CancelledError:
                # Fermato (nuova configurazione, add-on che si ferma): senza aspettare la chiusura ordinata, che con
                # un controller che non risponde più dura close_timeout secondi
                _abort(ws)
                raise
            except ConnectionClosed:
                if self._closing:
                    raise WebSocketError(self._closing) from None
                raise

    async def _watch(self, ws) -> None:
        """Mentre è collegato: quando il controllo periodico sveglia e il controller non risponde più o le scritture
        sono sospese (controller cambiato), il collegamento si chiude subito. Un solo controllo fallito non basta."""
        while True:
            await self.wake.wait()
            self.wake.clear()
            try:
                responding = self.responding()
            except Exception:  # noqa: BLE001 - si guarda di nuovo al prossimo risveglio
                _LOGGER.debug("Stato del controller non letto", exc_info=True)
                continue
            if responding is False:
                self._closing = "il controller non risponde più o è cambiato"
                _abort(ws)
                return

    def _check_certificate(self, ws) -> None:
        expected = self.fingerprint() if self.fingerprint else ""
        if not expected:
            return
        transport = getattr(ws, "transport", None)
        ssl_object = transport.get_extra_info("ssl_object") if transport else None
        der = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        if der and cert_fingerprint(der) != expected:
            raise WebSocketError("il certificato del controller non è quello memorizzato")

    def _notify(self, message: dict, initial: bool) -> None:
        if message.get("method") != "events:notify" or self._closing:
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
        if self.down_since:
            # Di nuovo collegato dopo un'interruzione, con il controller che rispondeva: stato letto per intero ed
            # eventi del tempo in mezzo dal registro del controller, anche se nel frattempo MQTT sembrava portarli
            self.monitor.outages += 1
        self.connected = True
        self.connected_since = _now()
        self._up_at = time.monotonic()
        if not self._announced:
            self._announced = True
            # Dopo «in diretta» nel log, la prossima caduta si scrive di nuovo anche se ha lo stesso motivo
            self._logged_error = ""
            _LOGGER.info("Eventi del controller in diretta con il WebSocket")
        else:
            _LOGGER.debug("WebSocket degli eventi di nuovo collegato")
        self._set_live(True)

    def _lost(self, err: BaseException) -> None:
        text = error_text(err)
        if self.connected:
            # Eventi interrotti: lo stato si rilegge per intero e gli eventi persi si recuperano dal registro
            self.monitor.outages += 1
            if time.monotonic() - self._up_at >= STABLE_AFTER:
                # Era collegato da un po': interruzione nuova, attese di nuovo brevi e motivo di nuovo nel log
                self.down_since = ""
                self._attempt = 0
                self._logged_error = ""
                self._announced = False
        if not self.down_since:
            self.down_since = _now()
        self.connected = False
        self.connected_since = ""
        self.last_error = text
        if text != self._logged_error:
            # Una volta per motivo: con il WebSocket che non si apre il log non si riempie a ogni tentativo
            self._logged_error = text
            # Il dettaglio può contenere l'indirizzo con il gettone di accesso: nel log solo il tipo di errore
            _LOGGER.warning("WebSocket degli eventi non collegato: %s (%s)", text, type(err).__name__)
        _LOGGER.debug("WebSocket degli eventi non collegato: %s", _redact(repr(err)))
        self._set_live(False)

    def _set_live(self, live: bool) -> None:
        if not live:
            self.connected = False
            self.connected_since = ""
        if self.monitor.ws_live == live:
            return
        self.monitor.ws_live = live
        if self.monitor.on_status:
            try:
                self.monitor.on_status()
            except Exception:  # noqa: BLE001 - l'aggiornamento dell'interfaccia non ferma gli eventi
                _LOGGER.exception("Aggiornamento dello stato degli eventi non riuscito")

    def failing(self) -> bool:
        """Non si collega da almeno FAULT_AFTER secondi con il controller che risponde: per l'interfaccia."""
        if self.connected or not self.down_since:
            return False
        try:
            since = datetime.fromisoformat(self.down_since)
        except ValueError:
            return False
        return datetime.now(tz=UTC) - since >= timedelta(seconds=FAULT_AFTER)

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "since": self.connected_since or None,
            "down_since": None if self.connected else self.down_since or None,
            "failing": self.failing(),
            "error": "" if self.connected else self.last_error,
            "events": self.events,
            "last_event_at": self.last_event_at or None,
        }


def _redact(text: str) -> str:
    """Gettone di accesso al WebSocket tolto da un testo per il log."""
    return re.sub(r"wssession=[^&'\"\s]+", "wssession=***", text)


def _abort(ws) -> None:
    """Chiude il collegamento senza aspettare il controller."""
    transport = getattr(ws, "transport", None)
    if transport is not None:
        transport.abort()


def _json(raw) -> dict:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return message if isinstance(message, dict) else {}
