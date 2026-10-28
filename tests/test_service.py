import asyncio
import dataclasses
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
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


async def test_sync_marks_cards_deleted_on_controller(service, controller, db):
    token = controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    await service.sync_from_controller()
    person = db.list_persons()[0]
    db.update_person_details(person["id"], "Ditta Rossi", "chiavi del magazzino")
    controller.credentials.pop(token)

    result = await service.sync_from_controller()

    # Mai cancellate da sole: la persona resta con i dati che conosce solo l'app
    assert result["removed"] == 0 and result["missing"] == 1 and result["new_missing"] == 1
    assert result["mass_missing"] is True
    [person] = db.list_persons()
    assert person["company"] == "Ditta Rossi"
    assert person["cards"][0]["missing_since"]
    assert (await service.sync_from_controller())["new_missing"] == 0


async def test_mass_missing_threshold():
    from app.service import mass_missing

    assert mass_missing(50, 50) is True
    assert mass_missing(50, 1) is False
    assert mass_missing(50, 5) is False
    assert mass_missing(50, 6) is True
    assert mass_missing(2, 1) is False
    assert mass_missing(0, 0) is False


async def test_missing_card_comes_back(service, controller, db):
    token = controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    await service.sync_from_controller()
    saved = controller.credentials.pop(token)
    await service.sync_from_controller()

    controller.credentials[token] = saved
    await service.sync_from_controller()

    assert db.missing_cards() == []


async def test_restore_missing_rewrites_card_and_user(service, controller, db):
    person = await service.create_person("Mario Rossi", "04AA", "admin", group="profilo_sempre")
    await service.set_card_enabled(person["cards"][0]["id"], False, "admin")
    # Controller resettato: niente utenti né tessere
    controller.credentials.clear()
    controller.users.clear()
    await service.sync_from_controller()

    result = await service.restore_missing("admin")

    assert result == {"restored": 1, "failed": []}
    [credential] = controller.credentials.values()
    assert credential["IdData"] == [{"Name": "Card", "Value": "04AA"}]
    assert credential["Enabled"] is False
    assert credential["CredentialAccessProfile"][0]["AccessProfile"] == "profilo_sempre"
    assert person["axis_user_token"] in controller.users
    [card] = db.get_person(person["id"])["cards"]
    assert card["axis_credential_token"] == credential["token"] and not card["missing_since"]
    assert (await service.sync_from_controller())["missing"] == 0


async def test_restore_missing_reports_failures(service, controller, db):
    await service.create_person("Mario Rossi", "04AA", "admin", group="profilo_sempre")
    controller.credentials.clear()
    await service.sync_from_controller()
    controller.fail_set_credential = True

    result = await service.restore_missing("admin")

    assert result["restored"] == 0
    assert result["failed"][0]["card"] == "04AA" and "errore finto" in result["failed"][0]["error"]
    assert len(db.missing_cards()) == 1


async def test_forget_missing_and_delete_missing_card(service, controller, db):
    anna = await service.create_person("Anna Bianchi", "01", "admin", group="profilo_sempre")
    mario = await service.create_person("Mario Rossi", "02", "admin", group="profilo_sempre")
    controller.credentials.clear()
    await service.sync_from_controller()

    # Tessera non presente: si elimina senza chiederla al controller
    controller.calls.clear()
    await service.delete_card(anna["cards"][0]["id"], "admin")
    assert not [c for c in controller.calls if c[0] == "remove_credential"]

    assert service.forget_missing("admin") == {"cards": 1, "persons": 1}
    assert db.get_person(mario["id"]) is None
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
    with pytest.raises(ServiceError, match=r"\(Sbloccata\)"):
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

    late = event("AccessControl/Denied/Credential/y", {"CredentialToken": token, "Reason": "NotPermittedAtThisTime"})
    service.record_event(dataclasses.replace(late, timestamp=datetime(2026, 10, 25, 23, tzinfo=UTC)))
    assert db.list_events()[0]["detail"] == "fuori fascia oraria"


async def test_same_denial_on_two_topics_is_logged_once(service, db):
    """Il controller manda lo stesso diniego su più topic: una riga sola nel registro."""
    data = {"AccessPointToken": "ap-in", "IdData": "c011a0d0", "Reason": "CredentialNotFound"}
    service.record_event(event("IdPoint/Request/IdData/x", {"Card": "c011a0d0"}))
    service.record_event(event("AccessControl/Denied/CredentialNotFound/Card/x", data))
    service.record_event(event("AccessControl/Denied/Credential/x", data))
    assert [e["kind"] for e in db.list_events()] == ["negato", "sconosciuta"]
    assert db.count_events("")["negato"] == 1


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


