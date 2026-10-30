"""Client VAPIX per il controller Axis A1210 (HTTPS + digest)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import ssl

import httpx

_LOGGER = logging.getLogger(__name__)

# Limite del controller per GetCredentialList
CREDENTIAL_PAGE_SIZE = 300
# Codici di errore dell'API Axis (FaultCode, Subcode o messaggio) → spiegazione per l'utente; il codice va nel log
FAULT_TEXTS = {
    "NotFound": "oggetto non trovato",
    "InvalidArgVal": "valore non accettato",
    "InvalidArgs": "valori non accettati",
    "InvalidArg": "valore non accettato",
    "ActionNotSupported": "operazione non supportata",
    "Method not supported": "operazione non supportata",
    "OperationProhibited": "operazione non permessa",
    "NotAuthorized": "operazione non permessa",
    "CapabilityViolated": "limite del controller raggiunto",
    "ReferenceInUse": "oggetto ancora in uso",
    "ModeNotAllowedForIoUser": "modo non ammesso",
}


class VapixError(Exception):
    """Il controller non risponde o risponde con un errore."""

    def __init__(self, message: str, fault: str = "") -> None:
        super().__init__(message)
        # Codice dell'errore Axis com'è arrivato (per esempio ter:NotFound): per i controlli, non per l'utente
        self.fault = fault


class ControllerUnreachable(VapixError):
    """Il controller non risponde: le richieste successive aspetterebbero allo stesso modo."""


def _fault_code(data) -> str:
    """Codici e testi di un Fault, o di una risposta {"error": ...} delle /axis-cgi, così come arrivano."""
    if not isinstance(data, dict):
        return ""
    parts = [data.get("FaultCode"), data.get("Subcode")]
    for key in ("Fault", "error"):
        value = data.get(key)
        if isinstance(value, dict):
            parts += [value.get(k) for k in ("Subcode", "Code", "code", "Reason", "Text", "message", "Message")]
        elif value:
            parts.append(value)
    return " ".join(str(p) for p in parts if p not in (None, ""))


def _refused(what: str, fault: str) -> VapixError:
    reason = next((text for code, text in FAULT_TEXTS.items() if code in fault), "")
    return VapixError(f"il controller ha rifiutato {what}" + (f" ({reason})" if reason else ""), fault)


def _network_reason(err: Exception) -> str:
    """Perché il collegamento non è riuscito, in parole: il nome dell'eccezione resta nel log."""
    if isinstance(err, httpx.ConnectTimeout | TimeoutError):
        return "tempo scaduto"
    if isinstance(err, httpx.TimeoutException):
        return "nessuna risposta in tempo"
    if "CERTIFICATE_VERIFY_FAILED" in str(err):
        return "certificato HTTPS non valido"
    if isinstance(err, ssl.SSLError):
        return "collegamento HTTPS non riuscito"
    if isinstance(err, socket.gaierror):
        return "indirizzo non trovato"
    if isinstance(err, httpx.ConnectError | ConnectionRefusedError):
        return "collegamento rifiutato o indirizzo errato"
    if isinstance(err, ValueError | httpx.InvalidURL):
        return "indirizzo non valido"
    return "errore di rete"


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
        self.host = host
        options = {"auth": httpx.DigestAuth(username, password), "verify": verify_ssl, "timeout": 10}
        try:
            self._client = httpx.AsyncClient(base_url=f"https://{host}", transport=transport, **options)
            self._invalid_host = False
        except httpx.InvalidURL:
            # Indirizzo salvato prima che l'app lo controllasse: ogni richiesta risponde con un errore, ma l'avvio
            # dell'app non si ferma e l'indirizzo si può correggere
            self._client = httpx.AsyncClient(transport=transport, **options)
            self._invalid_host = True

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, body: dict) -> dict:
        method = body.get("method") or next(iter(body), "")
        if self._invalid_host:
            raise ControllerUnreachable("indirizzo del controller non valido")
        # Client chiuso da una nuova configurazione mentre un'operazione lo usava ancora: httpx solleva
        # RuntimeError, che chi chiama non gestirebbe come un errore del controller
        if self._client.is_closed:
            raise ControllerUnreachable("il controller non risponde (collegamento chiuso)")
        try:
            response = await self._client.post(path, json=body)
        except (httpx.HTTPError, httpx.InvalidURL) as err:
            _LOGGER.debug("%s %s: %r", path, method, err)
            raise ControllerUnreachable(f"il controller non risponde ({_network_reason(err)})") from err
        except RuntimeError as err:
            if not self._client.is_closed:
                raise
            _LOGGER.debug("%s %s: %r", path, method, err)
            raise ControllerUnreachable("il controller non risponde (collegamento chiuso)") from err
        if response.status_code == 401:
            raise VapixError("credenziali del controller non valide")
        try:
            data = response.json()
        except ValueError as err:
            if response.status_code == 200:
                raise VapixError("risposta del controller non riconosciuta") from err
            data = None
        if response.status_code != 200 or (isinstance(data, dict) and "Fault" in data):
            # Corpo e codici in inglese servono a chi legge il log, non all'utente
            _LOGGER.info(
                "Il controller ha risposto %s a %s %s: %s", response.status_code, path, method, response.text[:500]
            )
            fault = _fault_code(data)
            if fault or response.status_code == 200:
                raise _refused("la richiesta", fault)
            raise VapixError(f"il controller ha risposto con errore {response.status_code}")
        return data

    @staticmethod
    def _check_error(path: str, data: dict) -> None:
        """Errore delle API /axis-cgi: arriva con lo stato 200 e {"error": {"code": ..., "message": ...}}."""
        if "error" in data:
            _LOGGER.info("Il controller ha risposto con un errore a %s: %s", path, data["error"])
            raise _refused("la richiesta", _fault_code(data))

    async def device_info(self) -> dict:
        """Modello, numero di serie e versione firmware."""
        data = await self._post(
            "/axis-cgi/basicdeviceinfo.cgi",
            {"apiVersion": "1.0", "method": "getAllProperties"},
        )
        self._check_error("/axis-cgi/basicdeviceinfo.cgi", data)
        return data.get("data", {}).get("propertyList", {})

    async def _cgi(self, path: str, method: str, params: dict | None = None, version: str = "1.0") -> dict:
        """API JSON delle /axis-cgi (di serie apiVersion 1.0): restituisce "data"."""
        body: dict = {"apiVersion": version, "method": method}
        if params is not None:
            body["params"] = params
        data = await self._post(path, body)
        self._check_error(path, data)
        return data.get("data") or {}

    async def date_time_info(self) -> dict:
        """Ora del controller: dateTime (locale), utcDateTime, timeZone."""
        return await self._cgi("/axis-cgi/time.cgi", "getDateTimeInfo")

    async def ntp_info(self) -> dict:
        """Server NTP configurati e in uso."""
        return await self._cgi("/axis-cgi/ntp.cgi", "getNTPInfo")

    async def mqtt_client_config(self) -> dict:
        """Broker MQTT configurato sul controller (per il backup): {"config": {...}, "status": {...}}."""
        return await self._cgi("/axis-cgi/mqtt/client.cgi", "getClientStatus")

    async def mqtt_event_config(self) -> dict:
        """Eventi pubblicati su MQTT e prefisso dei topic (per il backup)."""
        return await self._cgi("/axis-cgi/mqtt/event.cgi", "getEventPublicationConfig")

    # Impostazioni del dispositivo, per il ripristino completo di un controller resettato o sostituito

    async def _cgi_write(self, path: str, method: str, params: dict) -> dict:
        """Scrittura con l'ultima apiVersion che il controller dichiara: le letture con la 1.0 restituiscono già i
        campi delle versioni nuove (sull'A1210 del banco MQTT 1.5, eventi 1.2), che la 1.0 potrebbe rifiutare."""
        try:
            data = await self._post(path, {"method": "getSupportedVersions"})
            versions = (data.get("data") or {}).get("apiVersions") or []
            version = max(versions, key=lambda v: tuple(int(x) for x in str(v).split(".") if x.isdigit()))
        except (VapixError, ValueError):
            version = "1.0"
        return await self._cgi(path, method, params, str(version))

    async def set_time_zone(self, time_zone: str) -> None:
        """Fuso orario con il nome IANA (per esempio Europe/Rome): le fasce orarie si leggono in ora locale."""
        await self._cgi_write("/axis-cgi/time.cgi", "setTimeZone", {"timeZone": time_zone})

    async def set_ntp(self, enabled: bool, source: str, servers: list[str]) -> None:
        params: dict = {"enabled": enabled, "serversSource": source or "static"}
        if servers:
            params["staticServers"] = servers
        await self._cgi_write("/axis-cgi/ntp.cgi", "setNTPClientConfiguration", params)

    async def set_mqtt_client(self, config: dict) -> None:
        """Broker MQTT (con la password, se serve) e collegamento attivo: gli eventi tornano subito."""
        await self._cgi_write("/axis-cgi/mqtt/client.cgi", "configureClient", config)
        await self._cgi_write("/axis-cgi/mqtt/client.cgi", "activateClient", {})

    async def set_mqtt_events(self, config: dict) -> None:
        """Eventi da pubblicare su MQTT e prefisso dei topic."""
        await self._cgi_write("/axis-cgi/mqtt/event.cgi", "configureEventPublication", config)

    async def certificate_fingerprint(self) -> str:
        """Impronta SHA-256 del certificato HTTPS, per riconoscere il controller (come fa un browser)."""
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            # Come il client: anche un IPv6 tra parentesi quadre ([fd00::10]:443)
            url = httpx.URL(f"https://{self.host}")
            _, writer = await asyncio.wait_for(asyncio.open_connection(url.host, url.port or 443, ssl=context), 10)
        except (OSError, TimeoutError, ValueError, httpx.InvalidURL) as err:
            _LOGGER.debug("Certificato di %s non letto: %r", self.host, err)
            raise VapixError(f"certificato non leggibile ({_network_reason(err)})") from err
        try:
            der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True) or b""
        finally:
            writer.close()
        digest = hashlib.sha256(der).hexdigest().upper()
        return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)) if der else ""

    async def fetch_events(self, start: str, limit: int = 1000, row_id: int | None = None) -> tuple[list[dict], bool]:
        """Registro eventi interno del controller (fino a 30.000), dal più vecchio; True se ce ne sono altri."""
        request: dict = {"Limit": limit, "Start": start}
        if row_id is not None:
            request["RowID"] = row_id
        data = await self._post("/vapix/eventlogger", {"axlog:FetchEvents": request})
        return data.get("Event") or [], bool(data.get("More"))

    async def latest_events(self, limit: int, before: int | None = None) -> list[dict]:
        """Ultimi eventi del registro interno, dal più recente; con before quelli prima di quella riga (rowid): al
        collaudo RowID con Descending va indietro e non comprende la riga indicata."""
        request: dict = {"Limit": limit, "Descending": True}
        if before is not None:
            request["RowID"] = before
        data = await self._post("/vapix/eventlogger", {"axlog:FetchEvents": request})
        return data.get("Event") or []

    async def io_ports(self) -> list[dict]:
        """Porte I/O con lo stato attuale: port, name, direction, state e normalState (open o closed)."""
        return (await self._cgi("/axis-cgi/io/portmanagement.cgi", "getPorts")).get("items") or []

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

    async def list_authentication_profiles(self) -> list[dict]:
        """Modi di identificazione dei punti di accesso (CardOnly, REXOnly... e quelli creati a mano)."""
        return await self._list_all("/vapix/pacs", "pacsaxis:GetAuthenticationProfileList", "AuthenticationProfile")

    async def set_authentication_profile(self, profile: dict) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:SetAuthenticationProfile": {"AuthenticationProfile": [profile]}})

    async def list_access_controllers(self) -> list[dict]:
        """Controllo accessi del controller: decide solo per i punti di accesso elencati nel suo AccessPoint.
        Ce n'è uno di fabbrica ("Axis-<serie> AccessController") e il reset lo rimette con l'elenco vuoto."""
        return await self._list_all(
            "/vapix/pacs", "pacsaxis:GetAccessControllerList", "AccessController", required=True
        )

    async def set_access_controller(self, access_controller: dict) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:SetAccessController": {"AccessController": [access_controller]}})

    # Fasce orarie

    async def list_schedules(self) -> list[dict]:
        return await self._list_all("/vapix/schedule", "axsch:GetScheduleList", "Schedule", required=True)

    async def set_schedule(self, schedule: dict) -> str:
        """Crea (token vuoto) o aggiorna una fascia oraria; restituisce il token assegnato."""
        data = await self._post("/vapix/schedule", {"axsch:SetSchedule": {"Schedule": [schedule]}})
        return _first_token(data, schedule)

    async def remove_schedule(self, token: str) -> None:
        await self._post("/vapix/schedule", {"axsch:RemoveSchedule": {"Token": [token]}})

    # Programmazione: porte, lettori (IdPoint) e punti di accesso

    async def list_doors(self) -> list[dict]:
        return await self._list_all("/vapix/doorcontrol", "axtdc:GetDoorList", "Door", required=True)

    async def get_doors(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/doorcontrol", {"axtdc:GetDoor": {"Token": tokens}})
        return data.get("Door", [])

    async def set_door(self, door: dict) -> str:
        data = await self._post("/vapix/doorcontrol", {"axtdc:SetDoor": {"Door": [door]}})
        return _first_token(data, door)

    async def list_door_configurations(self) -> list[dict]:
        return await self._list_all("/vapix/doorcontrol", "axtdc:GetDoorConfigurationList", "DoorConfiguration")

    async def set_door_configuration(self, configuration: dict) -> None:
        """Serratura, contatto porta e relè della porta. DeviceUUID vuoto = questo controller."""
        await self._post("/vapix/doorcontrol", {"axtdc:SetDoorConfiguration": {"DoorConfiguration": [configuration]}})

    async def remove_door(self, token: str) -> None:
        """Toglie la porta e la sua configurazione."""
        await self._post("/vapix/doorcontrol", {"axtdc:RemoveDoor": {"Token": [token]}})
        await self._remove_if_present("/vapix/doorcontrol", "axtdc:RemoveDoorConfiguration", token)

    async def _remove_if_present(self, path: str, method: str, token: str) -> None:
        """Toglie un oggetto, o la configurazione che va tolta con lui: se non c'è già più il controller risponde
        NotFound, e va bene così."""
        try:
            await self._post(path, {method: {"Token": [token]}})
        except VapixError as err:
            if "NotFound" not in err.fault:
                raise

    async def list_idpoints(self) -> list[dict]:
        return await self._list_all("/vapix/idpoint", "axtid:GetIdPointList", "IdPoint", required=True)

    async def get_idpoints(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/idpoint", {"axtid:GetIdPoint": {"Token": tokens}})
        return data.get("IdPoint", [])

    async def set_idpoint(self, idpoint: dict) -> None:
        try:
            await self._post("/vapix/idpoint", {"axtid:SetIdPoint": {"IdPoint": [idpoint]}})
        except VapixError:
            # Nella documentazione Axis il metodo compare anche come SetIdPointList
            await self._post("/vapix/idpoint", {"axtid:SetIdPointList": {"IdPoint": [idpoint]}})

    async def create_idpoint(self, idpoint: dict) -> str:
        """Crea un punto di identificazione (senza token) o lo aggiorna; restituisce il token."""
        data = await self._post("/vapix/idpoint", {"axtid:SetIdPoint": {"IdPoint": [idpoint]}})
        return _first_token(data, idpoint)

    async def set_idpoint_configuration(self, token: str, device_uuid: str, configuration: dict[str, str]) -> None:
        """Configurazione hardware di un punto di identificazione: il controller completa le chiavi mancanti."""
        item = {
            "token": token,
            "DeviceUUID": device_uuid,
            "Configuration": [{"Name": k, "Value": v} for k, v in configuration.items()],
        }
        await self._post("/vapix/idpoint", {"axtid:SetIdPointConfiguration": {"IdPointConfiguration": [item]}})

    async def remove_idpoint(self, token: str) -> None:
        """Toglie l'IdPoint e la sua configurazione, che il controller altrimenti tiene orfana."""
        await self._remove_if_present("/vapix/idpoint", "axtid:RemoveIdPoint", token)
        await self._remove_if_present("/vapix/idpoint", "axtid:RemoveIdPointConfiguration", token)

    async def set_io_assignment(self, io: str, mode: str, users: list[dict]) -> None:
        """Assegna un I/O fisico (lista vuota = libera). Il controller risponde 200 anche se rifiuta: si controlla."""
        data = await self._post(
            "/vapix/pin",
            {"axisio:SetIoAssignment": {"IoAssignment": [{"IoName": io, "IoMode": mode, "IoUser": users}]}},
        )
        errors = [str(e.get("Error") or "errore") for e in data.get("IoAssignmentError") or []]
        if errors:
            _LOGGER.info("Il controller ha rifiutato l'assegnazione di %s (%s): %s", io, mode, ", ".join(errors))
            raise _refused("l'assegnazione dell'ingresso", ", ".join(errors))

    async def create_access_point(self, access_point: dict) -> str:
        data = await self._post("/vapix/pacs", {"pacsaxis:SetAccessPoint": {"AccessPoint": [access_point]}})
        return _first_token(data, access_point)

    async def remove_access_point(self, token: str) -> None:
        await self._remove_if_present("/vapix/pacs", "pacsaxis:RemoveAccessPoint", token)

    async def list_io(self) -> tuple[list[dict], list[dict]]:
        """I/O fisici del controller e a chi sono assegnati. Il servizio axisio sull'A1210 risponde su /vapix/pin."""
        info = await self._post("/vapix/pin", {"axisio:GetIoInfoList": {}})
        assigned = await self._post("/vapix/pin", {"axisio:GetIoAssignmentList": {}})
        return info.get("IoInfo", []), assigned.get("IoAssignment", [])

    async def list_idpoint_configurations(self) -> list[dict]:
        return await self._list_all("/vapix/idpoint", "axtid:GetIdPointConfigurationList", "IdPointConfiguration")

    async def get_access_points(self, tokens: list[str]) -> list[dict]:
        data = await self._post("/vapix/pacs", {"pacsaxis:GetAccessPoint": {"Token": tokens}})
        return data.get("AccessPoint", [])

    async def set_access_point(self, access_point: dict) -> None:
        await self._post("/vapix/pacs", {"pacsaxis:SetAccessPoint": {"AccessPoint": [access_point]}})

    async def door_state(self, door_token: str) -> dict:
        """Stato attuale della porta: modalità (DoorMode: Locked, Accessed...) e, solo con il contatto porta
        configurato, contatto (DoorPhysicalState: Open, Closed) e allarme (Alarm: Normal, DoorForcedOpen...)."""
        data = await self._post("/vapix/doorcontrol", {"tdc:GetDoorState": {"Token": door_token}})
        return data.get("DoorState") or {}

    async def door_mode(self, door_token: str) -> str | None:
        """Modalità attuale della porta (Locked, Accessed, Unlocked...)."""
        return (await self.door_state(door_token)).get("DoorMode")

    async def access_door(self, door_token: str, extended: bool = False) -> None:
        """Sblocco temporaneo. Mai UnlockDoor: la serratura non regge l'alimentazione continua."""
        request: dict = {"Token": door_token}
        if extended:
            request["UseExtendedTime"] = True
        await self._post("/vapix/doorcontrol", {"tdc:AccessDoor": request})


# Metodi che cambiano la configurazione del controller: sospesi se il controller all'indirizzo è cambiato
WRITE_PREFIXES = ("set_", "remove_", "enable_", "disable_", "create_")


class ControllerGuard:
    """Il client del controller con le scritture sospendibili; letture e aperture della porta passano sempre."""

    def __init__(self, client) -> None:
        self.client = client
        # Motivo della sospensione mostrato all'utente; vuoto = scritture permesse
        self.paused = ""

    def __getattr__(self, name: str):
        attr = getattr(self.client, name)
        if name.startswith(WRITE_PREFIXES) and callable(attr):

            async def guarded(*args, **kwargs):
                if self.paused:
                    raise VapixError(f"scritture sospese: {self.paused}")
                return await attr(*args, **kwargs)

            return guarded
        return attr
