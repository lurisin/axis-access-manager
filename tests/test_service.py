import asyncio
import dataclasses
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from app.db import Database
from app.events import Event, EventMonitor
from app.rules import normalize_validity
from app.service import AccessService, PersonChanged, ServiceError, normalize_card, user_token_for
from app.vapix import ControllerGuard, ControllerUnreachable, VapixError


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

    # Togli dall'App toglie solo le tessere: le persone restano in anagrafica
    controller.calls.clear()
    assert service.forget_missing("admin") == {"cards": 1}
    assert db.get_person(mario["id"])["cards"] == []
    assert [p["name"] for p in db.list_persons()] == ["Anna Bianchi", "Mario Rossi"]
    assert controller.calls == []


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


async def test_deleting_last_card_keeps_person(service, controller, db):
    person = await service.create_person("Mario Rossi", "04aa", "admin", company="Ditta", notes="chiavi")
    person = await service.add_card(person["id"], "04bb", "admin")
    old_user = person["axis_user_token"]

    remaining = await service.delete_card(person["cards"][0]["id"], "admin")
    assert [c["number"] for c in remaining["cards"]] == ["04bb"]
    assert old_user in controller.users

    # L'ultima: la persona resta con ditta e note, l'utente Axis sparisce dal controller e il token si azzera
    remaining = await service.delete_card(remaining["cards"][0]["id"], "admin")
    assert remaining["id"] == person["id"] and remaining["cards"] == []
    assert remaining["company"] == "Ditta" and remaining["notes"] == "chiavi"
    assert remaining["axis_user_token"] is None
    assert controller.users == {} and controller.credentials == {}
    assert [p["name"] for p in db.list_persons()] == ["Mario Rossi"]

    # Una tessera dopo crea un utente nuovo e funziona
    controller.calls.clear()
    person = await service.add_card(person["id"], "04cc", "admin")
    new_user = person["axis_user_token"]
    assert [c[0] for c in controller.calls] == ["set_user", "set_credential"]
    assert new_user.startswith("utente_mario_rossi_") and controller.users[new_user]["Name"] == "Mario Rossi"
    [credential] = controller.credentials.values()
    assert credential["UserToken"] == new_user and credential["IdData"] == [{"Name": "Card", "Value": "04cc"}]
    assert person["cards"][0]["axis_credential_token"] == credential["token"]
    # E la sincronizzazione la lascia alla stessa persona
    await service.sync_from_controller()
    assert [(p["id"], [c["number"] for c in p["cards"]]) for p in db.list_persons()] == [(person["id"], ["04cc"])]


async def test_last_card_with_user_not_removed_keeps_the_token(service, controller, db):
    """Utente Axis non tolto (controller che non risponde più): resta legato alla persona, che lo riusa."""
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    token = person["axis_user_token"]

    async def refused(token):
        raise VapixError("il controller non risponde (tempo scaduto)")

    controller.remove_user = refused
    remaining = await service.delete_card(person["cards"][0]["id"], "admin")
    assert remaining["cards"] == [] and remaining["axis_user_token"] == token
    del controller.remove_user

    person = await service.add_card(person["id"], "04bb", "admin")
    assert person["axis_user_token"] == token and list(controller.users) == [token]
    assert controller.credentials[person["cards"][0]["axis_credential_token"]]["UserToken"] == token
    # Elimina Persona toglie anche l'utente
    await service.delete_person(person["id"], "admin")
    assert controller.users == {} and controller.credentials == {}


class SwitchedOffController:
    """Controller spento davvero: ogni chiamata, letture comprese, resta senza risposta."""

    paused = ""

    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        async def unreachable(*args, **kwargs):
            self.calls.append(name)
            raise ControllerUnreachable("il controller non risponde (tempo scaduto)")

        return unreachable


def _full(*args, **kwargs):
    err = sqlite3.OperationalError("database or disk is full")
    err.sqlite_errorcode = sqlite3.SQLITE_FULL
    raise err


async def test_create_person_without_card_with_the_controller_off(db, settings):
    off = SwitchedOffController()
    service = AccessService(db, off, settings)
    category = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 1, "description": ""}
    )

    person = await service.create_person(
        "Anna Bianchi", "", "admin", company="Pulizie", notes="chiavi", category_id=category
    )

    assert off.calls == []
    assert person["cards"] == [] and person["axis_user_token"] is None
    assert (person["company"], person["notes"], person["category_id"]) == ("Pulizie", "chiavi", category)
    assert db.list_audit()[0]["detail"] == "Anna Bianchi, senza tessera, tipologia Ospite"
    # Anche le altre modifiche di una persona senza tessere restano nell'app
    person = await service.rename_person(person["id"], "Anna Rossi", "admin")
    person = await service.set_person_access(person["id"], "", "", "", "admin", category_id=category)
    assert person["name"] == "Anna Rossi" and off.calls == []
    # Con la tessera serve il controller, come prima
    with pytest.raises(ControllerUnreachable):
        await service.create_person("Mario", "04aa", "admin")
    assert [p["name"] for p in db.list_persons()] == ["Anna Rossi"]
    await service.delete_person(person["id"], "admin")
    assert db.list_persons() == []