async def test_create_person_with_group_and_validity(service, controller, db):
    person = await service.create_person(
        "Ospite",
        "04aa",
        "admin",
        group="p2",
        valid_from="2026-10-25T08:00:00Z",
        valid_to="2026-10-26T08:00:00+00:00",
        company="Ditta",
        notes="badge visitatore",
    )

    credential = next(iter(controller.credentials.values()))
    assert credential["CredentialAccessProfile"] == [{"AccessProfile": "p2", "ValidFrom": "", "ValidTo": ""}]
    assert credential["ValidFrom"] == "2026-10-25T08:00:00Z"
    assert credential["ValidTo"] == "2026-10-26T08:00:00Z"
    assert person["company"] == "Ditta" and person["notes"] == "badge visitatore"
    assert person["cards"][0]["access_profile"] == "p2"


async def test_validity_must_end_after_start(service):
    with pytest.raises(ServiceError, match="dopo l'inizio"):
        await service.create_person(
            "X", "04aa", "admin", valid_from="2026-10-26T00:00:00Z", valid_to="2026-10-25T00:00:00Z"
        )


async def test_new_card_inherits_access(service, controller):
    person = await service.create_person("Ospite", "04aa", "admin", group="p2", valid_to="2026-12-31T00:00:00Z")
    person = await service.add_card(person["id"], "04bb", "admin")
    second = controller.credentials[person["cards"][1]["axis_credential_token"]]
    assert second["CredentialAccessProfile"][0]["AccessProfile"] == "p2"
    assert second["ValidTo"] == "2026-12-31T00:00:00Z"


async def test_set_person_access_updates_every_card(service, controller, db):
    person = await service.create_person("Mario", "04aa", "admin")
    await service.add_card(person["id"], "04bb", "admin")

    person = await service.set_person_access(person["id"], "p2", "", "2027-01-01T00:00:00Z", "admin")

    for card in person["cards"]:
        credential = controller.credentials[card["axis_credential_token"]]
        assert credential["CredentialAccessProfile"][0]["AccessProfile"] == "p2"
        assert credential["ValidTo"] == "2027-01-01T00:00:00Z"
        assert card["access_profile"] == "p2"
    assert db.list_audit()[0]["action"] == "accesso modificato"


async def test_sync_reads_group_and_validity(service, controller, db):
    token = controller.add_existing("utente_mario", "Mario", "04aa")
    controller.credentials[token]["ValidTo"] = "2027-01-01T00:00:00Z"
    await service.sync_from_controller()
    card = db.list_persons()[0]["cards"][0]
    assert card["access_profile"] == "profilo_sempre"
    assert card["valid_to"] == "2027-01-01T00:00:00Z"


async def test_update_person_details_only_in_addon(service, controller, db):
    person = await service.create_person("Mario", "04aa", "admin")
    calls = len(controller.calls)
    person = await service.update_person(person["id"], "admin", company="Studio", notes="note")
    assert person["company"] == "Studio"
    assert len(controller.calls) == calls
    person = await service.update_person(person["id"], "admin", name="Mario Rossi")
    assert controller.users[person["axis_user_token"]]["Name"] == "Mario Rossi"


async def test_log_event_notifies_listeners(service):
    seen = []

    async def after(row):
        seen.append(row["kind"])

    service.after_event = after
    service.record_intercom("sensor.citofono", "Citofono")
    await asyncio.sleep(0)
    assert seen == ["citofono"]


async def test_diagnostic_events_update_home_assistant(service, db):
    changes = []

    async def on_change():
        changes.append(True)

    service.on_change = on_change
    # Anche il valore conservato dal broker aggiorna i sensori, ma non va nel registro
    service.record_event(event("Device/IO/Port/$source/port/0", {"state": "1"}), retained=True)
    service.record_event(event("Door/State/DoorWarning/$source/DoorToken/porta", {"State": "Normal"}))
    service.notify_change()
    await asyncio.sleep(0)
    assert len(changes) == 3
    assert db.list_events() == []


