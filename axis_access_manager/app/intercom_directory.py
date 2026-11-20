"""Videocitofoni annunciati sul broker dall'add-on Hikvision Doorbell.

L'add-on pubblica, conservate, le configurazioni di discovery di Home Assistant di ogni videocitofono: il sensore
«Call state» e un interruttore per ogni relè della porta («Door N relay»), con nome, modello e numero di serie del
dispositivo e i topic di stato. L'app le legge per mostrare i videocitofoni da scegliere e per legare la porta al
numero di serie, non al nome: il 17/11 il nome salvato nell'app non era più quello dell'add-on, e per un giorno intero
chiamate e aperture non sono arrivate (i topic si fanno con il nome).

L'add-on non cancella mai le configurazioni vecchie: dopo un cambio di nome sul broker ci possono essere due
configurazioni per lo stesso numero di serie, con il nome vecchio e quello nuovo. Il nome giusto è quello con l'ultima
prova dal vivo (una configurazione pubblicata adesso, o un messaggio di stato sui suoi topic); con le sole
configurazioni conservate non si sa quale sia. Le configurazioni restano sul broker anche con l'add-on fermo, e al
ricollegamento l'add-on ripubblica solo la disponibilità.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from .intercom import topic_name

_LOGGER = logging.getLogger(__name__)

# Configurazioni da seguire, sotto il prefisso di discovery di Home Assistant: la chiamata e i relè delle porte
CALL_OBJECT = "Call-state"
RELAY_OBJECT = re.compile(r"^Door-(\d{1,2})-relay$")
MANUFACTURER = "hikvision"
# Numero di serie come lo scrive l'add-on (device.identifiers): sull'impianto sono i codici ASCII del modello e del
# numero di serie uno dopo l'altro, 76 cifre
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def discovery_filters(prefix: str) -> list[str]:
    """Filtri MQTT delle configurazioni dei videocitofoni."""
    return [f"{prefix}/sensor/+/{CALL_OBJECT}/config", f"{prefix}/switch/+/+/config"]


def valid_serial(serial: str) -> bool:
    return bool(SERIAL_PATTERN.match(serial or ""))


def same_name(name: str, other: str) -> bool:
    """Stesso nome, o lo stesso nei topic (spazi e simboli diventano trattini)."""
    return bool(name) and (name == other or topic_name(name) == topic_name(other))


def shown_name(device: dict, stored: str = "") -> str:
    """Nome da mostrare: quello dal vivo; senza, il nome salvato nella porta se è tra quelli del dispositivo, poi
    l'ultimo visto dal vivo (anche prima di un ricollegamento), se no il primo in ordine alfabetico."""
    if device["name"]:
        return device["name"]
    if stored in device["names"]:
        return stored
    return device.get("known") or device["names"][0]


@dataclass
class Entry:
    """Una configurazione: la chiamata o un relè di un videocitofono, con un nome."""

    serial: str
    name: str
    model: str
    kind: str  # "call" o "relay"
    relay: int  # numero del relè (0 per la chiamata)
    state_topic: str
    attributes_topic: str
    availability_topic: str
    # Ora dell'app (monotona) dell'ultima volta che è arrivata e dell'ultima prova dal vivo (None: mai)
    seen_at: float
    live_at: float | None = None

    def same(self, other: Entry) -> bool:
        """Stessa configurazione, a parte le ore."""
        keys = ("serial", "name", "model", "kind", "relay", "state_topic", "attributes_topic", "availability_topic")
        return all(getattr(self, k) == getattr(other, k) for k in keys)