async def test_create_person_without_card_with_writes_paused(db, controller, settings):
    guard = ControllerGuard(controller)
    guard.paused = "all'indirizzo risponde un altro controller"
    service = AccessService(db, guard, settings)

    person = await service.create_person("Anna", "  ", "admin")

    assert person["cards"] == [] and controller.calls == []
    with pytest.raises(VapixError, match="scritture sospese"):
        await service.create_person("Mario", "04aa", "admin")


async def test_create_person_without_card_refuses_validity(service, db):
    with pytest.raises(ServiceError, match="valgono per le tessere"):
        await service.create_person("Anna", "", "admin", valid_to="2026-12-31T00:00:00Z")
    with pytest.raises(ServiceError, match="valgono per le tessere"):
        await service.create_person("Anna", "", "admin", group="p2")
    assert db.list_persons() == []


async def test_create_person_without_card_with_the_disk_full(service, controller, db, monkeypatch):
    monkeypatch.setattr(db, "set_person_category", _full)
    category = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 0, "description": ""}
    )
    with pytest.raises(ServiceError, match="spazio su disco esaurito: la persona non è stata registrata"):
        await service.create_person("Anna", "", "admin", category_id=category)
    # Tutto o niente: nessuna persona a metà
    assert db.list_persons() == [] and controller.calls == []


async def test_first_card_takes_the_category_access(service, controller, db):
    category = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 2, "description": ""}
    )
    person = await service.create_person("Anna", "", "admin", category_id=category)

    person = await service.add_card(person["id"], "04aa", "admin")

    [card] = person["cards"]
    assert card["access_profile"] == "p2"
    start, end = datetime.fromisoformat(card["valid_from"]), datetime.fromisoformat(card["valid_to"])
    assert end - start == timedelta(days=2) and abs(datetime.now(tz=UTC) - start) < timedelta(minutes=1)
    credential = controller.credentials[card["axis_credential_token"]]
    assert credential["CredentialAccessProfile"][0]["AccessProfile"] == "p2"
    assert controller.users[credential["UserToken"]]["Name"] == "Anna"
    assert person["axis_user_token"] == credential["UserToken"]


async def test_first_card_with_the_answer_lost_stays_with_the_person(service, controller, db):
    """Tessera scritta e risposta persa: il token dell'utente è già salvato, e la sincronizzazione porta la tessera
    alla persona invece di crearne una nuova."""
    person = await service.create_person("Anna", "", "admin")
    write = controller.set_credential

    async def lost(credential):
        await write(credential)
        raise ControllerUnreachable("il controller non risponde (tempo scaduto)")

    controller.set_credential = lost
    with pytest.raises(ControllerUnreachable):
        await service.add_card(person["id"], "04aa", "admin")
    del controller.set_credential

    await service.sync_from_controller()
    [anna] = db.list_persons()
    assert anna["id"] == person["id"] and [c["number"] for c in anna["cards"]] == ["04aa"]


async def test_first_card_with_the_disk_full_leaves_nothing_on_the_controller(service, controller, db, monkeypatch):
    person = await service.create_person("Anna", "", "admin")
    monkeypatch.setattr(db, "add_card", _full)

    with pytest.raises(ServiceError, match="la tessera non è stata aggiunta"):
        await service.add_card(person["id"], "04aa", "admin")

    # Né la tessera né l'utente, che senza tessere non resta sul controller
    assert controller.credentials == {} and controller.users == {}
    monkeypatch.undo()
    person = await service.add_card(person["id"], "04aa", "admin")
    assert controller.users[person["axis_user_token"]]["Name"] == "Anna"


async def test_first_card_with_chosen_access_and_category(service, controller, db, monkeypatch):
    """Importazione: la prima tessera nasce con gruppo, validità e tipologia della riga, e la persona cambia
    tipologia insieme a lei. Con il disco pieno né l'una né l'altra; con una tessera arrivata nel frattempo niente."""
    ospite, corsi = (
        db.save_category(
            None, {"name": name, "color": "blue", "group_token": "p2", "valid_days": days, "description": ""}
        )
        for name, days in (("Ospite", 1), ("Corsi", 30))
    )
    person = await service.create_person("Anna", "", "admin", category_id=ospite)
    monkeypatch.setattr(db, "set_person_category", _full)
    with pytest.raises(ServiceError, match="la tessera non è stata aggiunta"):
        await service.add_card(person["id"], "04aa", "admin", category_id=corsi)
    assert controller.credentials == {} and controller.users == {}
    assert db.get_person(person["id"])["cards"] == [] and db.get_person(person["id"])["category_id"] == ospite
    monkeypatch.undo()

    person = await service.add_card(person["id"], "04aa", "admin", valid_to="2027-01-31T18:00:00", category_id=corsi)
    [card] = person["cards"]
    assert person["category_id"] == corsi
    assert (card["access_profile"], card["valid_from"], card["valid_to"]) == (
        "p2",
        "",
        normalize_validity("2027-01-31T18:00:00"),
    )
    assert [a["action"] for a in db.list_audit()[:2]] == ["tipologia cambiata", "tessera aggiunta"]

    calls = len(controller.calls)
    with pytest.raises(PersonChanged, match="Anna ha già 1 tessera: la nuova ne prenderebbe l'accesso"):
        await service.add_card(person["id"], "04bb", "admin", group="profilo_sempre")
    assert len(controller.calls) == calls and len(db.get_person(person["id"])["cards"]) == 1
    # Senza valori scelti la seconda eredita l'accesso della prima, come sempre
    person = await service.add_card(person["id"], "04bb", "admin")
    assert {c["valid_to"] for c in person["cards"]} == {card["valid_to"]}


