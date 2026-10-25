import httpx
import pytest

from app.config import MqttSettings, normalize_host, parse_options, resolve_mqtt, supervisor_token

BASE = {
    "a1210_host": " 192.168.1.50 ",
    "a1210_username": "addon",
    "a1210_password": "segreta",
    "a1210_verify_ssl": False,
    "door_token": "porta_via_consulta_ingresso_scala",
    "mqtt_topic_prefix": "accessi/via_consulta_controller/",
    "log_level": "info",
}


def test_parse_options_trims_values():
    settings = parse_options(BASE)
    assert settings.a1210_host == "192.168.1.50"
    assert settings.mqtt_topic_prefix == "accessi/via_consulta_controller"
    assert settings.mqtt is None


def test_parse_options_with_explicit_broker():
    settings = parse_options({**BASE, "mqtt_host": "192.168.1.10", "mqtt_port": 1884, "mqtt_username": "u"})
    assert settings.mqtt == MqttSettings(host="192.168.1.10", port=1884, username="u", password=None)


async def test_resolve_mqtt_from_supervisor(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(
            200, json={"data": {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "pw"}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        mqtt = await resolve_mqtt(parse_options(BASE), client)
    assert mqtt == MqttSettings(host="core-mosquitto", port=1883, username="addons", password="pw")


async def test_resolve_mqtt_without_supervisor(monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    assert await resolve_mqtt(parse_options(BASE)) is None


@pytest.mark.parametrize(
    "value",
    [
        "192.168.1.50",
        " 192.168.1.50 ",
        "http://192.168.1.50",
        "https://192.168.1.50/",
        "HTTPS://192.168.1.50/index.html",
    ],
)
def test_normalize_host(value):
    assert normalize_host(value) == "192.168.1.50"


def test_supervisor_token_from_s6_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    (tmp_path / "SUPERVISOR_TOKEN").write_text("tok-s6\n")
    assert supervisor_token(tmp_path) == "tok-s6"
    assert supervisor_token(tmp_path / "manca") is None


async def test_resolve_mqtt_when_supervisor_has_no_broker(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"result": "error", "message": "Service not enabled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await resolve_mqtt(parse_options(BASE), client, token="tok") is None
    assert "Mosquitto" in caplog.text
