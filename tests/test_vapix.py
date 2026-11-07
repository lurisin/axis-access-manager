import asyncio
import json
import socket

import httpx
import pytest

from app.vapix import CREDENTIAL_PAGE_SIZE, A1210Client, ControllerUnreachable, VapixError


def client_with(handler) -> A1210Client:
    return A1210Client("a1210.local", "u", "p", transport=httpx.MockTransport(handler))


async def test_device_info():
    def handler(request):
        assert request.url.path == "/axis-cgi/basicdeviceinfo.cgi"
        return httpx.Response(200, json={"data": {"propertyList": {"ProdNbr": "A1210", "Version": "10.12.288.1"}}})

    info = await client_with(handler).device_info()
    assert info["Version"] == "10.12.288.1"


async def test_list_credentials_reads_all_pages():
    requests = []

    def handler(request):
        body = json.loads(request.content)["pacsaxis:GetCredentialList"]
        requests.append(body)
        if "StartReference" not in body:
            return httpx.Response(200, json={"Credential": [{"token": "a"}, {"token": "b"}], "NextStartReference": "2"})
        return httpx.Response(200, json={"Credential": [{"token": "c"}]})

    credentials = await client_with(handler).list_credentials()
    assert [c["token"] for c in credentials] == ["a", "b", "c"]
    assert requests == [{"Limit": CREDENTIAL_PAGE_SIZE}, {"Limit": CREDENTIAL_PAGE_SIZE, "StartReference": "2"}]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(401), "credenziali"),
        (httpx.Response(500), "errore 500"),
        (httpx.Response(200, text="non json"), "non riconosciuta"),
    ],
)
async def test_errors_are_reported(response, message):
    with pytest.raises(VapixError, match=message):
        await client_with(lambda request: response).door_info_list()


async def test_unreachable_controller():
    def handler(request):
        raise httpx.ConnectError("timeout")

    with pytest.raises(VapixError, match="non risponde"):
        await client_with(handler).device_info()


@pytest.fixture
def no_proxy(monkeypatch):
    """Collegamenti veri in locale, come nell'add-on: senza il proxy dell'ambiente di prova."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


async def test_network_errors_are_explained_from_the_real_cause(no_proxy, monkeypatch):
    """httpx avvolge il nome non risolto e l'errore TLS in ConnectError: la spiegazione viene dalla causa vera."""

    async def plain_http(reader, writer):
        # Server HTTP in chiaro dove ci si aspetta HTTPS
        await reader.read(1024)
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(plain_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host == "a1210.invalid":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    try:
        for host, reason in (
            (f"127.0.0.1:{port}", "collegamento HTTPS non riuscito"),
            ("a1210.invalid", "indirizzo non trovato"),
        ):
            client = A1210Client(host, "u", "p")
            for call in (client.device_info, client.ws_session):
                with pytest.raises(VapixError, match=f"non risponde \\({reason}\\)"):
                    await call()
            await client.aclose()
    finally:
        server.close()


async def test_settings_are_written_with_the_newest_api_version():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if body["method"] == "getSupportedVersions":
            return httpx.Response(200, json={"data": {"apiVersions": ["1.0", "1.5", "1.10"]}})
        return httpx.Response(200, json={"apiVersion": body["apiVersion"], "data": {}})

    await client_with(handler).set_mqtt_client({"server": {"host": "10.0.0.2", "port": 1883}, "password": "x"})
    writes = [(path, body) for path, body in requests if body["method"] != "getSupportedVersions"]
    assert [(b["method"], b["apiVersion"]) for _, b in writes] == [
        ("configureClient", "1.10"),
        ("activateClient", "1.10"),
    ]
    assert writes[0][1]["params"]["server"]["host"] == "10.0.0.2"


async def test_old_firmware_without_versions_list_gets_1_0():
    def handler(request):
        body = json.loads(request.content)
        if body["method"] == "getSupportedVersions":
            return httpx.Response(200, json={"error": {"code": 2002, "message": "Method not supported"}})
        assert body == {"apiVersion": "1.0", "method": "setTimeZone", "params": {"timeZone": "Europe/Rome"}}
        return httpx.Response(200, json={"data": {}})

    await client_with(handler).set_time_zone("Europe/Rome")


async def test_removing_an_object_without_configuration_is_fine():
    def handler(request):
        method = next(iter(json.loads(request.content)))
        if method.endswith("Configuration"):
            return httpx.Response(400, json={"Fault": "env:Sender", "FaultCode": "ter:NotFound"})
        return httpx.Response(200, json={})

    await client_with(handler).remove_idpoint("lettore-x")
    await client_with(handler).remove_door("porta-x")

    def refused(request):
        return httpx.Response(400, json={"Fault": "env:Sender", "FaultCode": "ter:InvalidArgVal"})

    with pytest.raises(VapixError, match=r"rifiutato la richiesta \(valore non accettato\)"):
        await client_with(refused).remove_idpoint("lettore-x")


async def test_door_and_its_configuration():
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"Token": ["porta"]} if "axtdc:SetDoor" in body else {})

    client = client_with(handler)
    assert await client.set_door({"token": "porta", "Name": "Scala"}) == "porta"
    config = {"token": "porta", "DeviceUUID": "", "Configuration": [{"Name": "Lock.Type", "Value": "Relay"}]}
    await client.set_door_configuration(config)
    assert requests[1] == {"axtdc:SetDoorConfiguration": {"DoorConfiguration": [config]}}