async def test_access_of_a_person_without_cards(service, controller, db):
    person = await service.create_person("Anna", "", "admin")
    with pytest.raises(ServiceError, match="aggiungi prima una tessera"):
        await service.set_person_access(person["id"], "p2", "", "2027-01-01T00:00:00Z", "admin")
    category = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 0, "description": ""}
    )
    person = await service.set_person_access(person["id"], "", "", "", "admin", category_id=category)
    assert person["category_id"] == category and controller.calls == []


async def test_access_of_a_person_without_cards_is_only_the_category(service, controller, db):
    """Senza tessere un gruppo diverso da quello della tipologia non ha dove stare: si rifiuta, invece di scriverlo in
    Attività e dare poi alla prima tessera un altro accesso. In Attività solo la tipologia, quando cambia."""
    controller.profiles["solo_ingresso"] = {"token": "solo_ingresso", "Name": "Solo Ingresso", "AccessPolicy": []}
    guest = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 0, "description": ""}
    )
    anna = await service.create_person("Anna", "", "admin")
    audit = len(db.list_audit())
    with pytest.raises(ServiceError, match="senza tessere il gruppo non si può salvare"):
        await service.set_person_access(anna["id"], "solo_ingresso", "", "", "admin")
    with pytest.raises(ServiceError, match="senza tessere il gruppo non si può salvare"):
        await service.set_person_access(anna["id"], "solo_ingresso", "", "", "admin", category_id=guest)
    # Il gruppo che la prima tessera prenderà comunque passa, senza righe in Attività: non cambia niente
    await service.set_person_access(anna["id"], "profilo_sempre", "", "", "admin")
    await service.set_person_access(anna["id"], "", "", "", "admin")
    assert len(db.list_audit()) == audit and db.get_person(anna["id"])["category_id"] == 0

    await service.set_person_access(anna["id"], "p2", "", "", "admin", category_id=guest)
    await service.set_person_access(anna["id"], "", "", "", "admin", category_id=guest)
    [changed] = db.list_audit()[: len(db.list_audit()) - audit]
    assert (changed["action"], changed["detail"]) == ("tipologia cambiata", "Anna: Ospite")
    assert controller.calls == []
    # La prima tessera prende proprio l'accesso della tipologia
    [card] = (await service.add_card(anna["id"], "04aa", "admin"))["cards"]
    assert card["access_profile"] == "p2"


async def test_deleting_a_person_without_cards_says_so(service, db):
    zeno = await service.create_person("Zeno", "", "admin")
    await service.delete_person(zeno["id"], "admin")
    assert (db.list_audit()[0]["action"], db.list_audit()[0]["detail"]) == ("persona eliminata", "Zeno, senza tessere")
    anna = await service.create_person("Anna", "04aa", "admin")
    await service.delete_person(anna["id"], "admin")
    assert db.list_audit()[0]["detail"] == "Anna (04aa)"


async def test_request_prepared_without_cards_does_not_touch_a_card_added_meanwhile(service, controller, db):
    """Tipologia senza validità (il modulo di chi non ha tessere) e Elimina Persona con 0 tessere contate: con una
    tessera arrivata nel frattempo non cambia niente, né nell'app né sul controller."""
    guest = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 1, "description": ""}
    )
    person = await service.create_person("Gina", "", "admin", category_id=guest)
    # Senza tessere va come prima
    await service.set_person_access(person["id"], "", "", "", "admin", category_id=guest, with_validity=False)
    [card] = (await service.add_card(person["id"], "04aa", "admin"))["cards"]
    controller.calls.clear()

    with pytest.raises(PersonChanged, match="Gina ha ora 1 tessera: la modifica era stata preparata"):
        await service.set_person_access(person["id"], "", "", "", "admin", category_id=guest, with_validity=False)
    with pytest.raises(PersonChanged, match="Gina ha ora 1 tessera: conferma l'eliminazione"):
        await service.delete_person(person["id"], "admin", cards=set())

    [after] = db.get_person(person["id"])["cards"]
    assert (after["valid_from"], after["valid_to"]) == (card["valid_from"], card["valid_to"]) != ("", "")
    assert controller.calls == [] and controller.credentials[card["axis_credential_token"]]["ValidTo"]
    # Tessera tolta nel frattempo (da un'altra scheda, o da un'eliminazione rimasta a metà): la conferma la elencava,
    # quindi non c'è niente di nuovo e la persona si elimina
    await service.delete_card(card["id"], "admin")
    await service.delete_person(person["id"], "admin", cards={card["id"]})
    assert db.list_persons() == []