class IntercomDirectory:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        # Configurazione per topic della configurazione
        self._entries: dict[str, Entry] = {}
        # Da quando l'app è collegata al broker (ora monotona), None se non lo è: le configurazioni conservate arrivano
        # subito dopo il collegamento
        self.connected_at: float | None = None
        # Ultimo collegamento, anche dopo che si è perso: senza collegamento valgono le configurazioni di quello
        self._last_connected_at: float | None = None
        # Ultima configurazione arrivata (vedi quiet_for)
        self._config_at: float | None = None
        # Videocitofoni (numero di serie e nome) già scritti nel log
        self._logged: set[tuple[str, str]] = set()
        # Chiamata dopo ogni cambiamento
        self.on_change: Callable[[], None] | None = None

    def connected(self, at: float | None = None) -> None:
        """Collegati (o ricollegati) al broker: contano solo le configurazioni che arrivano da adesso. Una tolta mentre
        l'app era scollegata non manda niente, e resterebbe per sempre."""
        self.connected_at = self._last_connected_at = self._clock() if at is None else at
        self._changed()

    def disconnected(self) -> None:
        if self.connected_at is None:
            return
        self.connected_at = None
        self._changed()

    def since(self) -> float | None:
        """Secondi da quando l'app è collegata al broker; None se non lo è."""
        return None if self.connected_at is None else max(0.0, self._clock() - self.connected_at)

    def quiet_for(self) -> float | None:
        """Secondi senza configurazioni nuove da quando l'app è collegata al broker; None se non lo è. Le configurazioni
        conservate arrivano una dopo l'altra subito dopo il collegamento (anche dopo che Home Assistant ha finito le
        sue pubblicazioni): finché ne arrivano, l'elenco può essere a metà."""
        if self.connected_at is None:
            return None
        last = max(self.connected_at, self._config_at or self.connected_at)
        return max(0.0, self._clock() - last)

    def handle_config(self, topic: str, payload: bytes, retained: bool = False) -> None:
        """Configurazione di discovery (filtri di discovery_filters). Una non conservata è pubblicata adesso: l'add-on
        sta usando quel nome. Vuota: tolta."""
        kind = _kind(topic)
        if kind is None:
            return
        self._config_at = self._clock()
        text = payload.decode(errors="replace").strip()
        if not text:
            if self._entries.pop(topic, None) is not None:
                self._changed()
            return
        entry = _parse(text, *kind, self._clock())
        if entry is None:
            # Non è un videocitofono Hikvision (anche le entità di altre integrazioni passano di qui)
            return
        old = self._entries.get(topic)
        if not retained:
            entry.live_at = entry.seen_at
        elif old is not None and old.serial == entry.serial and old.name == entry.name:
            # La prova dal vivo resta, ma vale solo nel collegamento in cui è arrivata (vedi _winner)
            entry.live_at = old.live_at
        self._entries[topic] = entry
        if (entry.serial, entry.name) not in self._logged:
            self._logged.add((entry.serial, entry.name))
            _LOGGER.info("Videocitofono sul broker: %s, numero di serie %s", entry.name, entry.serial)
        self._changed()

    def mark_live(self, topic: str) -> None:
        """Messaggio non conservato su un topic di stato: il videocitofono con quel nome è quello dal vivo."""
        now = self._clock()
        changed = False
        for entry in self._current():
            if topic in (entry.state_topic, entry.attributes_topic):
                before = self._winner(entry.serial)
                entry.live_at = now
                changed = changed or self._winner(entry.serial) != before
        if changed:
            self._changed()

    def devices(self) -> list[dict]:
        """Videocitofoni per numero di serie: nome dal vivo ("" se non si sa), tutti i nomi, modello, relè e topic."""
        groups: dict[str, list[Entry]] = {}
        for entry in self._current():
            groups.setdefault(entry.serial, []).append(entry)
        result = []
        for serial, entries in groups.items():
            winner = self._winner(serial)
            latest = max(entries, key=lambda e: (e.name == winner, e.live_at or 0.0, e.seen_at))
            relays: dict[int, dict[str, list[str]]] = {}
            for entry in entries:
                if entry.kind == "relay":
                    topics = relays.setdefault(entry.relay, {"state": [], "attributes": [], "availability": []})
                    _add(topics, entry)
            call: dict[str, list[str]] = {"state": [], "attributes": [], "availability": []}
            for entry in entries:
                if entry.kind == "call":
                    _add(call, entry)
            result.append(
                {
                    "serial": serial,
                    "name": winner,
                    # Ultimo nome visto dal vivo, anche con un collegamento di prima: solo da mostrare
                    "known": self._winner(serial, earlier=True),
                    "names": sorted({e.name for e in entries}, key=str.casefold),
                    "model": latest.model,
                    "relays": sorted(relays),
                    "call": call,
                    "relay_topics": relays,
                }
            )
        return sorted(result, key=lambda d: ((d["name"] or d["names"][0]).casefold(), d["serial"]))

    def _current(self) -> list[Entry]:
        """Configurazioni arrivate dal collegamento di adesso (senza collegamento: da quello di prima)."""
        since = self._last_connected_at
        return [e for e in self._entries.values() if since is None or e.seen_at >= since]

    def _winner(self, serial: str, earlier: bool = False) -> str:
        """Nome con la prova dal vivo più recente, arrivata con il collegamento di adesso; "" se non ce n'è. Una prova
        di un collegamento di prima non vale (earlier=True sì, solo per mostrare un nome): dopo ogni ricollegamento le
        configurazioni conservate tornano una alla volta, e se arriva prima quella del nome vecchio la porta tornerebbe
        al nome vecchio, e poi a quello nuovo."""
        since = None if earlier else self._last_connected_at
        live = [
            e
            for e in self._current()
            if e.serial == serial and e.live_at is not None and (since is None or e.live_at >= since)
        ]
        return max(live, key=lambda e: e.live_at or 0.0).name if live else ""

    def _changed(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:
            # Un errore di chi segue i videocitofoni non deve fermare l'ascolto MQTT
            _LOGGER.exception("Errore nell'aggiornamento dei videocitofoni")


def _kind(topic: str) -> tuple[str, int] | None:
    """Chiamata o relè N dal topic della configurazione; None per le altre entità."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[4] != "config":
        return None
    if parts[1] == "sensor" and parts[3] == CALL_OBJECT:
        return "call", 0
    match = RELAY_OBJECT.match(parts[3]) if parts[1] == "switch" else None
    return ("relay", int(match[1])) if match and int(match[1]) > 0 else None


def _parse(text: str, kind: str, relay: int, now: float) -> Entry | None:
    try:
        config = json.loads(text)
    except ValueError:
        return None
    if not isinstance(config, dict) or not isinstance(config.get("device"), dict):
        return None
    device = config["device"]
    if str(device.get("manufacturer") or "").strip().lower() != MANUFACTURER:
        return None
    identifiers = device.get("identifiers")
    if isinstance(identifiers, list):
        identifiers = identifiers[0] if identifiers else ""
    serial = str(identifiers or "").strip() if isinstance(identifiers, str | int) else ""
    name = str(device.get("name") or "").strip()
    unique_id = str(config.get("unique_id") or "")
    state = config.get("state_topic")
    if not valid_serial(serial) or not name or not _topic_ok(state):
        return None
    if "-com_relay_" in unique_id:
        # Relè di comunicazione: non apre la porta
        return None

    def topic(key: str) -> str:
        value = config.get(key)
        return value if _topic_ok(value) else ""

    return Entry(
        serial=serial,
        name=name[:100],
        model=str(device.get("model") or "").strip()[:100],
        kind=kind,
        relay=relay,
        state_topic=state,
        attributes_topic=topic("json_attributes_topic"),
        availability_topic=topic("availability_topic"),
        seen_at=now,
    )


def _topic_ok(value) -> bool:
    """Topic di stato esatto: mai un filtro con + o #, che porterebbe all'app i messaggi di altri dispositivi."""
    return isinstance(value, str) and 0 < len(value) <= 255 and not set(value) & {"+", "#", "\0"}


def _add(topics: dict[str, list[str]], entry: Entry) -> None:
    for key, value in (
        ("state", entry.state_topic),
        ("attributes", entry.attributes_topic),
        ("availability", entry.availability_topic),
    ):
        if value and value not in topics[key]:
            topics[key].append(value)