async def test_access_controller_read_and_written_as_on_the_bench():
    requests = []
    factory = {
        "token": "Axis-b8a44fe6a074 AccessController",
        "Name": "Axis-b8a44fe6a074 AccessController",
        "Description": "",
        "AccessPoint": [],
    }

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(
            200, json={"AccessController": [factory]} if "pacsaxis:GetAccessControllerList" in body else {}
        )

    client = client_with(handler)
    assert await client.list_access_controllers() == [factory]
    await client.set_access_controller({**factory, "AccessPoint": ["accesso_scala"]})
    assert requests[1] == (
        "/vapix/pacs",
        {"pacsaxis:SetAccessController": {"AccessController": [{**factory, "AccessPoint": ["accesso_scala"]}]}},
    )


@pytest.mark.parametrize(
    "lost",
    [httpx.ReadTimeout("risposta persa"), httpx.RemoteProtocolError("collegamento caduto")],
)
async def test_reader_write_without_an_answer_is_not_sent_again(lost):
    sent = []

    def handler(request):
        sent.append(next(iter(json.loads(request.content))))
        raise lost

    # Senza risposta la scrittura può essere arrivata: con l'altro nome del metodo sarebbe fatta due volte
    with pytest.raises(ControllerUnreachable):
        await client_with(handler).set_idpoint({"token": "lettore-1", "Name": "Lettore esterno"})
    assert sent == ["axtid:SetIdPoint"]


async def test_reader_write_refused_is_tried_with_the_other_method_name():
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "axtid:SetIdPoint" in body:
            return httpx.Response(400, json={"Fault": "env:Sender", "FaultCode": "ter:ActionNotSupported"})
        return httpx.Response(200, json={})

    reader = {"token": "lettore-1", "Name": "Lettore esterno"}
    await client_with(handler).set_idpoint(reader)
    assert sent == [{"axtid:SetIdPoint": {"IdPoint": [reader]}}, {"axtid:SetIdPointList": {"IdPoint": [reader]}}]


# Ogni remove_* con le richieste che manda: path e metodi, nell'ordine
REMOVALS = {
    "remove_user": ("/vapix/pacs", ["axudb:RemoveUser"]),
    "remove_credential": ("/vapix/pacs", ["pacsaxis:RemoveCredential"]),
    "remove_access_profile": ("/vapix/pacs", ["pacsaxis:RemoveAccessProfile"]),
    "remove_schedule": ("/vapix/schedule", ["axsch:RemoveSchedule"]),
    "remove_door": ("/vapix/doorcontrol", ["axtdc:RemoveDoor", "axtdc:RemoveDoorConfiguration"]),
    "remove_idpoint": ("/vapix/idpoint", ["axtid:RemoveIdPoint", "axtid:RemoveIdPointConfiguration"]),
    "remove_access_point": ("/vapix/pacs", ["pacsaxis:RemoveAccessPoint"]),
}