async def test_sync_keeps_persons_without_cards(service, controller, db):
    anna = await service.create_person("Anna", "", "admin", company="Pulizie")
    token = controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    await service.sync_from_controller()
    # La tessera di Mario passata a un altro utente sul controller: Mario resta, senza tessere
    controller.add_existing("utente_luca", "Luca", "04bb")
    controller.credentials[token]["UserToken"] = "utente_luca"

    await service.sync_from_controller()

    persons = {p["name"]: p for p in db.list_persons()}
    assert set(persons) == {"Anna", "Luca", "Mario Rossi"}
    assert persons["Anna"]["id"] == anna["id"] and persons["Anna"]["company"] == "Pulizie"
    assert persons["Mario Rossi"]["cards"] == []
    assert sorted(c["number"] for c in persons["Luca"]["cards"]) == ["04aa", "04bb"]


async def test_rename_person_without_cards_does_not_create_the_user(service, controller, db):
    """Persona rimasta senza tessere con l'utente già tolto dal controller: il nome cambia solo nell'app."""
    person = await service.create_person("Anna", "04aa", "admin")
    controller.credentials.clear()
    await service.sync_from_controller()
    service.forget_missing("admin")
    controller.users.clear()
    controller.calls.clear()

    person = await service.rename_person(person["id"], "Anna Rossi", "admin")

    assert person["name"] == "Anna Rossi" and controller.users == {}
    assert not [c for c in controller.calls if c[0] == "set_user"]
    # La prima tessera dopo riscrive l'utente con il nome nuovo
    person = await service.add_card(person["id"], "04bb", "admin")
    assert controller.users[person["axis_user_token"]]["Name"] == "Anna Rossi"


async def test_delete_person_removes_everything(service, controller, db):
    person = await service.create_person("Mario Rossi", "04aa", "admin")
    await service.add_card(person["id"], "04bb", "admin")

    await service.delete_person(person["id"], "admin")

    assert controller.credentials == {}
    assert controller.users == {}
    assert db.list_persons() == []


async def test_open_door_only_when_locked(service, controller, db):
    service.monitor.connected, service.monitor.controller_online = True, True
    service.monitor.door_mode = "Unlocked"
    controller.door = "Unlocked"
    with pytest.raises(ServiceError, match="non è bloccata"):
        await service.open_door(False, "admin")
    assert not [c for c in controller.calls if c[0] == "access_door"]

    # Già aperta a tempo (collaudo: secondo comando da Home Assistant un secondo dopo il primo): nessun comando,
    # nessun rifiuto, nessuna riga
    service.monitor.door_mode = "Accessed"
    controller.door = "Accessed"
    assert await service.open_door(False, "admin") is False
    assert not [c for c in controller.calls if c[0] == "access_door"] and db.list_events() == []

    # "Bloccata" perso via MQTT: in memoria resta "Aperta a Tempo", ma decide il controller
    controller.door = "Locked"
    assert await service.open_door(False, "admin") is True
    assert ("access_door", "porta", False) in controller.calls

    service.monitor.door_mode = "Locked"
    await service.open_door(True, "admin")
    assert ("access_door", "porta", True) in controller.calls
    assert db.list_events()[0]["kind"] == "apertura_remota"


async def test_open_door_without_live_events_asks_the_controller(service, controller):
    # Collaudo 29/10: controller scollegato dal broker per ore, in memoria una modalità vecchia
    service.monitor.connected, service.monitor.controller_online = True, False
    service.monitor.door_mode = "Locked"
    controller.door = "Accessed"
    assert await service.open_door(False, "admin") is False
    assert not [c for c in controller.calls if c[0] == "access_door"]
    assert service.monitor.door_mode == "Accessed"
    controller.door = "Locked"
    assert await service.open_door(False, "admin") is True

    # Controller che non risponde: senza eventi in diretta la memoria non basta
    async def broken(door_token):
        raise VapixError("timeout")

    controller.door_mode = broken
    service.monitor.door_mode = "Locked"
    with pytest.raises(ServiceError, match="sconosciuto"):
        await service.open_door(False, "admin")


async def test_open_door_trusts_memory_only_with_live_events(service, controller):
    # Broker collegato ma controller che non pubblica (MQTT facoltativo) e WebSocket giù: la memoria può essere vecchia
    service.monitor.connected, service.monitor.controller_online = True, None
    service.monitor.door_mode = "Locked"
    controller.door = "Accessed"
    assert await service.open_door(False, "admin") is False
    assert not [c for c in controller.calls if c[0] == "access_door"]
    # Con il WebSocket collegato la modalità in memoria è in diretta
    service.monitor.ws_live = True
    service.monitor.door_mode = "Locked"
    assert await service.open_door(False, "admin") is True
    assert ("access_door", "porta", False) in controller.calls


