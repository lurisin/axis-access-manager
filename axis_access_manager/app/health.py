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
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from .db import Database, now_iso
from .vapix import VapixError

_LOGGER = logging.getLogger(__name__)

HEALTH_INTERVAL = 30
# Controlli falliti di fila prima di segnalare il controller non raggiungibile (un minuto)
OFFLINE_AFTER = 2
# Scarto massimo dell'orologio del controller rispetto a Home Assistant
CLOCK_TOLERANCE = 60
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
    ntp: dict = field(default_factory=dict)

    @property
    def unreachable(self) -> bool:
        return self.failures >= OFFLINE_AFTER

    def as_dict(self) -> dict:
        return {**asdict(self), "unreachable": self.unreachable}


def parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


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
        client = self.make_client(row)
        self._clients[row["id"]] = (key, client)
        return client

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
            return state
        except Exception as err:  # noqa: BLE001 - un controller che risponde male non deve fermare gli altri
            _LOGGER.exception("Controllo del controller %s non riuscito", row["name"])
            state.online, state.error = False, str(err)
            state.failures += 1
            return state
        state.latency_ms = round((time.monotonic() - started) * 1000)
        state.online, state.failures, state.error = True, 0, ""
        state.model = str(info.get("ProdNbr") or info.get("ProdShortName") or "")
        state.firmware = str(info.get("Version") or "")
        state.serial = str(info.get("SerialNumber") or "")
        fingerprint = getattr(client, "certificate_fingerprint", None)
        if fingerprint:
            try:
                state.cert_fingerprint = await fingerprint()
            except VapixError as err:
                _LOGGER.debug("Certificato di %s non letto: %s", row["name"], err)
        await self._clock(client, state)
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
            return
        controller_time = parse_utc(info.get("utcDateTime") or info.get("dateTime") or "")
        # A metà della richiesta: il ritardo della rete non conta come scarto
        state.clock_offset = round(controller_time.timestamp() - (asked + answered) / 2, 1) if controller_time else None
        state.time_zone = str(info.get("timeZone") or info.get("posixTimeZone") or "")
        ntp = getattr(client, "ntp_info", None)
        if ntp:
            try:
                state.ntp = ntp_summary(await ntp())
            except VapixError as err:
                _LOGGER.debug("NTP del controller %s non letto: %s", state.name, err)

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


def clock_problem(state: ControllerHealth) -> bool:
    return state.online is True and state.clock_offset is not None and abs(state.clock_offset) > CLOCK_TOLERANCE


async def _close(client) -> None:
    close = getattr(client, "aclose", None)
    if close:
        try:
            await close()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Chiusura del client non riuscita", exc_info=True)
