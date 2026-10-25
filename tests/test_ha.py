from datetime import UTC, datetime, timedelta

import httpx

from app.ha import HomeAssistant

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def _ha(states, buttons="button.apri_porta,button.altro"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/template"):
            return httpx.Response(200, text=buttons)
        return httpx.Response(200, json=states)

    ha = HomeAssistant(token="x")
    ha._client = httpx.AsyncClient(base_url="http://ha/api", transport=httpx.MockTransport(handler))
    return ha


def _button(entity_id, at, user_id=None):
    return {"entity_id": entity_id, "state": at.isoformat(), "context": {"user_id": user_id}}


PERSON = {"entity_id": "person.luca", "state": "home", "attributes": {"user_id": "u1", "friendly_name": "Luca"}}


async def test_button_presser_names_the_person():
    states = [
        _button("button.apri_porta", NOW - timedelta(seconds=2), "u1"),
        PERSON,
        _button("button.altro", NOW - timedelta(hours=1), "u2"),
    ]
    assert await _ha(states).button_presser(NOW) == "Luca"


async def test_button_presser_without_user_or_press():
    # Automazione: pressione recente senza utente
    assert await _ha([_button("button.apri_porta", NOW, None), PERSON]).button_presser(NOW) == ""
    # Nessun pulsante premuto di recente, o pulsante che non è MQTT: non si sa
    assert await _ha([_button("button.apri_porta", NOW - timedelta(minutes=5), "u1")]).button_presser(NOW) is None
    assert await _ha([_button("button.zigbee", NOW, "u1"), PERSON]).button_presser(NOW) is None
    # Utente senza persona collegata
    assert await _ha([_button("button.apri_porta", NOW, "u9"), PERSON]).button_presser(NOW) is None
    assert await HomeAssistant(token="").button_presser(NOW) is None