async def test_open_door_asks_controller_when_state_unknown(service, controller):
    controller.door = "Unlocked"
    with pytest.raises(ServiceError, match=r"\(Sbloccata\)"):
        await service.open_door(False, "admin")


async def test_open_door_gives_the_safety_net_its_time_before_the_command(service, controller):
    from app.lock_guard import LockGuard
    from app.vapix import ControllerUnreachable

    guard = service.lock_guard = LockGuard()
    during = []

    async def lost(door_token, extended=False):
        during.append(guard.grants.get(door_token))
        raise ControllerUnreachable("il controller non risponde (nessuna risposta in tempo)")

    # Comando eseguito ma risposta persa: il tempo esteso vale già, e resta
    controller.access_door = lost
    with pytest.raises(ControllerUnreachable):
        await service.open_door(True, "Luca (Home Assistant)")
    assert during[0] is not None and during[0].extended and guard.grants["porta"] is during[0]
    # Anche dopo GRANT_WINDOW (Aperta a Tempo vista solo con la lettura dopo)
    assert guard.extended_at["porta"] == during[0].at

    async def refused(door_token, extended=False):
        raise VapixError("il controller ha rifiutato la richiesta")

    # Rifiutato dal controller: nessuna apertura, nessun consenso
    guard.grants.clear()
    guard.extended_at.clear()
    controller.access_door = refused
    with pytest.raises(VapixError):
        await service.open_door(True, "Luca (Home Assistant)")
    assert guard.grants == {} and guard.extended_at == {}


async def test_recent_extended_openings_from_the_log(service, db):
    now = datetime.now(tz=UTC)

    def opening(seconds_ago, door, detail):
        ts = (now - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
        db.add_event(ts=ts, kind="apertura_remota", detail=detail, source="Luca", door=door)

    opening(10, "porta", "tempo esteso, corriere")
    opening(30, "porta2", "tempo esteso")
    opening(5, "porta2", "")
    opening(90, "porta3", "tempo esteso")
    # Una riga con l'ora scritta male non ferma l'avvio dell'add-on
    db.add_event(ts=f"{now:%Y-%m-%dT%H:%M:%S}", kind="apertura_remota", detail="tempo esteso", door="porta4")
    found = service.recent_extended_openings(60)
    # Solo l'ultima apertura di ogni porta, se è estesa e recente
    assert list(found) == ["porta"] and 9 <= found["porta"] <= 12


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


async def test_alarm_sent_again_as_state_is_not_a_new_alarm(service, db):
    db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210 Banco", "verify_ssl": 0})
    published = []

    async def after_event(row):
        published.append(row["detail"])

    service.after_event = after_event
    at = datetime(2026, 11, 3, 21, 55, 20, 68000, tzinfo=UTC)
    alarm = Event("Door", "Door/State/DoorAlarm/$source/DoorToken/porta", at, {"State": "DoorForcedOpen"})
    casing = Event("Device", "Device/Casing/Open/$source/Name/Casing", at, {"Open": "1"})
    service.record_event(alarm)
    service.record_event(casing)
    await asyncio.sleep(0)
    # Il WebSocket si ricollega e il controller rimanda lo stato con la sua ora, preso per un evento in diretta (ciclo
    # occupato oltre i primi secondi): niente seconda riga, niente evento a Home Assistant né foto
    service.record_event(alarm)
    service.record_event(casing)
    await asyncio.sleep(0)
    assert [e["detail"] for e in db.list_events()] == ["coperchio del controller aperto", "porta forzata"]
    assert published == ["porta forzata", "coperchio del controller aperto"]
    # Un allarme di un altro secondo è un altro allarme
    service.record_event(dataclasses.replace(alarm, timestamp=at + timedelta(seconds=1)))
    assert [e["detail"] for e in db.list_events()].count("porta forzata") == 2


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


DOOR = {
    "snapshot_camera": "",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": "",
    "intercom_open_door": 0,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}
MORNING = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)


async def _cleaning(service, controller, db) -> dict:
    """Varco Scala (lettore 1) e Cantina (lettore 2), porte dell'app. La tipologia Pulizie apre solo il Varco Scala,
    e Anna Neri ha una tessera di Pulizie."""
    from app.rules_service import RulesService

    controller.access_points.append(
        {"token": "ap-cantina", "Entity": "porta2", "IdPointDevice": [{"IdPoint": "lettore-2", "DeviceUUID": ""}]}
    )
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    scala = db.save_door(None, controller_id, DOOR | {"door_token": "porta", "name": "Varco Scala"})
    db.save_door(None, controller_id, DOOR | {"door_token": "porta2", "name": "Cantina"})
    rules = RulesService(service)
    rows = [{"target": "door", "target_id": scala, "schedule": "standard_always"}]
    category = await rules.save_category(None, {"name": "Pulizie", "rules": rows}, "admin")
    person = await service.create_person("Anna Neri", "04aa", "admin", category_id=category["id"])
    await service.load_access_points()
    return {"category": category, "person": person, "token": person["cards"][0]["axis_credential_token"]}


