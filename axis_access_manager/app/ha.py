"""Collegamento all'API di Home Assistant tramite il Supervisor: istantanee e chi ha premuto un pulsante."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import supervisor_token
from .installation import valid_camera

_LOGGER = logging.getLogger(__name__)

CORE_API = "http://supervisor/core/api"
# Attributo dei pulsanti dell'add-on in Home Assistant con il comando che mandano: lega la pressione al comando
BUTTON_COMMAND = "comando"
PRESS_WINDOW = 15
# Istantanee nella cartella Media di Home Assistant: si vedono da Media, File editor e Samba.
# Fino alla 0.11 stavano nei dati dell'add-on, invisibili all'utente.
MEDIA_DIR = Path("/media")
SNAPSHOT_DIR = MEDIA_DIR / "axis_access_manager"
LEGACY_SNAPSHOT_DIR = Path("/data/snapshots")


def default_snapshot_dir() -> Path:
    """Cartella Media se l'add-on la vede (map media:rw), altrimenti quella interna di prima."""
    return SNAPSHOT_DIR if MEDIA_DIR.is_dir() else LEGACY_SNAPSHOT_DIR


class HomeAssistant:
    """Client dell'API di Home Assistant; senza token del Supervisor ogni funzione è disattivata."""

    def __init__(self, token: str | None = None, snapshot_dir: Path | None = None) -> None:
        self.token = token if token is not None else supervisor_token()
        self.snapshot_dir = snapshot_dir or default_snapshot_dir()
        self._client = httpx.AsyncClient(
            base_url=CORE_API, timeout=10, headers={"Authorization": f"Bearer {self.token}"} if self.token else {}
        )

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def states(self) -> list[dict]:
        """Telecamere disponibili, per scegliere quella delle istantanee."""
        if not self.available:
            return []
        try:
            response = await self._client.get("/states")
            response.raise_for_status()
            states = response.json()
        except (httpx.HTTPError, ValueError) as err:
            _LOGGER.warning("Stati di Home Assistant non leggibili: %s", err)
            return []
        result = []
        for state in states:
            entity_id = state.get("entity_id", "")
            if entity_id.startswith("camera."):
                result.append(
                    {
                        "entity_id": entity_id,
                        "name": (state.get("attributes") or {}).get("friendly_name") or entity_id,
                        "state": state.get("state"),
                    }
                )
        return sorted(result, key=lambda s: s["entity_id"])

    async def save_snapshot(self, camera: str, name: str) -> tuple[str, str]:
        """Salva un'immagine della telecamera; restituisce (nome del file, "") oppure ("", motivo dell'errore)."""
        # Solo entità camera.*: un valore salvato da una versione vecchia non deve chiamare altre API
        if not self.available or not valid_camera(camera):
            return "", ""
        try:
            response = await self._client.get(f"/camera_proxy/{urllib.parse.quote(camera, safe='')}")
        except httpx.HTTPError as err:
            # Il testo di httpx (in inglese, spesso vuoto con i tempi scaduti) resta nel log
            _LOGGER.warning("Istantanea da %s non riuscita: %r", camera, err)
            if isinstance(err, httpx.TimeoutException):
                return "", "Home Assistant non risponde (tempo scaduto)"
            return "", "Home Assistant non risponde"
        if response.status_code != 200 or not response.content:
            _LOGGER.warning("Istantanea da %s non riuscita: risposta %s", camera, response.status_code)
            return "", f"telecamera non disponibile (risposta {response.status_code} di Home Assistant)"
        if not response.headers.get("content-type", "").lower().startswith("image/"):
            _LOGGER.warning("Istantanea da %s non riuscita: %s", camera, response.headers.get("content-type"))
            return "", "la telecamera non ha mandato un'immagine"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{name}.jpg"
        await asyncio.to_thread((self.snapshot_dir / filename).write_bytes, response.content)
        return filename, ""

    async def button_presser(self, command: str = "", now: datetime | None = None) -> str | None:
        """Nome della persona che ha appena premuto il pulsante dell'add-on che manda questo comando.

        Il comando MQTT non dice chi l'ha mandato: Home Assistant però salva nello stato del pulsante l'ora
        della pressione e l'utente. Si guarda solo il pulsante con questo comando (attributo BUTTON_COMMAND),
        così la pressione di un altro pulsante non attribuisce l'apertura a chi non l'ha fatta. Restituisce None
        se non si riesce a saperlo, anche quando quel pulsante non è stato premuto di recente (comando pubblicato
        direttamente su MQTT), "" se non c'è un utente (ad esempio un'automazione).
        """
        if not self.available or not command:
            return None
        now = now or datetime.now(tz=UTC)
        try:
            response = await self._client.get("/states")
            response.raise_for_status()
            states = response.json()
        except (httpx.HTTPError, ValueError) as err:
            _LOGGER.warning("Utente del pulsante non leggibile da Home Assistant: %s", err)
            return None
        pressed = []
        for state in states:
            if not str(state.get("entity_id", "")).startswith("button."):
                continue
            if (state.get("attributes") or {}).get(BUTTON_COMMAND) != command:
                continue
            try:
                at = datetime.fromisoformat(str(state.get("state")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if abs((now - at).total_seconds()) <= PRESS_WINDOW:
                pressed.append((at, state))
        if not pressed:
            return None
        _, state = max(pressed, key=lambda p: p[0])
        user_id = (state.get("context") or {}).get("user_id")
        if not user_id:
            return ""
        for person in states:
            attributes = person.get("attributes") or {}
            if str(person.get("entity_id", "")).startswith("person.") and attributes.get("user_id") == user_id:
                return attributes.get("friendly_name") or ""
        return None

    def move_snapshots(self, source: Path = LEGACY_SNAPSHOT_DIR) -> int:
        """Sposta nella cartella attuale le istantanee salvate dove stavano prima; restituisce quante."""
        if source == self.snapshot_dir or not source.is_dir():
            return 0
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for path in source.glob("*.jpg"):
            shutil.move(path, self.snapshot_dir / path.name)
            moved += 1
        with contextlib.suppress(OSError):
            source.rmdir()
        return moved

    def delete_snapshots(self, filenames: list[str]) -> None:
        for filename in filenames:
            path = self.snapshot_dir / Path(filename).name
            # Un file che non si cancella (errore di I/O, permessi) non ferma la pulizia degli altri
            try:
                path.unlink(missing_ok=True)
            except OSError as err:
                _LOGGER.warning("Istantanea %s non cancellata: %s", path, err)