async def test_exit_button_is_an_exit_and_intercom_relay_a_remote_opening(service, db):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    db.save_controller_input(
        controller_id, "REX", {"name": "Pulsante Portineria", "role": "rex", "access_point_token": "ap-rex"}
    )
    db.save_controller_input(
        controller_id, "IO1", {"name": "Relè Citofono", "role": "intercom", "access_point_token": "ap-relay"}
    )
    service.record_event(event("AccessControl/AccessGranted/Anonymous/x", {"AccessPointToken": "ap-rex"}))
    later = event("AccessControl/AccessGranted/Anonymous/y", {"AccessPointToken": "ap-relay"})
    service.record_event(dataclasses.replace(later, timestamp=datetime(2026, 10, 25, 9, tzinfo=UTC)))
    relay, button = db.list_events()[:2]
    # Collaudo 0.21.4: il pulsante era un'apertura remota; è un'uscita. Il relè apre a distanza dal citofono
    assert button["kind"] == "uscita" and button["source"] == "Pulsante Portineria"
    assert button["detail"] == "Pulsante Uscita · REX"
    assert relay["kind"] == "apertura_remota" and relay["source"] == "Relè Citofono"
    assert relay["detail"] == "Relè Citofono · AUX 1"

    # Negato (al banco: InvalidAccessProfile): nel registro con il nome dell'ingresso, non come tessera anonima
    denied = event("AccessControl/Denied/Anonymous/z", {"AccessPointToken": "ap-relay", "Reason": "Boh"})
    service.record_event(dataclasses.replace(denied, timestamp=datetime(2026, 10, 25, 10, tzinfo=UTC)))
    row = db.list_events()[0]
    assert row["kind"] == "negato" and row["source"] == "Relè Citofono" and row["card"] == ""
    assert row["detail"] == "Relè Citofono · AUX 1 · Boh"


async def test_exit_button_from_mqtt_topic_is_logged_once_with_the_input_name(service, db):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    token = "Axis-b8a44fe6a074:1792938186.362930000"
    db.save_controller_input(
        controller_id, "IO1", {"name": "Pulsante Portineria", "role": "rex", "access_point_token": token}
    )
    topic = f"accessi/x/eventi/AccessControl/AccessGranted/Anonymous/$source/AccessPointToken/{token}"
    payload = json.dumps({"timestamp": 1761479926000, "message": {"source": {}, "key": {}, "data": {}}}).encode()
    service.monitor.on_event = service.record_event
    service.monitor.handle(topic, payload)
    [row] = db.list_events()
    assert row["kind"] == "uscita" and row["source"] == "Pulsante Portineria"
    assert row["detail"] == "Pulsante Uscita · AUX 1"

    # Al banco un ponticello tenuto a mano ha dato 4 REX in 5 secondi: una riga sola, segnata
    for millis in (1761479928000, 1761479930000, 1761479931000):
        service.monitor.handle(topic, payload.replace(b"1761479926000", str(millis).encode()))
    [row] = db.list_events()
    assert row["detail"] == "Pulsante Uscita · AUX 1 · più pressioni"
    # Il recupero dal registro del controller non rifà le pressioni accorpate
    service._recovering = True
    try:
        moment = datetime.fromtimestamp(1761479930, tz=UTC)
        service.record_event(
            Event("AccessControl", "AccessControl/AccessGranted/Anonymous/", moment, {"AccessPointToken": token})
        )
    finally:
        service._recovering = False
    assert len(db.list_events()) == 1
    # Una pressione più tardi è un'altra riga
    service.monitor.handle(topic, payload.replace(b"1761479926000", b"1761479990000"))
    assert len(db.list_events()) == 2


