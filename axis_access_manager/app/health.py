"""Stato di ogni controller: raggiungibile, identità, orologio. Controllo in parallelo ogni HEALTH_INTERVAL.

L'identità (numero di serie, modello, firmware, impronta del certificato) si memorizza al primo collegamento.
Se all'indirizzo risponde un apparecchio diverso, le scritture verso quel controller restano sospese
finché un amministratore non conferma il cambio: così un controller sostituito o resettato non riceve
modifiche e non svuota l'anagrafica.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, now_iso
from .programming import CONTROLLER_INPUTS, rex_policies, rex_uncovered, unlinked_points
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

HEALTH_INTERVAL = 30
# Controlli falliti di fila prima di segnalare il controller non raggiungibile (un minuto)
OFFLINE_AFTER = 2
# Scarto massimo dell'orologio del controller rispetto a Home Assistant
CLOCK_TOLERANCE = 60
# Ora e NTP cambiano di rado: si rileggono un controllo su CLOCK_EVERY (ogni 5 minuti) e quando il controller
# torna raggiungibile. Collegamento e identità si controllano sempre.
CLOCK_EVERY = 10
IDENTITY_FIELDS = ("serial", "model", "firmware", "cert_fingerprint")


@dataclass
class ControllerHealth:
    id: int
    name: str
    online: bool | None = None
    failures: int = 0
    error: str = ""
    checked_at: str = ""
    latency_ms: int | None = None
    serial: str = ""
    model: str = ""
    firmware: str = ""
    cert_fingerprint: str = ""
    # Secondi in più (o in meno) dell'orologio del controller rispetto a Home Assistant
    clock_offset: float | None = None
    time_zone: str = ""
    # Fuso del controller con ore diverse da quello di Home Assistant: fasce orarie e festività sfasate
    zone_differs: bool = False
    ha_time_zone: str = ""
    ntp: dict = field(default_factory=dict)
    # Punti di accesso per cui il controller non decide (dopo un reset): lì tessere e pulsanti non aprono
    unmanaged: list[str] = field(default_factory=list)
    # Ingressi che aprono la porta senza il permesso di farlo: il controller li nega
    rex_missing: list[str] = field(default_factory=list)
    # Controlli riusciti da quando risponde: decide quando rileggere ora e NTP
    answered: int = 0

    @property
    def unreachable(self) -> bool:
        return self.failures >= OFFLINE_AFTER

    def as_dict(self) -> dict:
        data = asdict(self)
        data.pop("answered")
        return {**data, "unreachable": self.unreachable}


def parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def ha_time_zone() -> str:
    """Fuso orario di Home Assistant (per esempio Europe/Rome), vuoto se l'add-on non lo conosce."""
    return os.environ.get("TZ") or ""


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def zone_differs(info: dict, ha_zone: str, now: datetime | None = None) -> bool:
    """Il fuso del controller ha ore diverse da quello di Home Assistant, adesso o in un altro periodo dell'anno.

    Fasce orarie e festività si scrivono in ora locale del controller: con un fuso diverso scattano all'ora sbagliata,
    anche se l'orologio è giusto. Nomi diversi con le stesse ore (Europe/Rome ed Europe/Berlin) vanno bene.
    Se uno dei due fusi non si conosce non si segnala niente.
    """
    ha = _zone(ha_zone)
    if ha is None:
        return False
    now = now or datetime.now(tz=UTC)
    # Ora locale del controller con lo scarto da UTC, se la dà: vale anche per un fuso POSIX senza nome
    try:
        local = datetime.fromisoformat(str(info.get("localDateTime") or "").replace("Z", "+00:00"))
    except ValueError:
        local = None
    if local is not None and local.tzinfo is not None and local.utcoffset() != local.astimezone(ha).utcoffset():
        return True
    controller = _zone(str(info.get("timeZone") or ""))
    if controller is None:
        return False
    # Oggi, in inverno e in estate: un fuso senza ora legale (UTC) si distingue anche quando oggi coincide
    moments = (now, datetime(now.year, 1, 15, 12, tzinfo=UTC), datetime(now.year, 7, 15, 12, tzinfo=UTC))
    return any(m.astimezone(controller).utcoffset() != m.astimezone(ha).utcoffset() for m in moments)


