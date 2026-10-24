import httpx

from app.config import MqttSettings, parse_options, resolve_mqtt

BASE = {
    "a1210_host": " 172.27.202.22 ",
    "a1210_username": "addon",
    "a1210_password": "segreta",
    "a1210_verify_ssl": False,
    "door_token": "porta_via_consulta_ingresso_scala",
    "mqtt_topic_prefix": "accessi/via_consulta_controller/",
    "log_level": "info",
}


def test_parse_options_trims_values():
    settings = parse_options(BASE)
    assert settings.a1210_host == "172.27.202.22"
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