def _read(reader: str, at: datetime, card: str = "04aa") -> Event:
    """Lettura come la danno MQTT, WebSocket e registro interno: il lettore anche nei dati."""
    topic = f"IdPoint/Request/IdData/$source/IdPointToken/{reader}"
    return Event("IdPoint", topic, at, {"IdPointToken": reader, "Card": card})


def _denied(point: str, at: datetime, reason: str, **data) -> Event:
    topic = f"AccessControl/Denied/Credential/$source/AccessPointToken/{point}"
    return Event("AccessControl", topic, at, {"AccessPointToken": point, "Reason": reason, **data})


def _granted(point: str, at: datetime, token: str) -> Event:
    topic = f"AccessControl/AccessGranted/Credential/$source/AccessPointToken/{point}"
    return Event("AccessControl", topic, at, {"AccessPointToken": point, "CredentialToken": token})


async def test_card_on_a_door_outside_its_category_is_not_enabled_live(service, controller, db, settings):
    from app.events import event_from_ws
    from app.ha_mqtt import HaBridge

    plant = await _cleaning(service, controller, db)
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append(json.loads(payload))
        return True

    bridge = HaBridge(settings, EventMonitor(prefix="accessi/x"), db)
    bridge.monitor.publish = publish
    service.after_event = bridge.publish_event
    service.monitor.on_event = service.record_event
    # Calcolato solo dall'app: il controller spento non conta, e niente gli viene chiesto né scritto
    controller.down = True
    attempts = len(controller.attempts)

    def mqtt(event: Event) -> None:
        message = {"timestamp": event.timestamp.timestamp() * 1000, "message": {"data": event.data}}
        service.monitor.handle(f"accessi/x/eventi/{event.topic}", json.dumps(message).encode())

    def websocket(event: Event, point: dict) -> None:
        path = event.topic.split("/$source/")[0]
        message = {"source": point, "key": {}, "data": event.data}
        notification = {"topic": f"tns1:{path}", "timestamp": event.timestamp.timestamp() * 1000, "message": message}
        service.monitor.apply_event(event_from_ws(notification))

    mqtt(_read("lettore-2", MORNING))
    mqtt(_denied("ap-cantina", MORNING, "Unauthorized", CredentialToken=plant["token"]))
    later = MORNING + timedelta(minutes=1)
    websocket(_read("lettore-2", later), {"IdPointToken": "lettore-2"})
    websocket(_denied("ap-cantina", later, "Unauthorized", Card="04aa"), {"AccessPointToken": "ap-cantina"})
    # Sulla sua porta resta il motivo del controller
    mqtt(_read("lettore-1", MORNING + timedelta(minutes=2)))
    mqtt(_denied("ap-in", MORNING + timedelta(minutes=2), "NotPermittedAtThisTime", CredentialToken=plant["token"]))
    # Un consenso del controller resta un consenso, anche fuori dalla tipologia
    mqtt(_read("lettore-2", MORNING + timedelta(minutes=3)))
    mqtt(_granted("ap-cantina", MORNING + timedelta(minutes=3), plant["token"]))
    await asyncio.sleep(0)

    rows = [(e["kind"], e["door"], e["person_name"], e["detail"]) for e in reversed(db.list_events())]
    assert rows == [
        ("lettura", "porta2", "Anna Neri", "non abilitato"),
        ("negato", "porta2", "Anna Neri", "non abilitato"),
        ("lettura", "porta2", "Anna Neri", "non abilitato"),
        ("negato", "porta2", "Anna Neri", "non abilitato"),
        ("lettura", "porta", "Anna Neri", ""),
        ("negato", "porta", "Anna Neri", "fuori fascia oraria"),
        ("lettura", "porta2", "Anna Neri", "non abilitato"),
        ("concesso", "porta2", "Anna Neri", ""),
    ]
    assert controller.attempts[attempts:] == []
    # Home Assistant riceve gli stessi tipi di evento di prima: cambia solo il dettaglio
    events = [(m["event_type"], m["persona"], m["dettaglio"]) for m in sent if "event_type" in m]
    assert events[:2] == [
        ("tessera_letta", "Anna Neri", "non abilitato"),
        ("accesso_negato", "Anna Neri", "non abilitato"),
    ]
    assert events[-1] == ("accesso_concesso", "Anna Neri", "")
    assert sent[-1] == {
        "persona": "Anna Neri",
        "tessera": "04aa",
        "esito": "accesso_concesso",
        "dettaglio": "",
        "ora": (MORNING + timedelta(minutes=3)).isoformat(timespec="seconds"),
    }
    assert db.list_unknown_cards() == []