def ntp_summary(info: dict) -> dict:
    """Server NTP e sincronizzazione. L'A1210 (API 1.2) risponde con {"client": {...}}."""
    client = info.get("client") if isinstance(info.get("client"), dict) else info
    source = str(client.get("serversSource") or client.get("timeServersSource") or "")
    static = client.get("staticServers") or client.get("timeServers") or []
    advertised = client.get("advertisedServers") or []
    servers = (advertised or static) if source.upper() == "DHCP" else (static or advertised)
    if isinstance(servers, str):
        servers = [servers]
    synced = client.get("synced", client.get("timeServersActive"))
    offset = client.get("timeOffset")
    return {
        "enabled": client.get("enabled"),
        "source": source,
        "servers": [str(s) for s in servers if s],
        "active": bool(synced) if synced is not None else None,
        "offset": round(float(offset), 3) if isinstance(offset, int | float) else None,
    }


def identity_text(old: dict, new: dict) -> str:
    """Cosa è cambiato, per il guasto e il registro attività."""
    labels = {"serial": "numero di serie", "model": "modello", "cert_fingerprint": "certificato"}
    parts = []
    for key, label in labels.items():
        if old.get(key) and new.get(key) and old[key] != new[key]:
            if key == "cert_fingerprint":
                parts.append("certificato HTTPS diverso")
            else:
                parts.append(f"{label} {old[key]} → {new[key]}")
    return ", ".join(parts)


