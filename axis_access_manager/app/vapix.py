"""Client VAPIX per il controller Axis A1210 (HTTPS + digest)."""

from __future__ import annotations

import httpx

# Limite del controller per GetCredentialList
CREDENTIAL_PAGE_SIZE = 300


class VapixError(Exception):
    """Il controller non risponde o risponde con un errore."""


class A1210Client:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://{host}",
            auth=httpx.DigestAuth(username, password),
            verify=verify_ssl,
            timeout=10,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        try:
            response = await self._client.post(path, json=body)
        except httpx.HTTPError as err:
            raise VapixError(f"il controller non risponde ({err.__class__.__name__})") from err
        if response.status_code == 401:
            raise VapixError("credenziali del controller non valide")
        if response.status_code != 200:
            raise VapixError(f"il controller ha risposto con errore {response.status_code}")
        try:
            return response.json()
        except ValueError as err:
            raise VapixError("risposta del controller non riconosciuta") from err

    async def device_info(self) -> dict:
        """Modello, numero di serie e versione firmware."""
        data = await self._post(
            "/axis-cgi/basicdeviceinfo.cgi",
            {"apiVersion": "1.0", "method": "getAllProperties"},
        )
        if "error" in data:
            raise VapixError(data["error"].get("message", "errore sconosciuto"))
        return data.get("data", {}).get("propertyList", {})

    async def door_info_list(self) -> list[dict]:
        data = await self._post("/vapix/doorcontrol", {"tdc:GetDoorInfoList": {}})
        return data.get("DoorInfo", [])

    async def list_credentials(self) -> list[dict]:
        """Tutte le tessere, lette a pagine da CREDENTIAL_PAGE_SIZE."""
        credentials: list[dict] = []
        start: str | None = None
        while True:
            request: dict = {"Limit": CREDENTIAL_PAGE_SIZE}
            if start:
                request["StartReference"] = start
            data = await self._post("/vapix/pacs", {"pacsaxis:GetCredentialList": request})
            if "Credential" not in data:
                raise VapixError("risposta del controller non riconosciuta")
            credentials.extend(data["Credential"])
            start = data.get("NextStartReference")
            if not start:
                return credentials