async def test_same_press_from_mqtt_and_from_recovery_is_not_a_repeat(service, db):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    db.save_controller_input(
        controller_id, "REX", {"name": "Via Consulta - Videocitofono", "role": "intercom", "access_point_token": "ap"}
    )
    denied = {"AccessPointToken": "ap", "Reason": "NotPermittedAtThisTime"}
    moment = datetime(2026, 10, 26, 12, 51, 41, tzinfo=UTC)

    def press(recovering: bool, at: datetime) -> None:
        service._recent_decisions.clear()
        service._recovering = recovering
        try:
            service.record_event(Event("AccessControl", "AccessControl/Denied/Anonymous/", at, dict(denied)))
        finally:
            service._recovering = False

    # Collaudo 0.21.5: un REX simulato, poi il recupero all'avvio lo rilegge e la riga diventava "più pressioni"
    press(False, moment)
    press(True, moment + timedelta(seconds=1))
    [row] = db.list_events()
    assert not row["detail"].endswith("più pressioni")
    # Al contrario: recuperato prima, poi arriva in ritardo via MQTT
    press(True, moment + timedelta(minutes=5))
    press(False, moment + timedelta(minutes=5))
    first, second = db.list_events()[:2]
    assert not first["detail"].endswith("più pressioni") and not second["detail"].endswith("più pressioni")
    # Una seconda pressione vera resta accorpata
    press(False, moment + timedelta(minutes=5, seconds=4))
    assert len(db.list_events()) == 2 and db.list_events()[0]["detail"].endswith("più pressioni")


async def test_casing_and_tilt_are_merged_within_five_minutes(service, db):
    db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210 Banco", "verify_ssl": 0})
    start = datetime(2026, 10, 26, 12, 20, 59, tzinfo=UTC)
    for seconds in range(0, 125, 10):
        moment = start + timedelta(seconds=seconds)
        service.record_event(Event("Device", "Device/Casing/Open/$source/Name/Casing", moment, {"Open": "1"}))
        service.record_event(Event("Device", "Device/Casing/Open/$source/Name/Casing", moment, {"Open": "0"}))
    service.record_event(Event("Device", "Device/Tampering/TiltDetected", start, {}))
    service.record_event(Event("Device", "Device/Tampering/TiltDetected", start + timedelta(seconds=40), {}))
    rows = {e["detail"]: e for e in db.list_events()}
    assert set(rows) == {"coperchio del controller aperto · ripetuta", "controller inclinato · ripetuta"}
    assert all(e["kind"] == "manomissione" and e["source"] == "A1210 Banco" for e in rows.values())
    # Dopo cinque minuti è un'altra manomissione
    service.record_event(Event("Device", "Device/Tampering/TiltDetected", start + timedelta(minutes=6), {}))
    assert [e["detail"] for e in db.list_events()][0] == "controller inclinato"


async def test_controller_casing_tamper_names_the_controller_not_a_door(service, db):
    db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210 Banco", "verify_ssl": 0})
    service.record_event(event("Device/Casing/Open/$source/Name/Casing", {"Open": "1"}))
    tamper = db.list_events()[0]
    assert tamper["kind"] == "manomissione" and tamper["detail"] == "coperchio del controller aperto"
    assert tamper["source"] == "A1210 Banco" and tamper["door"] == ""

    # Eventi delle versioni precedenti: stesso dettaglio e nome del controller
    db.add_event(ts="2026-10-25T09:00:00+00:00", kind="manomissione", detail="alloggiamento controller aperto")
    assert db.rename_controller_events("alloggiamento controller aperto", "coperchio del controller aperto", "A1") == 1
    assert {e["source"] for e in db.list_events()} == {"A1210 Banco", "A1"}


def _bench_log(credential_token: str) -> list[dict]:
    """Registro eventi letto dall'A1210 del banco (0.18): tessera sconosciuta e tessera valida."""
    import json
    from pathlib import Path

    entries = json.loads((Path(__file__).parent / "data" / "eventlogger_a1210.json").read_text())
    for entry in entries:
        for kv in entry["KeyValues"]:
            if kv["Key"] == "CredentialToken":
                kv["Value"] = credential_token
    return entries


async def test_event_from_log_matches_mqtt_topics():
    from app.events import event_from_log

    events = [event_from_log(e) for e in _bench_log("x")]
    assert [e.topic for e in events][:4] == [
        "IdPoint/Activity/",
        "IdPoint/Request/IdData/",
        "AccessControl/Denied/CredentialNotFound/Card/",
        "AccessControl/Denied/CredentialNotFound/",
    ]
    assert events[1].data["Card"] == "c0110b18"
    assert events[6].data == {"DoorToken": "porta_via_consulta_ingresso_scala", "State": "Accessed"}
    assert events[0].timestamp.isoformat(timespec="seconds") == "2026-10-25T17:54:43+00:00"