async def test_not_enabled_from_the_controller_log(service, controller, db):
    plant = await _cleaning(service, controller, db)
    # Dopo un riavvio dell'add-on: la mappa dei punti di accesso si rilegge prima del recupero
    service.access_point_doors, service.idpoint_doors = {}, {}

    def entry(rowid, values):
        pairs = [{"Key": k, "Value": v} for k, v in values.items()]
        return {"rowid": rowid, "UtcTime": "2026-10-25T09:00:00.100000Z", "KeyValues": pairs}

    read = {"topic0": "IdPoint", "topic1": "Request", "topic2": "IdData", "IdPointToken": "lettore-2", "Card": "04aa"}
    denied = {
        "topic0": "AccessControl",
        "topic1": "Denied",
        "topic2": "Credential",
        "AccessPointToken": "ap-cantina",
        "CredentialToken": plant["token"],
        "Reason": "Unauthorized",
    }
    controller.log_events = [entry(1, read), entry(2, denied)]

    assert await service.recover_events(datetime(2026, 10, 25, 8, 0, tzinfo=UTC)) == 2

    rows = [(e["kind"], e["door"], e["person_name"], e["detail"], e["recovered"]) for e in db.list_events()]
    assert sorted(rows) == [
        ("lettura", "porta2", "Anna Neri", "non abilitato", 1),
        ("negato", "porta2", "Anna Neri", "non abilitato", 1),
    ]


async def test_suspended_expired_and_not_yet_valid_win_over_not_enabled(service, controller, db):
    plant = await _cleaning(service, controller, db)
    card = plant["person"]["cards"][0]
    moments = (MORNING + timedelta(minutes=n) for n in range(100))

    def read(reader: str = "lettore-2") -> str:
        service.record_event(_read(reader, next(moments)))
        return db.list_events()[0]["detail"]

    def denied(reason: str = "Unauthorized") -> str:
        service.record_event(_denied("ap-cantina", next(moments), reason, CredentialToken=plant["token"]))
        return db.list_events()[0]["detail"]

    # Valida all'ora del passaggio, scaduta adesso: nel recupero vale l'ora dell'evento
    db.set_card_access(card["id"], {**card, "valid_to": "2026-10-25T12:00:00Z"})
    assert (read(), denied()) == ("non abilitato", "non abilitato")
    db.set_card_access(card["id"], {**card, "valid_to": "2026-10-01T00:00:00Z"})
    assert (read(), denied()) == ("tessera scaduta", "tessera scaduta")
    db.set_card_access(card["id"], {**card, "valid_from": "2026-11-01T00:00:00Z"})
    assert (read(), denied()) == ("tessera non ancora valida", "tessera non ancora valida")

    # Anche quando lo dice solo il controller
    db.set_card_access(card["id"], card)
    assert (denied("CredentialExpired"), denied("CredentialNotEnabled")) == ("tessera scaduta", "tessera sospesa")

    await service.set_card_enabled(card["id"], False, "admin")
    assert (read(), denied()) == ("tessera sospesa", "tessera sospesa")
    db.set_card_access(card["id"], {**card, "valid_to": "2026-10-01T00:00:00Z"})
    assert (read("lettore-1"), denied("CredentialExpired")) == ("tessera sospesa", "tessera sospesa")
    assert {e["person_name"] for e in db.list_events()} == {"Anna Neri"}


async def test_not_enabled_only_where_the_category_decides(service, controller, db):
    plant = await _cleaning(service, controller, db)
    token = plant["token"]
    controller.access_points.append({"token": "ap-garage", "Entity": "porta3"})
    await service.load_access_points()
    moments = (MORNING + timedelta(minutes=n) for n in range(100))

    def detail(event: Event) -> str:
        service.record_event(event)
        return db.list_events()[0]["detail"]

    unauthorized = "non autorizzata su questa porta o in questo orario"
    # Porta non certa (punto di accesso sconosciuto: l'evento va sulla porta principale) o che l'app non gestisce
    assert detail(_denied("ap-nuovo", next(moments), "Unauthorized", CredentialToken=token)) == unauthorized
    assert detail(_denied("ap-garage", next(moments), "Unauthorized", CredentialToken=token)) == unauthorized

    # Accesso personalizzato: le porte le decide il profilo scelto a mano sulla tessera
    await service.set_person_access(plant["person"]["id"], "profilo_sempre", "", "", "admin")
    assert detail(_denied("ap-cantina", next(moments), "Unauthorized", CredentialToken=token)) == unauthorized
    assert detail(_read("lettore-2", next(moments))) == ""

    # Senza tipologia: la tessera ha un gruppo scelto a mano, come sopra
    other = await service.create_person("Bruno Verdi", "04bb", "admin", group="profilo_sempre")
    assert other["category_id"] == 0
    other_token = other["cards"][0]["axis_credential_token"]
    assert detail(_denied("ap-cantina", next(moments), "Unauthorized", CredentialToken=other_token)) == unauthorized
    assert detail(_read("lettore-2", next(moments), card="04bb")) == ""

    # Tipologia senza righe (rimasta dai gruppi delle versioni precedenti): non apre niente
    db.set_category_rules(plant["category"]["id"], [])
    await service.set_person_access(plant["person"]["id"], plant["category"]["token"], "", "", "admin")
    assert detail(_denied("ap-in", next(moments), "NotPermittedAtThisTime", CredentialToken=token)) == "non abilitato"

    # Tessera sconosciuta: invariata
    service.record_event(_read("lettore-2", next(moments), card="05ff"))
    service.record_event(_denied("ap-cantina", next(moments), "CredentialNotFound", Card="05ff"))
    denied, read = db.list_events()[:2]
    assert (read["kind"], read["person_name"], read["detail"]) == ("sconosciuta", "", "")
    assert (denied["kind"], denied["person_name"], denied["detail"]) == ("negato", "", "tessera non registrata")
    assert db.list_unknown_cards()[0]["number"] == "05ff"


