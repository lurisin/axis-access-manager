"""Citofono Hikvision: chiamate nel log e "Apri" dal citofono inoltrato all'A1210.

Gli eventi arrivano dall'add-on Hikvision Doorbell, che li pubblica sullo stesso broker MQTT
(libreria ha-mqtt-discoverable, topic <prefisso>/<componente>/<dispositivo>/<entità>/state).

Il relè del citofono non va mai alla serratura. Può essere collegato a un ingresso dell'A1210 con il ruolo Relè
Citofono: il controller registra ogni pressione come apertura remota, ma apre solo se quell'ingresso ha il permesso di
aprire. All'Eliseo non ce l'ha, per sicurezza (un relè manomesso non deve aprire): la porta la apre l'app, che quando
il citofono segnala un'apertura manda all'A1210 un solo AccessDoor, mai uno sblocco permanente ("Apri" dal
Videocitofono Apre Questa Porta). Dove invece l'ingresso apre, l'app trova la porta già aperta e non manda altro.

Il 13/11 sera, dopo un riavvio di Home Assistant, il bridge ha consegnato in ritardo le pressioni dell'Apri fatte con
Home Assistant spento (20:02:42; messaggi alle 20:03:04 e 20:03:09) e la porta si è aperta 22 s dopo la richiesta,
con nessuno davanti. Per questo un relè ON non si inoltra nei primi secondi dopo un ricollegamento (dell'app al broker
o del bridge) senza una chiamata arrivata dopo, né se l'ora dell'evento negli attributi è vecchia. La 0.28.4 non lo
inoltrava nemmeno se il controller aveva appena registrato una pressione del Relè Citofono: all'Eliseo, dove
quell'ingresso non apre, così la porta restava chiusa. Tolto nella 0.28.5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .db import now_iso

_LOGGER = logging.getLogger(__name__)

# Un'apertura dal citofono genera più messaggi (comando da HA ed evento del citofono):
# entro questo intervallo si manda un solo AccessDoor
OPEN_DEBOUNCE_SECONDS = 5.0
# Gli attributi (chi ha aperto) arrivano subito prima dello stato ON
ATTRIBUTES_MAX_AGE_SECONDS = 5.0
# Una chiamata suona al massimo un minuto: un nuovo "ringing" dopo questo tempo è una chiamata nuova, anche se la fine
# della precedente non è arrivata
RING_MAX_SECONDS = 90.0
# Messaggi vecchi dopo un ricollegamento: un relè ON nei primi secondi dopo che l'app si ricollega al broker o che il
# bridge torna (disponibilità "online" pubblicata adesso, relè "OFF" ripubblicato da un bridge ripartito) non si
# inoltra, se non c'è una chiamata arrivata dopo il ricollegamento. Il 13/11 il bridge è tornato alle 20:02:57 e
# l'add-on alle 20:02:58, e le pressioni vecchie sono arrivate alle 20:03:04 e 20:03:09 (7 e 12 s dopo): 30 s le
# coprono con più del doppio di margine, per un bridge che si ricollega al citofono più lentamente. Un Apri dal vivo
# durante una chiamata passa sempre; uno senza chiamata in quei 30 s resta nel registro, e basta ripeterlo.
# Vale anche quando il controller torna a rispondere dopo un'interruzione (il 14/11 la rete della porta è caduta per
# due ore: al ritorno il videocitofono ha consegnato quattro pressioni vecchie, alle 19:23:54, 19:24:05, 19:24:12 e
# 19:24:20, e l'app le ha inoltrate tutte), e finché le pressioni vecchie continuano ad arrivare: ognuna fa ripartire
# i 30 s. Con il controller che non risponde, un Apri senza chiamata non si inoltra (non aprirebbe adesso, e la
# richiesta non deve restare in coda per dopo)
RECONNECT_SECONDS = 30.0
# Con l'ora dell'evento negli attributi del relè: un messaggio più vecchio di così non si inoltra (stesso caso, senza
# ricollegamento visto). Lo scarto dell'orologio del citofono si misura alle aperture durante una chiamata
STALE_SECONDS = 15.0
# Attributi del relè che possono portare l'ora dell'evento
EVENT_TIME_KEYS = ("time", "event_time", "eventTime", "date_time", "dateTime", "datetime", "timestamp")
# Senza messaggi dal bridge per questo tempo, un relè "OFF" già spento è il bridge ripartito: in ha-mqtt-discoverable
# (0.26.3) lo Switch pubblica solo i cambi, e un bridge nuovo comincia con off(). Una chiamata "idle" ripetuta invece
# non dice niente: Sensor.set_state ripubblica sempre (force_update), e il bridge rilegge callStatus a intervalli se
# ha call_state_poll
BRIDGE_SILENCE_SECONDS = 60.0
# Righe uguali di aperture non inoltrate (una raffica di messaggi vecchi): una sola nel registro entro questo tempo
SKIP_REPEAT_SECONDS = 10.0

# Valori del sensore "Call state": evento di chiamata o lettura periodica di callStatus
RINGING_STATES = {"ringing", "ring"}
ANSWERED_STATES = {"oncall"}
IDLE_STATES = {"idle", "dismissed"}

# Aperture con credenziali salvate sul citofono: non si inoltrano, l'unico elenco
# di chi può entrare è quello dell'A1210. Il nome è quello a metà frase nel dettaglio del registro
LOCAL_CREDENTIALS = {
    "PASSWORD": "PIN",
    "HIJACKING": "PIN antirapina",
    "CARD": "tessera",
    "BLUETOOTH": "Bluetooth",
    "QR_CODE": "codice QR",
    "FACE": "volto",
    "FINGERPRINT": "impronta",
}
# Origine dell'apertura nel registro ("App Hik-Connect (Videocitofono)")
UNLOCK_SOURCES = {
    "HOUSEHOLDER": "App o Monitor",
    # Al banco l'apertura dall'app Hik-Connect arriva così (sorgente "00"); l'account che apre non c'è
    "CENTER_PLATFORM": "App Hik-Connect",
    "PASSWORD": "PIN",
    "HIJACKING": "PIN Antirapina",
    "CARD": "Tessera",
    "BLUETOOTH": "Bluetooth",
    "QR_CODE": "Codice QR",
    "FACE": "Volto",
    "FINGERPRINT": "Impronta",
}
# Apertura dell'inquilino: control_source dice se dall'app o dal monitor interno
HOUSEHOLDER_SOURCES = {"app": "App Hik-Connect", "monitor": "Monitor Interno", "indoor": "Monitor Interno"}


def unlock_source(attributes: dict) -> str:
    """Come è stato aperto il citofono, come etichetta del registro."""
    unlock_type = str(attributes.get("unlock_type") or "HOUSEHOLDER")
    if unlock_type == "HOUSEHOLDER":
        control = str(attributes.get("control_source") or "").lower()
        for key, label in HOUSEHOLDER_SOURCES.items():
            if key in control:
                return label
    return UNLOCK_SOURCES.get(unlock_type, UNLOCK_SOURCES["HOUSEHOLDER"])


def topic_name(raw: str) -> str:
    """Come ha-mqtt-discoverable scrive nomi di dispositivo ed entità nei topic."""
    return re.sub(r"[^A-Za-z0-9_-]", "-", raw)


def event_time(attributes: dict) -> datetime | None:
    """Ora dell'evento negli attributi del relè, se c'è: testo ISO con il fuso o secondi (o millisecondi) dal 1970.
    Un'ora senza fuso non si sa di quale fuso è: non vale."""
    for key in EVENT_TIME_KEYS:
        value = attributes.get(key)
        if isinstance(value, bool) or value in (None, ""):
            continue
        try:
            if isinstance(value, int | float):
                moment = datetime.fromtimestamp(value / 1000 if value > 1e12 else value, tz=UTC)
            else:
                moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except (OverflowError, OSError, ValueError):
            continue
        if moment.tzinfo is not None:
            return moment
    return None


