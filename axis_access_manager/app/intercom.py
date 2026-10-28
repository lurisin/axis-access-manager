"""Citofono Hikvision: chiamate nel log e "Apri" dal citofono inoltrato all'A1210.

Gli eventi arrivano dall'add-on Hikvision Doorbell, che li pubblica sullo stesso broker MQTT
(libreria ha-mqtt-discoverable, topic <prefisso>/<componente>/<dispositivo>/<entità>/state).
Il relè del citofono non comanda la serratura: quando il citofono segnala un'apertura,
l'add-on manda all'A1210 un solo AccessDoor, mai uno sblocco permanente.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .db import now_iso

_LOGGER = logging.getLogger(__name__)

# Un'apertura dal citofono genera più messaggi (comando da HA ed evento del citofono):
# entro questo intervallo si manda un solo AccessDoor
OPEN_DEBOUNCE_SECONDS = 5.0
# Gli attributi (chi ha aperto) arrivano subito prima dello stato ON
ATTRIBUTES_MAX_AGE_SECONDS = 5.0

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
# Origine dell'apertura nel registro ("App Hik-Connect (Citofono)")
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
    ) -> None:
        self.settings = settings
        self._log = log
        self._open_door = open_door
        self._clock = clock
        device = topic_name(settings.name)
        base = f"{settings.state_prefix}"
        self.call_topic = f"{base}/sensor/{device}/{topic_name('Call state')}/state"
        relay = topic_name(f"Door {settings.relay} relay")
        self.relay_topic = f"{base}/switch/{device}/{relay}/state"
        self.relay_attributes_topic = f"{base}/switch/{device}/{relay}/attributes"
        # Stesso citofono su due porte (relè 1 e 2): la chiamata è una e la segue il bridge della prima porta
        self.call: IntercomBridge | None = None
        self._call_state = "idle"
        self._ring_at: float | None = None
        self._answered = False
        self._attributes: dict = {}
        self._attributes_at = 0.0
        self._last_open = float("-inf")
        self._tasks: set[asyncio.Task] = set()
        self.last_message_at: str | None = None

    @property
    def topics(self) -> list[str]:
        return [self.call_topic, self.relay_topic, self.relay_attributes_topic]

    @property
    def call_state(self) -> str:
        return self.call.call_state if self.call else self._call_state

    def handle(self, topic: str, payload: bytes, retained: bool = False) -> None:
        # Gli stati sono conservati dal broker: alla riconnessione non sono eventi nuovi
        if retained:
            return
        self.last_message_at = now_iso()
        text = payload.decode(errors="replace").strip()
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
        elif topic == self.relay_topic and text.upper() == "ON":
            self._on_unlock()

    def _event(self, detail: str, raw: dict | None = None) -> None:
        self._log(ts=now_iso(), kind="citofono", detail=detail, source=self.settings.name, raw=raw)

    def _on_call_state(self, value: str) -> None:
        state = value.lower()
        previous, self._call_state = self._call_state, state
        if state == previous:
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
        if now - self._last_open < OPEN_DEBOUNCE_SECONDS:
            return
        self._last_open = now
        if unlock_type in LOCAL_CREDENTIALS:
            # Il visitatore entra da solo: se ha suonato, nessuno ha risposto
            self._event(
                f"apertura con {LOCAL_CREDENTIALS[unlock_type]} del citofono non inoltrata all'A1210", attributes
            )
            return
        note = ""
        ring = self.call or self
        if ring._ring_at is not None:
            note = f"{ring._since_ring()} s dopo la chiamata"
            # Chi apre ha risposto, anche senza inoltro: alla fine della chiamata non serve "nessuna risposta"
            ring._answered = True
        if not self.settings.open_door:
            detail = ", ".join(x for x in (f"apertura da {how} (inoltro all'A1210 disattivato)", note) if x)
            self._event(detail, attributes)
            return
        # Nel registro come le aperture da Home Assistant: chi o cosa, e da dove ("App Hik-Connect (Citofono)")
        source = f"{how} (Citofono)"
        task = asyncio.get_running_loop().create_task(self._forward(source, note))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _forward(self, source: str, note: str = "") -> None:
        try:
            await self._open_door(source, note)
            _LOGGER.info("Apertura dal citofono inoltrata all'A1210 (%s)", source)
        except Exception as err:
            # Porta non bloccata, controller irraggiungibile...: resta traccia nel log
            _LOGGER.warning("Apertura dal citofono non eseguita: %s", err)
            self._event(f"apertura non eseguita: {err}")