async def test_recover_events_from_controller_log(service, controller, db):
    person = await service.create_person("Ciccio Bello", "04A11A34", "admin", group="profilo_sempre")
    controller.log_events = _bench_log(person["cards"][0]["axis_credential_token"])
    rows = []

    async def after(row):
        rows.append(row)

    service.after_event = after

    recovered = await service.recover_events(datetime(2026, 10, 25, 17, 0, tzinfo=UTC))

    events = db.list_events()
    # Sconosciuta e negato (il doppio /Card conta una volta), lettura e concesso; DoorMode resta fuori
    assert sorted(e["kind"] for e in events) == ["concesso", "lettura", "negato", "sconosciuta"]
    assert recovered == 4
    assert all(e["recovered"] for e in events)
    granted = next(e for e in events if e["kind"] == "concesso")
    assert granted["person_name"] == "Ciccio Bello"
    # Una seconda lettura dello stesso registro non duplica niente
    assert await service.recover_events(datetime(2026, 10, 25, 17, 0, tzinfo=UTC)) == 0
    assert len(db.list_events()) == 4
    assert db.list_unknown_cards()[0]["count"] == 1


async def test_live_event_after_recovery_is_not_duplicated(service, controller, db):
    controller.log_events = _bench_log("x")[:4]
    await service.recover_events(datetime(2026, 10, 25, 17, 0, tzinfo=UTC))
    assert len(db.list_events()) == 2

    # Lo stesso negato arriva in ritardo via MQTT
    service.record_event(
        Event(
            category="AccessControl",
            topic="AccessControl/Denied/CredentialNotFound/Card/AccessPointToken/x",
            timestamp=datetime(2026, 10, 25, 17, 54, 43, tzinfo=UTC),
            data={"Card": "c0110b18", "AccessPointToken": "accesso_via_consulta_ingresso_scala"},
        )
    )

    assert len(db.list_events()) == 2


async def test_recovery_does_not_duplicate_events_of_a_card_removed_meanwhile(service, db):
    # Collaudo 0.18.0: tessera letta e accettata via MQTT, poi tolta dall'app, poi recupero dal registro.
    # Il recupero ricalcolava lettura → sconosciuta e un concesso senza numero: due doppioni.
    person = await service.create_person("Prova Stabilità", "c0118a01", "admin", group="profilo_sempre")
    card = person["cards"][0]
    moment = datetime(2026, 10, 25, 17, 28, 41, tzinfo=UTC)
    read = {"Card": "c0118a01", "IdPointToken": "lettore-1"}
    granted = {
        "AccessPointToken": "ap-in",
        "CredentialToken": card["axis_credential_token"],
        "CredentialHolderName": person["axis_user_token"],
    }
    service.record_event(Event("IdPoint", "IdPoint/Request/IdData/IdPointToken/lettore-1", moment, read))
    service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Card/", moment, granted))
    assert [e["kind"] for e in db.list_events()] == ["concesso", "lettura"]
    await service.delete_person(person["id"], "admin")

    service._recent_decisions.clear()  # come dopo un riavvio dell'add-on
    service._recovering = True
    try:
        service.record_event(Event("IdPoint", "IdPoint/Request/IdData/", moment, read))
        service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Card/", moment, granted))
        # Un passaggio davvero perso, dopo che la persona è stata tolta
        later = moment + timedelta(seconds=20)
        service.record_event(Event("IdPoint", "IdPoint/Request/IdData/", later, read))
        service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Card/", later, granted))
    finally:
        service._recovering = False

    events = db.list_events()
    assert [(e["kind"], e["recovered"]) for e in events] == [
        ("concesso", 1),
        ("sconosciuta", 1),
        ("concesso", 0),
        ("lettura", 0),
    ]
    # Nome leggibile al posto del token dell'utente, e la tessera in Da Registrare solo per la lettura persa
    assert events[0]["person_name"] == "Prova Stabilità (tolta dall'app)"
    assert db.list_unknown_cards()[0]["count"] == 1