def _iso(moment: datetime) -> str:
    """Ora come nel registro: UTC, al secondo."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class IntercomSettings:
    name: str  # nome del citofono nelle opzioni dell'add-on Hikvision Doorbell
    open_door: bool = False
    relay: int = 1
    state_prefix: str = "hmd"


class IntercomBridge:
    def __init__(
        self,
        settings: IntercomSettings,
        log: Callable[..., object],
        open_door: Callable[[str, str], Awaitable[None]],
        clock: Callable[[], float] = time.monotonic,
        *,
        wall: Callable[[], datetime] | None = None,
        controller_down: Callable[[], bool] | None = None,
    ) -> None:
        """wall: ora adesso sull'orologio del controller, per l'ora dell'evento negli attributi del relè.
        controller_down: il controller della porta adesso non risponde (vedi health.ControllerHealth)."""
        self.settings = settings
        self._log = log
        self._open_door = open_door
        self._clock = clock
        self._wall = wall or (lambda: datetime.now(tz=UTC))
        device = topic_name(settings.name)
        base = f"{settings.state_prefix}"
        call = f"{base}/sensor/{device}/{topic_name('Call state')}"
        self.call_topic = f"{call}/state"
        relay = topic_name(f"Door {settings.relay} relay")
        self.relay_topic = f"{base}/switch/{device}/{relay}/state"
        self.relay_attributes_topic = f"{base}/switch/{device}/{relay}/attributes"
        # Disponibilità pubblicata da ha-mqtt-discoverable (manual_availability): un "online" non conservato è il
        # bridge che torna
        self.availability_topics = [f"{call}/availability", f"{base}/switch/{device}/{relay}/availability"]
        # Stesso citofono su due porte (relè 1 e 2): la chiamata è una e la segue il bridge della prima porta
        self.call: IntercomBridge | None = None
        self._call_state = "idle"
        self._ring_at: float | None = None
        self._answered = False
        self._attributes: dict = {}
        self._attributes_at = 0.0
        self._last_open = float("-inf")
        # Aperture con credenziali del videocitofono: tempo di attesa a parte, così non bloccano l'Apri dell'inquilino
        self._last_local = float("-inf")
        self._tasks: set[asyncio.Task] = set()
        self.last_message_at: str | None = None
        # Ultimo ricollegamento (dell'app al broker o del bridge) e cosa era, per i messaggi vecchi
        self._back_at = float("-inf")
        self._back_reason = ""
        # Ultimo "ringing" o "oncall" arrivato: una chiamata dal vivo, se è dopo il ricollegamento
        self._live_call_at = float("-inf")
        # Ultimo messaggio del bridge (None: nessuno da quando l'app ascolta) e ultimo stato del relè
        self._heard_at: float | None = None
        self._relay_state = ""
        # Scarto dell'orologio del citofono (secondi di ritardo dei messaggi dal vivo), misurato alle aperture durante
        # una chiamata; None finché non si sa
        self._skew: float | None = None
        # Ultima riga di apertura non inoltrata e quando: una raffica fa una riga sola
        self._skipped = ("", float("-inf"))
        # Ultima pressione non inoltrata perché vecchia: quelle che arrivano a raffica dopo sono della stessa coda
        self._last_stale = float("-inf")
        self._controller_down = controller_down

    @property
    def topics(self) -> list[str]:
        return [self.call_topic, self.relay_topic, self.relay_attributes_topic]

    @property
    def subscriptions(self) -> list[str]:
        """Topic da seguire: chiamata, relè e disponibilità del bridge."""
        return [*self.topics, *self.availability_topics]

    @property
    def call_state(self) -> str:
        return self.call.call_state if self.call else self._call_state

    def broker_connected(self) -> None:
        """L'app si è collegata (o ricollegata) al broker: i primi messaggi del citofono possono essere vecchi."""
        self._came_back("il ricollegamento dell'app")

    def controller_back(self) -> None:
        """Il controller della porta risponde di nuovo dopo un'interruzione: il videocitofono, spesso sulla stessa rete,
        può consegnare adesso le pressioni fatte mentre non c'era (14/11)."""
        self._came_back("il ritorno del controller")

    def handle(self, topic: str, payload: bytes, retained: bool = False) -> None:
        # Gli stati sono conservati dal broker: alla riconnessione non sono eventi nuovi. La fine chiamata arrivata
        # mentre l'app era scollegata allinea però lo stato, senza eventi: altrimenti la chiamata dopo si perde
        text = payload.decode(errors="replace").strip()
        if topic in self.availability_topics:
            self._on_availability(text.lower(), retained)
            return
        if retained:
            if topic == self.call_topic and text.lower() in IDLE_STATES:
                self._call_state, self._ring_at, self._answered = text.lower(), None, False
            return
        now = self._clock()
        quiet = self._heard_at is None or now - self._heard_at >= BRIDGE_SILENCE_SECONDS
        self._heard_at = now
        self.last_message_at = now_iso()
        if topic == self.call_topic:
            self._on_call_state(text)
        elif topic == self.relay_attributes_topic:
            try:
                attributes = json.loads(text)
            except ValueError:
                return
            if isinstance(attributes, dict):
                self._attributes = attributes
                self._attributes_at = self._clock()
        elif topic == self.relay_topic:
            state = text.upper()
            if quiet and state == "OFF" and self._relay_state != "ON":
                # Relè spento ripubblicato dopo un silenzio: il bridge è ripartito
                self._came_back("il ritorno del videocitofono")
            self._relay_state = state
            if state == "ON":
                self._on_unlock()

    def _on_availability(self, value: str, retained: bool) -> None:
        # Il bridge pubblica "online" solo quando parte e a ogni suo ricollegamento al broker (set_availability in
        # on_connect): è sempre un ritorno, anche dopo un "online" senza "offline" in mezzo. Il testamento "offline"
        # non arriva quando il bridge si chiude in modo ordinato (DISCONNECT) né quando cade il broker, che con la
        # persistenza tiene l'ultimo "online". Conservata dal broker invece è lo stato di prima, non un ritorno (al
        # ricollegamento dell'app vale già quello)
        if value == "online" and not retained:
            self._came_back("il ritorno del videocitofono")

    def _came_back(self, reason: str) -> None:
        self._back_at = self._clock()
        self._back_reason = reason

    def _reconnected(self) -> tuple[float, str]:
        """Ultimo ricollegamento: anche quelli visti dal bridge della chiamata, con lo stesso citofono su due porte."""
        own = (self._back_at, self._back_reason)
        other = (self.call._back_at, self.call._back_reason) if self.call else own
        return max(own, other, key=lambda item: item[0])

    def _event(self, detail: str, raw: dict | None = None) -> None:
        self._log(ts=now_iso(), kind="citofono", detail=detail, source=self.settings.name, raw=raw)

    def _on_call_state(self, value: str) -> None:
        state = value.lower()
        previous, self._call_state = self._call_state, state
        if state in RINGING_STATES or state in ANSWERED_STATES:
            # Chiamata dal vivo: un Apri che arriva dopo è un'apertura vera, anche subito dopo un ricollegamento
            self._live_call_at = self._clock()
        if state in RINGING_STATES and self._ring_at is not None and self._since_ring() > RING_MAX_SECONDS:
            # La fine della chiamata precedente non è mai arrivata: questa è una chiamata nuova
            self._ring_at = None
        elif state == previous:
            return
        if state in RINGING_STATES:
            if self._ring_at is None:
                self._ring_at = self._clock()
                self._answered = False
                self._event("ha suonato")
        elif state in ANSWERED_STATES:
            if self._ring_at is not None and not self._answered:
                self._answered = True
                self._event(f"risposta dopo {self._since_ring()} s")
        elif state in IDLE_STATES and self._ring_at is not None:
            if not self._answered:
                self._event("chiamata chiusa" if state == "dismissed" else "nessuna risposta")
            self._ring_at = None
            self._answered = False

    def _since_ring(self) -> int:
        return round(self._clock() - self._ring_at) if self._ring_at is not None else 0

    def _on_unlock(self) -> None:
        now = self._clock()
        attributes = self._attributes if now - self._attributes_at <= ATTRIBUTES_MAX_AGE_SECONDS else {}
        unlock_type = str(attributes.get("unlock_type") or "")
        how = unlock_source(attributes)
        if unlock_type in LOCAL_CREDENTIALS:
            if now - self._last_local < OPEN_DEBOUNCE_SECONDS:
                _LOGGER.debug("Apertura locale del citofono già registrata: secondo messaggio scartato (%s)", how)
                return
            self._last_local = now
            # Il visitatore entra da solo: se ha suonato, nessuno ha risposto
            self._event(
                f"apertura con {LOCAL_CREDENTIALS[unlock_type]} del videocitofono non inoltrata all'A1210", attributes
            )
            return
        if now - self._last_open < OPEN_DEBOUNCE_SECONDS:
            _LOGGER.debug("Apertura dal citofono già inoltrata: secondo messaggio scartato (%s)", how)
            return
        ring = self.call or self
        if not self.settings.open_door:
            self._last_open = now
            note = self._answer(ring)
            detail = ", ".join(x for x in (f"apertura da {how} (inoltro all'A1210 disattivato)", note) if x)
            self._event(detail, attributes)
            return
        back_at, back_reason = self._reconnected()
        # Chiamata dal vivo arrivata dopo il ricollegamento: chi apre ha risposto adesso
        live = ring._live_call_at >= back_at and now - ring._live_call_at <= RING_MAX_SECONDS
        moment = event_time(attributes)
        delay = (self._wall() - moment).total_seconds() if moment else None
        if live and delay is not None:
            self._skew = delay
        stale = ""
        if not live and self._controller_down is not None and self._controller_down():
            stale = "il controller non risponde, senza una chiamata in corso"
        elif not live and now - back_at < RECONNECT_SECONDS:
            stale = f"messaggio arrivato {round(now - back_at)} s dopo {back_reason}, senza una chiamata in corso"
        elif not live and now - self._last_stale < RECONNECT_SECONDS:
            stale = "altra pressione arrivata insieme a quelle vecchie, senza una chiamata in corso"
        elif not live and delay is not None and delay - (self._skew or 0.0) > STALE_SECONDS:
            stale = f"messaggio di {round(delay - (self._skew or 0.0))} s prima (ora del videocitofono)"
        if stale:
            self._last_stale = now
            # Possibile pressione vecchia consegnata in ritardo: mai un'apertura quando nessuno la aspetta più.
            # Non conta come apertura: un Apri vero subito dopo passa
            self._skip(f"apertura dal citofono non inoltrata: {stale}", attributes, "vecchio")
            return
        self._last_open = now
        note = self._answer(ring)
        # Nel registro come le aperture da Home Assistant: chi o cosa, e da dove ("App Hik-Connect (Videocitofono)")
        source = f"{how} (Videocitofono)"
        task = asyncio.get_running_loop().create_task(self._forward(source, note))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _answer(self, ring: IntercomBridge) -> str:
        """Apertura durante la chiamata: dopo quanti secondi. Chi apre ha risposto, anche senza inoltro: alla fine
        della chiamata non serve "nessuna risposta"."""
        if ring._ring_at is None:
            return ""
        ring._answered = True
        return f"{ring._since_ring()} s dopo la chiamata"

    def _skip(self, detail: str, attributes: dict, why: str) -> None:
        """Apertura non inoltrata: una riga nel registro, una sola per una raffica di messaggi con lo stesso motivo
        (why). Nel log dell'add-on tutti."""
        now = self._clock()
        _LOGGER.info("%s (%s)", detail[:1].upper() + detail[1:], self.settings.name)
        if self._skipped[0] == why and now - self._skipped[1] < SKIP_REPEAT_SECONDS:
            return
        self._skipped = (why, now)
        self._event(detail, attributes)

    async def _forward(self, source: str, note: str = "") -> None:
        try:
            if await self._open_door(source, note) is False:
                _LOGGER.info("Apertura dal citofono non inoltrata: porta già aperta (%s)", source)
                return
            _LOGGER.info("Apertura dal citofono inoltrata all'A1210 (%s)", source)
        except Exception as err:
            # Porta non bloccata, controller irraggiungibile...: resta traccia nel log
            _LOGGER.warning("Apertura dal citofono non eseguita: %s", err)
            self._event(f"apertura non eseguita: {err}")
