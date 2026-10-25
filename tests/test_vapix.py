import json

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

    with pytest.raises(VapixError, match="errore 400"):
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
