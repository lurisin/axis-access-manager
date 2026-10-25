"""Collegamento all'API di Home Assistant tramite il Supervisor: istantanee dalle telecamere."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from .config import supervisor_token

_LOGGER = logging.getLogger(__name__)

CORE_API = "http://supervisor/core/api"
SNAPSHOT_DIR = Path("/data/snapshots")


class HomeAssistant:
    """Client dell'API di Home Assistant; senza token del Supervisor ogni funzione è disattivata."""

    def __init__(self, token: str | None = None, snapshot_dir: Path = SNAPSHOT_DIR) -> None:
        self.token = token if token is not None else supervisor_token()
        self.snapshot_dir = snapshot_dir
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

    async def save_snapshot(self, camera: str, name: str) -> str:
        """Salva un'immagine della telecamera; restituisce il nome del file o '' se non riesce."""
        if not self.available or not camera:
            return ""
        try:
            response = await self._client.get(f"/camera_proxy/{camera}")
        except httpx.HTTPError as err:
            _LOGGER.warning("Istantanea da %s non riuscita: %s", camera, err)
            return ""
        if response.status_code != 200 or not response.content:
            _LOGGER.warning("Istantanea da %s non riuscita: risposta %s", camera, response.status_code)
            return ""
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{name}.jpg"
        await asyncio.to_thread((self.snapshot_dir / filename).write_bytes, response.content)
        return filename

    def delete_snapshots(self, filenames: list[str]) -> None:
        for filename in filenames:
            path = self.snapshot_dir / Path(filename).name
            path.unlink(missing_ok=True)
