from datetime import UTC, datetime

import pytest

from app.events import Event, EventMonitor
from app.service import AccessService, ServiceError, normalize_card, user_token_for
from app.vapix import VapixError


@pytest.fixture
def service(db, controller, settings):
    return AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))


def event(topic: str, data: dict) -> Event:
    return Event(category=topic.split("/")[0], topic=topic, timestamp=datetime(2026, 10, 25, tzinfo=UTC), data=data)


def test_normalize_card():
    assert normalize_card(" 04:A1 b2 ") == "04A1b2"
    with pytest.raises(ServiceError):
        normalize_card("zz12")


def test_user_token_follows_ha_convention():
    assert user_token_for("Mario Rossì", datetime(2026, 10, 25, 1, 2, 3)) == "utente_mario_rossi_261025010203"


async def test_sync_imports_existing_cards(service, controller, db):
    controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    controller.add_existing("utente_mario", "Mario Rossi", "04bb", enabled=False)
    controller.add_existing("utente_anna", "Anna Bianchi", "04cc")

    result = await service.sync_from_controller()

    assert result["cards"] == 3
    persons = db.list_persons()
    assert [p["name"] for p in persons] == ["Anna Bianchi", "Mario Rossi"]
    assert [c["enabled"] for c in persons[1]["cards"]] == [True, False]


async def test_sync_removes_cards_deleted_on_controller(service, controller, db):
    token = controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    await service.sync_from_controller()
    controller.credentials.pop(token)

    result = await service.sync_from_controller()

    assert result["removed"] == 1
    assert db.list_persons() == []


async def test_create_person_writes_user_and_card(service, controller, db):
    person = await service.create_person("Mario Rossi", "04AA", "admin")

    assert person["name"] == "Mario Rossi"
    credential = next(iter(controller.credentials.values()))
    assert credential["IdData"] == [{"Name": "Card", "Value": "04AA"}]
    assert credential["CredentialAccessProfile"][0]["AccessProfile"] == "profilo_sempre"
    assert credential["UserToken"] in controller.users
    assert db.list_audit()[0]["user"] == "admin"


async def test_create_person_rejects_card_already_registered(service):
    await service.create_person("Mario Rossi", "04aa", "admin")
    with pytest.raises(ServiceError, match="già registrata a nome Mario Rossi"):
        await service.create_person("Anna", "04AA", "admin")


async def test_create_person_rolls_back_user_on_card_error(service, controller, db):
    controller.fail_set_credential = True
    with pytest.raises(VapixError):
        await service.create_person("Mario Rossi", "04aa", "admin")
    assert controller.users == {}
    assert db.list_persons() == []


async def test_create_person_clears_unknown_card(service, db):
    db.note_unknown_card("04aa", "2026-10-25T00:00:00+00:00")
    await service.create_person("Mario Rossi", "04AA", "admin")
    assert db.list_unknown_cards() == []


async def test_rename_updates_user_and_credentials(service, controller):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    await service.rename_person(person["id"], "Mario Rossi Jr", "admin")

    assert controller.users[person["axis_user_token"]]["Name"] == "Mario Rossi Jr"
    assert next(iter(controller.credentials.values()))["Description"] == "Mario Rossi Jr"


async def test_suspend_and_enable_card(service, controller):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    card = person["cards"][0]

    updated = await service.set_card_enabled(card["id"], False, "admin")
    assert updated["cards"][0]["enabled"] is False
    assert controller.credentials[card["axis_credential_token"]]["Enabled"] is False

    await service.set_card_enabled(card["id"], True, "admin")
    assert controller.credentials[card["axis_credential_token"]]["Enabled"] is True


async def test_deleting_last_card_removes_person(service, controller, db):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    person = await service.add_card(person["id"], "04bb", "admin")

    remaining = await service.delete_card(person["cards"][0]["id"], "admin")
    assert [c["number"] for c in remaining["cards"]] == ["04bb"]

    assert await service.delete_card(remaining["cards"][0]["id"], "admin") is None
    assert db.list_persons() == []
    assert controller.users == {}


async def test_delete_person_removes_everything(service, controller, db):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    await service.add_card(person["id"], "04bb", "admin")

    await service.delete_person(person["id"], "admin")

    assert controller.credentials == {}
    assert controller.users == {}
    assert db.list_persons() == []


async def test_open_door_only_when_locked(service, controller, db):
    service.monitor.door_mode = "Accessed"
    with pytest.raises(ServiceError, match="non è bloccata"):
        await service.open_door(False, "admin")
    assert not [c for c in controller.calls if c[0] == "access_door"]

    service.monitor.door_mode = "Locked"
    await service.open_door(True, "admin")
    assert ("access_door", "porta", True) in controller.calls
    assert db.list_events()[0]["kind"] == "apertura_remota"


async def test_open_door_asks_controller_when_state_unknown(service, controller):
    controller.door = "Unlocked"
    with pytest.raises(ServiceError, match="Unlocked"):
        await service.open_door(False, "admin")


async def test_card_read_events(service, db):
    await service.create_person("Mario Rossi", "04AA", "admin")

    service.record_event(event("IdPoint/Request/IdData/x", {"Card": "04aa"}))
    service.record_event(event("IdPoint/Request/IdData/x", {"Card": "05ff"}))
    service.record_event(event("IdPoint/Request/IdData/x", {"Card": "05ff"}))

    events = db.list_events()
    assert [e["kind"] for e in events] == ["sconosciuta", "sconosciuta", "lettura"]
    assert events[2]["person_name"] == "Mario Rossi"
    assert db.list_unknown_cards()[0]["count"] == 2


async def test_access_control_events(service, db):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    token = person["cards"][0]["axis_credential_token"]

    service.record_event(event("AccessControl/AccessGranted/Credential/x", {"CredentialToken": token}))
    service.record_event(
        event("AccessControl/Denied/Credential/x", {"CredentialToken": token, "Reason": "CredentialExpired"})
    )

    denied, granted = db.list_events()[:2]
    assert granted["kind"] == "concesso" and granted["person_name"] == "Mario Rossi"
    assert denied["kind"] == "negato" and denied["detail"] == "tessera scaduta"


def test_retained_and_irrelevant_events_are_not_logged(service, db):
    service.record_event(event("IdPoint/Request/IdData/x", {"Card": "05ff"}), retained=True)
    service.record_event(event("Door/State/DoorAlarm/x", {"State": "Normal"}))
    service.record_event(event("Door/State/DoorMode/x", {"State": "Locked"}))
    assert db.list_events() == []

    service.record_event(event("Door/State/DoorAlarm/x", {"State": "DoorForcedOpen"}))
    assert db.list_events()[0]["detail"] == "porta forzata"


async def test_sync_uses_card_description_when_users_unavailable(service, controller, db):
    controller.add_existing("utente_mario", "Mario Rossi", "04aa")

    async def broken():
        raise VapixError("non supportato")

    controller.list_users = broken
    await service.sync_from_controller()
    assert db.list_persons()[0]["name"] == "Mario Rossi"