class HealthMonitor:
    def __init__(
        self, db: Database, make_client: Callable[[dict], object], on_change: Callable[[], None] | None = None
    ):
        self.db = db
        self.make_client = make_client
        self.on_change = on_change
        self.states: dict[int, ControllerHealth] = {}
        self._clients: dict[int, tuple[tuple, object]] = {}

    def _client(self, row: dict):
        key = (row["host"], row["username"], row["password"], row["verify_ssl"])
        current = self._clients.get(row["id"])
        if current and current[0] == key:
            return current[1]
        if current:
            asyncio.get_running_loop().create_task(_close(current[1]))
        if row["id"] in self.states:
            # Indirizzo o credenziali cambiati: al prossimo controllo si rileggono anche ora e NTP
            self.states[row["id"]].answered = 0
        client = self.make_client(row)
        self._clients[row["id"]] = (key, client)
        return client

    def client_for(self, row: dict):
        """Client di sola lettura per un controller (per esempio per il backup)."""
        return self._client(row)

    async def aclose(self) -> None:
        for _, client in self._clients.values():
            await _close(client)
        self._clients.clear()

    async def check_all(self) -> None:
        controllers = [c for c in self.db.list_controllers() if c["host"]]
        ids = {c["id"] for c in controllers}
        for gone in set(self.states) - ids:
            self.states.pop(gone, None)
        for gone in set(self._clients) - ids:
            await _close(self._clients.pop(gone)[1])
        before = {i: (s.online, s.unreachable) for i, s in self.states.items()}
        await asyncio.gather(*(self.check(c) for c in controllers))
        after = {i: (s.online, s.unreachable) for i, s in self.states.items()}
        if before != after and self.on_change:
            self.on_change()

    async def check(self, row: dict) -> ControllerHealth:
        state = self.states.setdefault(row["id"], ControllerHealth(row["id"], row["name"]))
        state.name = row["name"]
        state.checked_at = now_iso()
        client = self._client(row)
        started = time.monotonic()
        try:
            info = await client.device_info()
        except VapixError as err:
            state.online = False
            state.failures += 1
            state.error = str(err)
            state.latency_ms = None
            state.answered = 0
            return state
        except Exception:  # noqa: BLE001 - un controller che risponde male non deve fermare gli altri
            # Il dettaglio (in inglese) solo nel log
            _LOGGER.exception("Controllo del controller %s non riuscito", row["name"])
            state.online, state.error = False, "risposta del controller non riconosciuta"
            state.failures += 1
            state.latency_ms = None
            state.answered = 0
            return state
        state.latency_ms = round((time.monotonic() - started) * 1000)
        state.online, state.failures, state.error = True, 0, ""
        state.model = str(info.get("ProdNbr") or info.get("ProdShortName") or "")
        state.firmware = str(info.get("Version") or "")
        state.serial = str(info.get("SerialNumber") or "")
        # Il certificato a ogni controllo: è solo l'avvio di un collegamento TLS, senza richieste al controller
        fingerprint = getattr(client, "certificate_fingerprint", None)
        if fingerprint:
            try:
                state.cert_fingerprint = await fingerprint()
            except VapixError as err:
                _LOGGER.debug("Certificato di %s non letto: %s", row["name"], err)
        if state.answered % CLOCK_EVERY == 0:
            await self._clock(client, state)
            await self._managed(client, state)
        state.answered += 1
        self.check_identity(row, state)
        return state

    async def _clock(self, client, state: ControllerHealth) -> None:
        reader = getattr(client, "date_time_info", None)
        if reader is None:
            return
        try:
            asked = time.time()
            info = await reader()
            answered = time.time()
        except VapixError as err:
            _LOGGER.debug("Ora del controller %s non letta: %s", state.name, err)
            state.clock_offset = None
            state.zone_differs = False
            return
        controller_time = parse_utc(info.get("utcDateTime") or info.get("dateTime") or "")
        ntp = getattr(client, "ntp_info", None)
        if ntp:
            try:
                state.ntp = ntp_summary(await ntp())
            except VapixError as err:
                _LOGGER.debug("NTP del controller %s non letto: %s", state.name, err)
        # Scarto scritto dopo l'NTP: chi legge lo stato nel frattempo non vede uno scarto senza sapere
        # se il controller è sincronizzato ("36 s avanti" invece di "Home Assistant è 36 s indietro").
        # A metà della richiesta: il ritardo della rete non conta come scarto
        state.clock_offset = round(controller_time.timestamp() - (asked + answered) / 2, 1) if controller_time else None
        state.time_zone = str(info.get("timeZone") or info.get("posixTimeZone") or "")
        state.ha_time_zone = ha_time_zone()
        state.zone_differs = zone_differs(info, state.ha_time_zone)

    async def recheck_clock(self, row: dict | None) -> None:
        """Dopo aver scritto il fuso orario nel controller: l'avviso sparisce subito, non al giro dei 5 minuti."""
        state = self.states.get(row["id"]) if row else None
        if state is not None:
            await self._clock(self._client(row), state)

    async def recheck_managed(self, row: dict | None) -> None:
        """Dopo averli fatti gestire (programmazione, ripristino): l'avviso sparisce subito, non al giro dell'ora."""
        state = self.states.get(row["id"]) if row else None
        if state is not None:
            await self._managed(self._client(row), state)

    async def _managed(self, client, state: ControllerHealth) -> None:
        if getattr(client, "list_access_controllers", None) is None:
            return
        try:
            points = await client.list_access_points()
            controllers = await client.list_access_controllers()
        except VapixError as err:
            _LOGGER.debug("Punti di accesso di %s non letti: %s", state.name, err)
            return
        state.unmanaged = [
            p.get("Name") or p["token"] for p in unlinked_points(points, controllers) if p.get("Enabled") is not False
        ]
        rows = self.db.controller_inputs(state.id)
        existing = {p.get("token") for p in points}
        wanted = [t for t in rex_policies(rows.values()) if t in existing]
        try:
            missing = set(rex_uncovered(await client.list_access_profiles(), wanted)) if wanted else set()
        except VapixError as err:
            _LOGGER.debug("Profili di accesso di %s non letti: %s", state.name, err)
            return
        state.rex_missing = [
            rows[io]["name"] for io in CONTROLLER_INPUTS if io in rows and rows[io]["access_point_token"] in missing
        ]

    def check_identity(self, row: dict, state: ControllerHealth) -> None:
        """Primo collegamento: si memorizza l'identità. Apparecchio diverso: cambio da confermare."""
        observed = {k: getattr(state, k) for k in IDENTITY_FIELDS}
        if not observed["serial"]:
            return
        stored = {k: row.get(k) or "" for k in IDENTITY_FIELDS}
        if not stored["serial"]:
            self.db.set_controller_identity(row["id"], observed)
            self.db.add_audit(
                "sistema",
                "controller riconosciuto",
                f"{row['name']}: {observed['model']} n. {observed['serial']}, firmware {observed['firmware']}",
            )
            return
        changed = identity_text(stored, observed)
        pending = json.loads(row.get("identity_change") or "{}")
        if changed:
            same = all(pending.get(k) == observed[k] for k in ("serial", "cert_fingerprint"))
            if not same:
                self.db.set_controller_identity_change(row["id"], json.dumps({**observed, "at": now_iso()}))
                self.db.add_audit("sistema", "controller cambiato", f"{row['name']}: {changed}, scritture sospese")
                _LOGGER.warning("Controller %s cambiato (%s): scritture sospese", row["name"], changed)
            return
        if pending:
            # Risponde di nuovo il controller memorizzato: niente da confermare
            self.db.set_controller_identity_change(row["id"], "")
            self.db.add_audit("sistema", "controller di nuovo riconosciuto", row["name"])
        updates = {k: observed[k] for k in IDENTITY_FIELDS if observed[k] and not stored[k]}
        if observed["firmware"] and stored["firmware"] and observed["firmware"] != stored["firmware"]:
            updates["firmware"] = observed["firmware"]
            self.db.add_audit(
                "sistema",
                "firmware del controller aggiornato",
                f"{row['name']}: {stored['firmware']} → {observed['firmware']}",
            )
        if updates:
            self.db.set_controller_identity(row["id"], {**stored, **updates})

    def accept_identity(self, controller_id: int, user: str) -> dict:
        """L'amministratore conferma il controller trovato all'indirizzo: diventa quello memorizzato."""
        row = self.db.get_controller(controller_id)
        if row is None:
            raise LookupError("controller non trovato")
        pending = row.get("identity_change") or ""
        if not pending:
            return row
        observed = json.loads(pending)
        old = {k: row.get(k) or "" for k in IDENTITY_FIELDS}
        self.db.set_controller_identity(controller_id, {k: observed.get(k, "") for k in IDENTITY_FIELDS})
        self.db.set_controller_identity_change(controller_id, "")
        self.db.add_audit(user, "cambio del controller confermato", f"{row['name']}: {identity_text(old, observed)}")
        return self.db.get_controller(controller_id)


