from datetime import UTC, datetime, timedelta

import httpx

from app.ha import HomeAssistant

NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


def _ha(states):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=states)

    ha = HomeAssistant(token="x")
    ha._client = httpx.AsyncClient(base_url="http://ha/api", transport=httpx.MockTransport(handler))
    return ha


def _button(entity_id, at, user_id=None, command="apri"):
    attributes = {"comando": command} if command else {}
    return {"entity_id": entity_id, "state": at.isoformat(), "attributes": attributes, "context": {"user_id": user_id}}


PERSON = {"entity_id": "person.luca", "state": "home", "attributes": {"user_id": "u1", "friendly_name": "Luca"}}


async def test_button_presser_names_the_person():
    states = [
        _button("button.apri_porta", NOW - timedelta(seconds=2), "u1"),
        PERSON,
        _button("button.altro", NOW - timedelta(hours=1), "u2", command=None),
    ]
    assert await _ha(states).button_presser("apri", NOW) == "Luca"


async def test_button_presser_without_user_or_press():
    # Automazione: pressione recente senza utente
    assert await _ha([_button("button.apri_porta", NOW, None), PERSON]).button_presser("apri", NOW) == ""
    # Nessun pulsante premuto di recente, o pulsante che non è dell'add-on: non si sa
    stale = [_button("button.apri_porta", NOW - timedelta(minutes=5), "u1")]
    assert await _ha(stale).button_presser("apri", NOW) is None
    assert await _ha([_button("button.zigbee", NOW, "u1", command=None), PERSON]).button_presser("apri", NOW) is None
    # Utente senza persona collegata
    assert await _ha([_button("button.apri_porta", NOW, "u9"), PERSON]).button_presser("apri", NOW) is None
    assert await HomeAssistant(token="").button_presser("apri", NOW) is None


async def test_button_presser_looks_only_at_the_button_of_the_command():
    # Un altro pulsante premuto subito dopo non cambia chi ha aperto
    states = [
        _button("button.apri_porta", NOW - timedelta(seconds=2), "u1"),
        _button("button.apri_cantina", NOW - timedelta(seconds=1), None, command="apri:porta2"),
        PERSON,
    ]
    assert await _ha(states).button_presser("apri", NOW) == "Luca"
    # Comando pubblicato su MQTT senza premere quel pulsante: non si attribuisce a chi ha premuto l'altro
    assert await _ha(states[:1] + [PERSON]).button_presser("apri_lungo", NOW) is None
    assert await _ha(states).button_presser("", NOW) is None