def test_migration_fills_event_refs(tmp_path):
    import sqlite3

    from app.db import SCHEMA
    from app.service import event_ref

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE events ADD COLUMN door TEXT NOT NULL DEFAULT ''")
    rows = [
        ("lettura", "C0118A01", "", "lettore"),
        ("sconosciuta", "c0118b02", "", "lettore"),
        ("concesso", "c0118a01", "", "controller"),
        ("negato", "", "tessera non registrata", "controller"),
        ("allarme", "", "porta aperta troppo a lungo", "porta"),
        ("apertura_remota", "", "", "Mario (HA)"),
    ]
    for kind, card, detail, source in rows:
        conn.execute(
            "INSERT INTO events (ts, kind, card, detail, source, door) "
            "VALUES ('2026-10-25T17:00:00+00:00', ?, ?, ?, ?, 'p')",
            (kind, card, detail, source),
        )
    conn.commit()
    conn.close()

    db = Database(path, "0.18.1")
    refs = {e["kind"]: e["ref"] for e in db._query("SELECT kind, ref FROM events")}
    for kind, card, detail, _ in rows[:5]:
        assert refs[kind] == event_ref(kind, "p", card, detail)
    # Le aperture da Home Assistant non vengono dal controller
    assert refs["apertura_remota"] == ""
    db.close()


def test_migration_removes_duplicates_left_by_recovery(tmp_path):
    import sqlite3

    from app.db import SCHEMA

    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("ALTER TABLE events ADD COLUMN door TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE events ADD COLUMN recovered INTEGER NOT NULL DEFAULT 0")
    # Collaudo 0.18.0: lettura e concesso via MQTT, poi gli stessi due ricalcolati dal recupero
    rows = [
        ("2026-10-25T17:28:41+00:00", "lettura", "c0118a01", "", "lettore", 0),
        ("2026-10-25T17:28:41+00:00", "concesso", "c0118a01", "Prova Stabilità", "controller", 0),
        ("2026-10-25T17:28:41+00:00", "sconosciuta", "c0118a01", "", "lettore", 1),
        ("2026-10-25T17:28:41+00:00", "concesso", "", "utente_prova_stabilita_261025192617", "controller", 1),
        # Recuperato davvero perso: resta
        ("2026-10-25T18:00:59+00:00", "sconosciuta", "c0118b02", "", "lettore", 1),
    ]
    conn.executemany(
        "INSERT INTO events (ts, kind, card, person_name, source, recovered, door) VALUES (?, ?, ?, ?, ?, ?, 'p')",
        rows,
    )
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    db = Database(path, "0.19.1")
    left = db._query("SELECT kind, card, recovered FROM events ORDER BY id")
    assert [(e["kind"], e["card"], e["recovered"]) for e in left] == [
        ("lettura", "c0118a01", 0),
        ("concesso", "c0118a01", 0),
        ("sconosciuta", "c0118b02", 1),
    ]
    db.close()


async def test_holder_name_without_earlier_events(service):
    # Nessun accesso nel registro: il nome si ricava dal token, senza accenti
    assert service._holder_name({"CredentialHolderName": "utente_mario_rossi_261025120000"}) == (
        "Mario Rossi (tolta dall'app)"
    )
    assert service._holder_name({"CredentialHolderName": "Ospite Esterno"}) == "Ospite Esterno"


async def test_new_person_never_reuses_number_of_deleted_one(service, db):
    # Collaudo 0.19: "Prova Backup" aveva preso il numero di "Prova Stabilità", con passaggi e ultimo accesso
    first = await service.create_person("Prova Stabilità", "c0118a01", "admin", group="profilo_sempre")
    db.add_event(ts="2026-10-25T17:28:41+00:00", kind="concesso", person_id=first["id"], person_name="Prova Stabilità")
    await service.delete_person(first["id"], "admin")
    # Gli eventi restano con il nome, staccati dalla persona eliminata
    [event] = db.list_events()
    assert event["person_id"] is None and event["person_name"] == "Prova Stabilità"
    second = await service.create_person("Prova Backup", "c0118a02", "admin", group="profilo_sempre")
    assert second["id"] > first["id"]
    assert db.list_events(person_id=second["id"]) == []
    # Anche senza eventi: il numero più alto dato resta memorizzato
    await service.delete_person(second["id"], "admin")
    third = await service.create_person("Terza", "c0118a03", "admin", group="profilo_sempre")
    assert third["id"] > second["id"]