def test_every_removal_is_listed():
    assert {name for name in dir(A1210Client) if name.startswith("remove_")} == set(REMOVALS)


@pytest.mark.parametrize(
    "fault",
    [
        {"Fault": "env:Sender", "FaultCode": "ter:NotFound"},
        {"Fault": "env:Sender", "FaultCode": "ter:InvalidArgVal", "Subcode": "ter:NotFound"},
    ],
)
@pytest.mark.parametrize("name", sorted(REMOVALS))
async def test_removing_what_is_not_there_anymore_is_done(name, fault):
    path, methods = REMOVALS[name]
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        return httpx.Response(400, json=fault)

    # Tolto a mano dal sito Axis, o tolto da un tentativo di prima con la risposta persa: è già come si voleva
    await getattr(client_with(handler), name)("oggetto-x")
    assert sent == [(path, {method: {"Token": ["oggetto-x"]}}) for method in methods]


@pytest.mark.parametrize("name", sorted(REMOVALS))
async def test_removal_refused_or_without_an_answer_is_still_an_error(name):
    def in_use(request):
        return httpx.Response(400, json={"Fault": "env:Sender", "FaultCode": "ter:ReferenceInUse"})

    with pytest.raises(VapixError, match=r"rifiutato la richiesta \(oggetto ancora in uso\)"):
        await getattr(client_with(in_use), name)("oggetto-x")

    def lost(request):
        raise httpx.ReadTimeout("risposta persa")

    with pytest.raises(ControllerUnreachable):
        await getattr(client_with(lost), name)("oggetto-x")


# Elenchi che con strict distinguono «non letto» da «vuoto»: metodo e chiave dell'elenco
STRICT_LISTS = {
    "list_users": ("axudb:GetUserList", "User"),
    "list_access_points": ("pacsaxis:GetAccessPointList", "AccessPoint"),
    "list_authentication_profiles": ("pacsaxis:GetAuthenticationProfileList", "AuthenticationProfile"),
    "list_door_configurations": ("axtdc:GetDoorConfigurationList", "DoorConfiguration"),
    "list_idpoint_configurations": ("axtid:GetIdPointConfigurationList", "IdPointConfiguration"),
}


@pytest.mark.parametrize("name", sorted(STRICT_LISTS))
async def test_strict_list_without_its_key_is_not_an_empty_list(name):
    method, key = STRICT_LISTS[name]

    def without_key(request):
        assert method in json.loads(request.content)
        return httpx.Response(200, json={})

    # Come oggi senza strict: vuoto
    assert await getattr(client_with(without_key), name)() == []
    with pytest.raises(VapixError, match="risposta del controller non riconosciuta"):
        await getattr(client_with(without_key), name)(strict=True)

    def empty(request):
        return httpx.Response(200, json={key: []})

    assert await getattr(client_with(empty), name)(strict=True) == []

    def second_page_without_key(request):
        body = json.loads(request.content)[method]
        if "StartReference" not in body:
            return httpx.Response(200, json={key: [{"token": "a"}], "NextStartReference": "1"})
        return httpx.Response(200, json={})

    assert await getattr(client_with(second_page_without_key), name)() == [{"token": "a"}]
    with pytest.raises(VapixError, match="risposta del controller non riconosciuta"):
        await getattr(client_with(second_page_without_key), name)(strict=True)

    def null(request):
        return httpx.Response(200, content=b"null", headers={"Content-Type": "application/json"})

    # Non un errore di Python: chi legge gestisce solo gli errori del controller
    with pytest.raises(VapixError, match="risposta del controller non riconosciuta"):
        await getattr(client_with(null), name)(strict=True)
