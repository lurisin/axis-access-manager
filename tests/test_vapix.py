import asyncio
import json
import socket

import httpx
import pytest

from app.vapix import CREDENTIAL_PAGE_SIZE, A1210Client, VapixError


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
