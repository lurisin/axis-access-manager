"""Client VAPIX per il controller Axis A1210 (HTTPS + digest)."""

from __future__ import annotations

import httpx

# Limite del controller per GetCredentialList
CREDENTIAL_PAGE_SIZE = 300


class VapixError(Exception):
    """Il controller non risponde o risponde con un errore."""


def _fault_text(fault) -> str:
    if isinstance(fault, dict):
        for key in ("Reason", "Text", "message", "Message"):
            if fault.get(key):
                return str(fault[key])
    return str(fault)


def _first_token(data: dict, item: dict) -> str:
    """Token restituito da una chiamata Set (lista o stringa), altrimenti quello inviato."""
    token = data.get("Token")
    if isinstance(token, list):
        token = token[0] if token else ""
    return str(token or item.get("token", ""))


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
            detail = response.text.strip()[:200]
            message = f"il controller ha risposto con errore {response.status_code}"
            raise VapixError(f"{message}: {detail}" if detail else message)
        try:
            data = response.json()
        except ValueError as err:
            raise VapixError("risposta del controller non riconosciuta") from err
        if isinstance(data, dict) and "Fault" in data:
            raise VapixError(f"il controller ha rifiutato la richiesta: {_fault_text(data['Fault'])}")
        return data

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

    async def _list_all(self, path: str, method: str, key: str, required: bool = False) -> list[dict]:
        """Elenco completo letto a pagine da CREDENTIAL_PAGE_SIZE."""
        items: list[dict] = []
        start: str | None = None
        while True:
            request: dict = {"Limit": CREDENTIAL_PAGE_SIZE}
            if start:
                request["StartReference"] = start
            data = await self._post(path, {method: request})
            if required and key not in data:
                raise VapixError("risposta del controller non riconosciuta")
            items.extend(data.get(key) or [])
            start = data.get("NextStartReference")
            if not start:
                return items

    async def list_credentials(self) -> list[dict]:
        return await self._list_all("/vapix/pacs", "pacsaxis:GetCredentialList", "Credential", required=True)

    async def list_users(self) -> list[dict]:
        """Tutti gli utenti (titolari delle tessere)."""
        return await self._list_all("/vapix/pacs", "axudb:GetUserList", "User")

    async def get_users(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/pacs", {"axudb:GetUser": {"Token": tokens}})
        return data.get("User", [])

    async def set_user(self, user: dict) -> None:
        """Crea l'utente o, se il token esiste già, lo aggiorna."""
        await self._post("/vapix/pacs", {"axudb:SetUser": {"User": [user]}})

    async def remove_user(self, token: str) -> None:
        await self._post("/vapix/pacs", {"axudb:RemoveUser": {"Token": [token]}})

    async def get_credentials(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/pacs", {"pacsaxis:GetCredential": {"Token": tokens}})
        return data.get("Credential", [])

    async def set_credential(self, credential: dict) -> str:
        """Crea (token vuoto) o aggiorna una tessera; restituisce il token assegnato."""
        data = await self._post("/vapix/pacs", {"pacsaxis:SetCredential": {"Credential": [credential]}})
        return _first_token(data, credential)

    async def remove_credential(self, token: str) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:RemoveCredential": {"Token": [token]}})

    async def enable_credential(self, token: str) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:EnableCredential": {"Token": token}})

    async def disable_credential(self, token: str) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:DisableCredential": {"Token": token, "Status": "Disabled"}})

    # Gruppi (profili di accesso) e punti di accesso

    async def list_access_points(self) -> list[dict]:
        return await self._list_all("/vapix/pacs", "pacsaxis:GetAccessPointList", "AccessPoint")

    async def list_access_profiles(self) -> list[dict]:
        return await self._list_all("/vapix/pacs", "pacsaxis:GetAccessProfileList", "AccessProfile", required=True)

    async def get_access_profiles(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/pacs", {"pacsaxis:GetAccessProfile": {"Token": tokens}})
        return data.get("AccessProfile", [])

    async def set_access_profile(self, profile: dict) -> str:
        """Crea (token vuoto) o aggiorna un profilo; restituisce il token assegnato."""
        data = await self._post("/vapix/pacs", {"pacsaxis:SetAccessProfile": {"AccessProfile": [profile]}})
        return _first_token(data, profile)

    async def remove_access_profile(self, token: str) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:RemoveAccessProfile": {"Token": [token]}})

    # Fasce orarie

    async def list_schedules(self) -> list[dict]:
        return await self._list_all("/vapix/schedule", "axsch:GetScheduleList", "Schedule", required=True)

    async def set_schedule(self, schedule: dict) -> str:
        """Crea (token vuoto) o aggiorna una fascia oraria; restituisce il token assegnato."""
        data = await self._post("/vapix/schedule", {"axsch:SetSchedule": {"Schedule": [schedule]}})
        return _first_token(data, schedule)

    async def remove_schedule(self, token: str) -> None:
        await self._post("/vapix/schedule", {"axsch:RemoveSchedule": {"Token": [token]}})

    async def door_mode(self, door_token: str) -> str | None:
        """Modalità attuale della porta (Locked, Accessed, Unlocked...)."""
        data = await self._post("/vapix/doorcontrol", {"tdc:GetDoorState": {"Token": door_token}})
        return (data.get("DoorState") or {}).get("DoorMode")

    async def access_door(self, door_token: str, extended: bool = False) -> None:
        """Sblocco temporaneo. Mai UnlockDoor: la serratura non regge l'alimentazione continua."""
        request: dict = {"Token": door_token}
        if extended:
            request["UseExtendedTime"] = True
        await self._post("/vapix/doorcontrol", {"tdc:AccessDoor": request})