async def test_not_enabled_in_notifications_search_and_csv(service, controller, db):
    from app.notifications import NotificationService
    from app.transfer import events_csv

    plant = await _cleaning(service, controller, db)
    now = datetime.now(tz=UTC).replace(microsecond=0)
    service.record_event(_denied("ap-cantina", now, "Unauthorized", CredentialToken=plant["token"]))

    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={"porta2": "Cantina"},
    )
    [denied] = [i for i in items if i["type"] == "denied"]
    assert (denied["title"], denied["text"]) == ("Accesso Negato", "Anna Neri · non abilitato")
    assert denied["severity"] == "low"
    assert not [i for i in items if i["type"] == "unknown_card"]
    [found] = db.list_events(search="non abilitato")
    assert found["person_name"] == "Anna Neri"
    assert ";non abilitato;" in events_csv(db.list_events(), {"porta2": "Cantina"})


async def test_not_enabled_reaches_home_assistant_with_the_disk_full(service, controller, db, monkeypatch):
    plant = await _cleaning(service, controller, db)
    rows = []

    async def after(row):
        rows.append(row)

    def full(**fields):
        raise sqlite3.OperationalError("database or disk is full")

    service.after_event = after
    monkeypatch.setattr(db, "add_event", full)
    service.record_event(_denied("ap-cantina", MORNING, "Unauthorized", CredentialToken=plant["token"]))
    await asyncio.sleep(0)

    assert [(r["kind"], r["person_name"], r["detail"], r["id"]) for r in rows] == [
        ("negato", "Anna Neri", "non abilitato", None)
    ]


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


async def test_lock_guard_sees_grants_and_door_events(service, controller, db):
    """Rete di sicurezza della serratura: consensi dal controller e dall'app, eventi della porta, allarme."""
    from app.lock_guard import FORCED, LockGuard

    guard = LockGuard(clock=lambda: 100.0)
    service.lock_guard = guard
    # Il contatto porta al momento del consenso: aperta, chiusa o non si sa
    service.monitor.door_physical["porta2"] = "Closed"
    service.record_event(event("AccessControl/AccessGranted/Anonymous/x", {"DoorToken": "porta2"}))
    grant = guard.grants["porta2"]
    assert (grant.at, grant.extended, grant.door_open) == (100.0, False, False)
    guard.wake.clear()
    service.record_event(event("Door/State/DoorPhysicalState/$source/DoorToken/porta", {"State": "Open"}))
    assert guard.wake.is_set()
    # Recuperati dal registro o conservati dal broker: già passati, non contano
    guard.grants.clear()
    service.record_event(event("AccessControl/AccessGranted/Anonymous/x", {"DoorToken": "porta2"}), retained=True)
    service._recovering = True
    service.record_event(event("AccessControl/AccessGranted/Anonymous/x", {"DoorToken": "porta2"}))
    service._recovering = False
    assert guard.grants == {}

    service.monitor.connected, service.monitor.controller_online = True, True
    service.monitor.door_mode = "Locked"
    await service.open_door(True, "admin")
    grant = guard.grants["porta"]
    assert (grant.at, grant.extended, grant.door_open) == (100.0, True, None)

    # Senza eventi in diretta: l'ultimo consenso alla porta dal registro del controller
    service.access_point_doors = {"ap2": "porta2"}
    controller.log_events = [
        {
            "rowid": rowid,
            "UtcTime": ts,
            "KeyValues": [
                {"Key": "topic0", "Value": "AccessControl"},
                {"Key": "topic1", "Value": "AccessGranted"},
                {"Key": "AccessPointToken", "Value": point},
            ],
        }
        for rowid, ts, point in ((7, "2026-11-03T15:00:00Z", "ap2"), (8, "2026-11-03T15:00:05Z", "ap1"))
    ]
    # Orologio del controller non misurato: i secondi non si sanno
    assert await service.last_grant("porta2") == ("7", None)
    service.measured_clock_offset = lambda: 0.0
    found = await service.last_grant("porta2")
    assert found is not None and found[0] == "7" and found[1] > 0
    assert await service.last_grant("porta3") is None

    service.log_lock_alarm("porta", FORCED)
    row = db.list_events(kind="allarme")[0]
    assert (row["detail"], row["source"], row["door"]) == ("riblocco forzato", "App", "porta")