def paused_reason(row: dict | None) -> str:
    """Motivo per cui le scritture verso il controller sono sospese, vuoto se non lo sono."""
    if not row or not row.get("identity_change"):
        return ""
    try:
        observed = json.loads(row["identity_change"])
    except ValueError:
        observed = {}
    what = identity_text({k: row.get(k) or "" for k in IDENTITY_FIELDS}, observed) or "apparecchio diverso"
    return f"il controller {row['name']} è cambiato ({what}): conferma il cambio in Impianti"


def controller_synced(state: ControllerHealth) -> bool:
    """Il controller dice di essere sincronizzato con un server NTP, con uno scarto entro la tolleranza."""
    offset = state.ntp.get("offset")
    return state.ntp.get("active") is True and (offset is None or abs(offset) <= CLOCK_TOLERANCE)


def clock_fault(state: ControllerHealth) -> str:
    """Quale orologio è sbagliato: "controller", "home_assistant" o vuoto se lo scarto è nella tolleranza.

    Lo scarto misura solo la differenza tra i due. Se il controller è sincronizzato via NTP l'ora giusta è la sua,
    e a essere fuori è Home Assistant (per esempio perché il suo server NTP non risponde).
    """
    if state.online is not True or state.clock_offset is None or abs(state.clock_offset) <= CLOCK_TOLERANCE:
        return ""
    return "home_assistant" if controller_synced(state) else "controller"


def clock_problem(state: ControllerHealth) -> bool:
    """Orologio del controller sbagliato: le fasce orarie scattano all'ora sbagliata."""
    return clock_fault(state) == "controller"


async def _close(client) -> None:
    close = getattr(client, "aclose", None)
    if close:
        try:
            await close()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Chiusura del client non riuscita", exc_info=True)
