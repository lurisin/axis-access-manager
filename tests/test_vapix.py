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