def test_migration_detaches_events_older_than_person(tmp_path):
    import sqlite3

    path = tmp_path / "v3.db"
    Database(path).close()
    conn = sqlite3.connect(path)
    # Come al banco: persona 8 nuova con gli eventi della persona 8 eliminata prima
    conn.execute(
        "INSERT INTO persons (id, name, created_at, updated_at) VALUES "
        "(8, 'Prova Backup', '2026-10-25T18:50:00+00:00', '2026-10-25T18:50:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events (ts, kind, person_id, person_name) VALUES "
        "('2026-10-25T17:28:41+00:00', 'concesso', 8, 'Prova Stabilità'), "
        "('2026-10-25T18:55:00+00:00', 'concesso', 8, 'Prova Backup')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    db = Database(path, "0.19.2")
    rows = db._query("SELECT person_id, person_name FROM events ORDER BY ts")
    assert [(r["person_id"], r["person_name"]) for r in rows] == [(None, "Prova Stabilità"), (8, "Prova Backup")]
    db.close()


def test_migration_detaches_events_of_deleted_persons(tmp_path):
    import sqlite3

    path = tmp_path / "v4.db"
    Database(path).close()
    conn = sqlite3.connect(path)
    # Collaudo 0.19.2: la persona 8 era già eliminata quando è girata la migrazione 4,
    # poi un ripristino l'ha rimessa con lo stesso numero e gli eventi di chi l'aveva avuto prima
    conn.execute(
        "INSERT INTO persons (id, name, created_at, updated_at) VALUES "
        "(3, 'Anna', '2026-10-01T08:00:00+00:00', '2026-10-01T08:00:00+00:00'), "
        "(5, 'Prova Backup', '2026-10-25T18:50:00+00:00', '2026-10-25T18:50:00+00:00')"
    )
    conn.execute(
        "INSERT INTO events (ts, kind, person_id, person_name) VALUES "
        "('2026-10-25T10:00:00+00:00', 'concesso', 3, 'Anna'), "
        "('2026-10-25T17:28:41+00:00', 'concesso', 8, 'Prova Stabilità'), "
        "('2026-10-25T16:00:00+00:00', 'concesso', 5, 'Collaudo Prova'), "
        "('2026-10-25T18:55:00+00:00', 'concesso', 5, 'Prova Backup')"
    )
    conn.execute("DELETE FROM app_settings WHERE key = 'persons_last_id'")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()

    db = Database(path, "0.19.3")
    rows = db._query("SELECT person_id, person_name FROM events ORDER BY ts")
    assert [(r["person_id"], r["person_name"]) for r in rows] == [
        (3, "Anna"),
        (None, "Collaudo Prova"),
        (None, "Prova Stabilità"),
        (5, "Prova Backup"),
    ]
    # Il numero 8 resta usato anche se nessun evento lo cita più
    assert db.add_person("Nuova", None) == 9
    db.close()


def test_event_from_log_in_utc():
    from app.events import event_from_log

    entry = {"UtcTime": "2026-10-25T19:54:43+02:00", "KeyValues": [{"Key": "topic0", "Value": "AccessControl"}]}
    assert event_from_log(entry).timestamp.isoformat(timespec="seconds") == "2026-10-25T17:54:43+00:00"


async def test_app_events_use_controller_clock(service, db):
    """HA 36 s indietro, controller sincronizzato: apertura remota e citofono con l'ora del controller."""
    service.monitor.door_mode = "Locked"
    service.clock_offset = lambda: 36.0
    await service.open_door(False, "admin")
    service.record_intercom("binary_sensor.citofono", "Citofono")
    expected = datetime.now(tz=UTC) + timedelta(seconds=36)
    for e in db.list_events():
        assert abs(datetime.fromisoformat(e["ts"]) - expected) < timedelta(seconds=3)
    # Controller non sincronizzato (o scarto assurdo): ora di Home Assistant
    service.clock_offset = lambda: None
    assert abs(datetime.fromisoformat(service.event_now()) - datetime.now(tz=UTC)) < timedelta(seconds=3)
    service.clock_offset = lambda: 3 * 86400
    assert abs(datetime.fromisoformat(service.event_now()) - datetime.now(tz=UTC)) < timedelta(seconds=3)


def test_clock_offset_only_from_synced_primary_controller():
    from types import SimpleNamespace

    from app.health import ControllerHealth
    from app.main import _controller_clock_offset

    health = ControllerHealth(id=1, name="A1210", clock_offset=36.0, ntp={"active": True})
    state = SimpleNamespace(installation=SimpleNamespace(primary={"id": 1}), health=SimpleNamespace(states={1: health}))
    assert _controller_clock_offset(state) == 36.0
    health.ntp = {"active": False}
    assert _controller_clock_offset(state) is None
    state.installation.primary = None
    assert _controller_clock_offset(state) is None
