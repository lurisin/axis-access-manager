"""Codice Stampato delle tessere (cards.printed_code, 0.28.3): quello che c'è scritto sopra la tessera fisica, messo da
chi la consegna perché le tessere sono tutte bianche («Ciccio», «1234», «A-07»). Facoltativo, unico senza badare alle
maiuscole, solo nell'app: mai sul controller (né utenti né tessere: desired.build e il confronto in ombra non cambiano)
e mai in Home Assistant. Non è un dato personale: lo vede chi vede le tessere e, accanto al numero, chi vede il
registro (il codice di adesso della tessera, il registro salva solo il numero).

Solo dati di fantasia."""

import json
import logging
import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_backup import Plant
from test_ha_mqtt import FakeClient
from test_page_time_zone import NAMES, needs_node, run_page
from test_person_details import database_0_28_2
from test_person_last_change import Clock, watch

from app import db as db_module
from app.backup import COMMON
from app.compare import compare, read_strict
from app.db import SCHEMA_VERSION, Database
from app.desired import build, load_conf
from app.events import Event, EventMonitor
from app.ha_mqtt import HaBridge
from app.main import create_app
from app.restore import Runtime, apply, plan
from app.service import AccessService, ServiceError, normalize_printed_code
from app.transfer import import_persons, persons_csv, read_import

DB_NAME = "axis_access_manager.db"
ADMIN = {"X-Remote-User-Name": "admin"}
VIEWER = {"X-Remote-User-Name": "consulta"}
LOG_ONLY = {"X-Remote-User-Name": "portiere"}
GROUPS = [{"token": "profilo_sempre", "name": "Sempre"}]
CARDS_KINDS = ["utente", "tessera"]
TS = "2026-11-13T08:00:00+00:00"


@pytest.fixture
def service(db, controller, settings):
    return AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(db_module, "now_iso", clock)
    return clock


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as client:
        client.get("/api/me", headers=ADMIN)
        client.post("/api/users", json={"username": "consulta", "perms": ["persone"]}, headers=ADMIN)
        client.post("/api/users", json={"username": "portiere", "perms": ["stato", "registro"]}, headers=ADMIN)
        yield client


def codes(db) -> dict[str, str]:
    """Codice Stampato di ogni tessera, per numero."""
    return {card["number"]: card["printed_code"] for card in db.all_cards()}


def card_of(db, number: str) -> dict:
    return next(card for card in db.all_cards() if card["number"].lower() == number.lower())


def audit_details(db) -> list[str]:
    return [row["detail"] for row in db.list_audit() if row["action"] == "codice stampato cambiato"]


def post(client, url: str, body: dict, headers=ADMIN):
    return client.post(url, json=body, headers=headers)


# Il valore


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("", ""),
        ("   ", ""),
        ("  Ciccio ", "Ciccio"),
        ("1234", "1234"),
        ("0042", "0042"),
        ("A-07", "A-07"),
        # Spazi interni compattati, anche non separabili, tabulazioni e a capo
        ("Sala   Prove", "Sala Prove"),
        ("Turno\tB\nNotte", "Turno B Notte"),
        # Caratteri invisibili di un testo incollato
        ("​Cic­cio﻿", "Ciccio"),
        ("x" * 30, "x" * 30),
        ('Tessera "Ospite"', 'Tessera "Ospite"'),
    ],
)
def test_the_code_is_cleaned_like_the_other_fields(value, expected):
    assert normalize_printed_code(value) == expected


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("x" * 31, "Codice Stampato troppo lungo (massimo 30 caratteri)"),
        ("Cic\x07cio", "Codice Stampato non valido: contiene caratteri di controllo"),
    ],
)
def test_a_too_long_code_or_control_characters_are_refused(value, message):
    with pytest.raises(ServiceError) as err:
        normalize_printed_code(value)
    assert str(err.value) == message


# Migrazione


def database_without_code(path) -> int:
    """Database allo schema 10 dei rami di prima (anagrafica senza il Codice Stampato): con una tessera."""
    db = Database(path, "0.28.3")
    person = db.add_person("Mario Rossi", "u1")
    db.add_card(person, "04AA", "cred-1")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX cards_printed_code")
    conn.execute("ALTER TABLE cards DROP COLUMN printed_code")
    conn.commit()
    conn.close()
    return person


def indexes(db: Database) -> set[str]:
    return {row["name"] for row in db._query("SELECT name FROM sqlite_master WHERE type = 'index'")}


def test_a_database_already_at_schema_10_gets_the_code(tmp_path, caplog):
    path = tmp_path / DB_NAME
    database_without_code(path)
    with caplog.at_level(logging.INFO):
        db = Database(path, "0.28.3")
    try:
        assert db.schema_version == SCHEMA_VERSION == 10
        assert codes(db) == {"04AA": ""}
        assert "cards_printed_code" in indexes(db)
        assert "Codice Stampato aggiunto alle tessere" in caplog.text
        db.set_card_printed_code(card_of(db, "04aa")["id"], "Ciccio")
    finally:
        db.close()
    # Di nuovo: niente da aggiungere, il codice resta
    caplog.clear()
    with caplog.at_level(logging.INFO):
        db = Database(path, "0.28.3")
    try:
        assert codes(db) == {"04AA": "Ciccio"}
        assert "Codice Stampato" not in caplog.text
    finally:
        db.close()


def test_a_database_of_0_28_2_gets_the_code_with_the_migration(tmp_path):
    path = tmp_path / DB_NAME
    ids = database_0_28_2(path, ["Mario Rossi"])
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO cards (person_id, number, enabled, axis_credential_token, created_at) VALUES (?, '04AA', 1, "
        "'cred-1', ?)",
        (next(iter(ids)), TS),
    )
    conn.commit()
    conn.close()
    db = Database(path, "0.28.3")
    try:
        assert db.schema_version == 10 and codes(db) == {"04AA": ""}
        assert "cards_printed_code" in indexes(db)
    finally:
        db.close()


def test_two_equal_codes_put_in_by_hand_do_not_stop_the_app(tmp_path, caplog, controller, settings):
    path = tmp_path / DB_NAME
    person = database_without_code(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE cards ADD COLUMN printed_code TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE cards SET printed_code = 'ciccio'")
    conn.execute(
        "INSERT INTO cards (person_id, number, enabled, axis_credential_token, created_at, printed_code) "
        "VALUES (?, '04BB', 1, 'cred-2', ?, 'CICCIO')",
        (person, TS),
    )
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        db = Database(path, "0.28.3")
    try:
        assert "Indice del Codice Stampato non creato" in caplog.text
        assert "cards_printed_code" not in indexes(db) and len(db.all_cards()) == 2
        # Il servizio non ne lascia scrivere un altro uguale
        service = AccessService(db, controller, settings)
        with pytest.raises(ServiceError, match="già usato dalla tessera di Mario Rossi"):
            service.check_printed_code("Ciccio")
    finally:
        db.close()


def test_the_database_refuses_the_same_code_in_other_letters(db):
    mario = db.add_person("Mario Rossi", None)
    db.add_card(mario, "04AA", "cred-1", printed_code="Ciccio")
    db.add_card(mario, "04BB", "cred-2")
    db.add_card(mario, "04CC", "cred-3")
    with pytest.raises(sqlite3.IntegrityError):
        db.set_card_printed_code(card_of(db, "04bb")["id"], "cICCIO")
    # Tessere senza codice quante se ne vuole
    assert sorted(codes(db).values()) == ["", "", "Ciccio"]


# Unicità


async def test_the_same_code_in_other_letters_names_who_has_it(service, controller, db):
    await service.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    anna = await service.create_person("Anna Bianchi", "", "admin")
    calls = list(controller.calls)
    with pytest.raises(ServiceError) as err:
        await service.add_card(anna["id"], "04bb", "admin", printed_code="  CICCIO ")
    assert str(err.value) == "Codice Stampato già usato dalla tessera di Mario Rossi"
    # Controllato prima di scrivere sul controller: niente tessera, né sul controller né nell'app
    assert controller.calls == calls and codes(db) == {"04aa": "Ciccio"}
    with pytest.raises(ServiceError, match="già usato dalla tessera di Mario Rossi"):
        await service.create_person("Ugo Neri", "04cc", "admin", printed_code="ciccio")
    assert controller.calls == calls and len(db.all_persons()) == 2
    # Lo stesso codice sulla stessa tessera non è un doppione
    card = card_of(db, "04aa")
    service.set_card_printed_code(card["id"], "ciccio", "admin")
    assert codes(db) == {"04aa": "ciccio"}


# Permessi e API


def test_who_sees_the_cards_sees_the_code_and_only_who_edits_persons_changes_it(client):
    person = post(client, "/api/persons", {"first_name": "Mario", "last_name": "Rossi", "card": "04aa"}).json()
    card = person["cards"][0]
    url = f"/api/cards/{card['id']}"
    for headers in (VIEWER, LOG_ONLY):
        assert client.patch(url, json={"printed_code": "Ciccio"}, headers=headers).status_code == 403
        assert (
            post(
                client, f"/api/persons/{person['id']}/cards", {"card": "04bb", "printed_code": "X"}, headers
            ).status_code
            == 403
        )
        body = {"first_name": "Anna", "card": "04cc", "printed_code": "Y"}
        assert post(client, "/api/persons", body, headers).status_code == 403
    assert client.patch(url, json={"printed_code": "Ciccio"}, headers=ADMIN).status_code == 200
    # Chi vede le persone vede il codice di ogni tessera, nell'elenco e nella scheda
    listed = client.get("/api/persons", headers=VIEWER).json()
    assert listed[0]["cards"][0]["printed_code"] == "Ciccio"
    assert client.get(f"/api/persons/{person['id']}", headers=VIEWER).json()["cards"][0]["printed_code"] == "Ciccio"
    assert client.get("/api/persons", headers=LOG_ONLY).status_code == 403


def test_every_write_point_of_the_page(client):
    db = client.app.state.db
    # Nuova Persona con il numero della tessera
    mario = post(
        client, "/api/persons", {"first_name": "Mario", "last_name": "Rossi", "card": "04aa", "printed_code": " 0042 "}
    ).json()
    assert mario["cards"][0]["printed_code"] == "0042"
    # Aggiungi Tessera della scheda
    response = post(client, f"/api/persons/{mario['id']}/cards", {"card": "04ab", "printed_code": "Ciccio"})
    assert response.status_code == 200 and codes(db)["04ab"] == "Ciccio"
    # Senza codice resta senza, come prima
    assert post(client, f"/api/persons/{mario['id']}/cards", {"card": "04ac"}).status_code == 200
    assert codes(db)["04ac"] == ""
    # Da Registrare: passata sul lettore, registrata con il codice appena stampato sopra
    db.note_unknown_card("04DD", TS)
    db.note_unknown_card("04EE", TS)
    body = {"first_name": "Anna", "last_name": "Bianchi", "card": "04DD", "printed_code": "A-07"}
    anna = post(client, "/api/persons", body).json()
    assert anna["cards"][0]["printed_code"] == "A-07"
    response = post(client, f"/api/persons/{anna['id']}/cards", {"card": "04EE", "printed_code": "1234"})
    assert response.status_code == 200 and codes(db)["04EE"] == "1234"
    assert db.list_unknown_cards() == []
    # Senza tessera il codice non ha dove stare
    response = post(client, "/api/persons", {"first_name": "Ugo", "printed_code": "Z9"})
    assert response.status_code == 400 and "senza tessera" in response.json()["detail"]
    assert all(p["name"] != "Ugo" for p in db.all_persons())
    # Doppione: il messaggio dice di chi è la tessera
    response = post(client, "/api/persons", {"first_name": "Ugo", "card": "04ff", "printed_code": "ciccio"})
    assert response.status_code == 400
    assert response.json()["detail"] == "Codice Stampato già usato dalla tessera di Mario Rossi"
    response = post(client, "/api/persons", {"first_name": "Ugo", "card": "04ff", "printed_code": "x" * 31})
    assert response.status_code == 400 and "massimo 30 caratteri" in response.json()["detail"]


def test_patch_of_a_card_keeps_the_old_body_and_takes_the_code(client, controller):
    db = client.app.state.db
    person = post(client, "/api/persons", {"first_name": "Mario", "last_name": "Rossi", "card": "04aa"}).json()
    card = person["cards"][0]
    url = f"/api/cards/{card['id']}"
    # Quello che manda la pagina per sospendere e riattivare
    response = client.patch(url, json={"enabled": False}, headers=ADMIN)
    assert response.status_code == 200 and response.json()["cards"][0]["enabled"] is False
    assert ("disable_credential", card["axis_credential_token"]) in controller.calls
    response = client.patch(url, json={"printed_code": "Ciccio"}, headers=ADMIN)
    assert response.status_code == 200 and response.json()["cards"][0]["printed_code"] == "Ciccio"
    # Tutti e due insieme
    response = client.patch(url, json={"enabled": True, "printed_code": "1234"}, headers=ADMIN)
    assert response.status_code == 200
    assert (card_of(db, "04aa")["enabled"], card_of(db, "04aa")["printed_code"]) == (True, "1234")
    # Un codice rifiutato non lascia mezza modifica: la tessera resta attiva, sul controller non si scrive
    calls = list(controller.calls)
    response = client.patch(url, json={"enabled": False, "printed_code": "x" * 31}, headers=ADMIN)
    assert response.status_code == 400 and controller.calls == calls and card_of(db, "04aa")["enabled"] == 1
    # Senza niente da cambiare
    assert client.patch(url, json={}, headers=ADMIN).status_code == 400
    assert client.patch("/api/cards/999", json={"printed_code": "Z"}, headers=ADMIN).status_code == 400
    # Tolto: vuoto
    assert client.patch(url, json={"printed_code": ""}, headers=ADMIN).json()["cards"][0]["printed_code"] == ""


async def test_only_the_code_never_calls_the_controller_and_does_not_wait_for_it(service, controller, db, clock):
    mario = await service.create_person("Mario Rossi", "04aa", "admin")
    card = mario["cards"][0]
    calls, reads, attempts = list(controller.calls), list(controller.reads), list(controller.attempts)
    controller.down = True
    with watch(db, clock) as step:
        # Anche mentre un'altra operazione aspetta il controller (lock preso)
        async with service.lock:
            person = await service.update_card(card["id"], "admin", printed_code="Ciccio")
    assert person["cards"][0]["printed_code"] == "Ciccio"
    assert (controller.calls, controller.reads, controller.attempts) == (calls, reads, attempts)
    # Ultima Modifica della persona e Attività con i valori
    assert step.changed() == {mario["id"]}
    assert audit_details(db) == ["Mario Rossi, codice stampato della tessera 04aa: — → Ciccio"]
    # Lo stesso codice (anche con gli spazi): niente
    with watch(db, clock) as step:
        service.set_card_printed_code(card["id"], " Ciccio ", "admin")
    assert step.changed() == set() and len(audit_details(db)) == 1
    # Cambiato e tolto
    with watch(db, clock) as step:
        service.set_card_printed_code(card["id"], "1234", "admin")
    assert step.changed() == {mario["id"]}
    service.set_card_printed_code(card["id"], "", "admin")
    assert audit_details(db)[:2] == [
        "Mario Rossi, codice stampato della tessera 04aa: 1234 → —",
        "Mario Rossi, codice stampato della tessera 04aa: Ciccio → 1234",
    ]
    assert (controller.calls, controller.attempts) == (calls, attempts)


async def test_adding_a_card_with_a_code_tells_it_in_the_activity(service, db):
    await service.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    anna = await service.create_person("Anna Bianchi", "", "admin")
    await service.add_card(anna["id"], "04bb", "admin", printed_code="A-07")
    details = [row["detail"] for row in db.list_audit()]
    assert "Mario Rossi, tessera 04aa, codice stampato Ciccio" in details
    assert "Anna Bianchi, tessera 04bb, codice stampato A-07" in details


# Sincronizzazione, Riscrivi, tessere tolte


async def test_synchronization_never_takes_the_code_away(service, controller, db, clock):
    mario = await service.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    anna = await service.create_person("Anna Bianchi", "04bb", "admin", printed_code="1234")
    token = mario["cards"][0]["axis_credential_token"]
    await service.sync_from_controller()
    assert codes(db) == {"04aa": "Ciccio", "04bb": "1234"}

    # Passata sul controller ad Anna (stessa tessera, utente diverso): la tessera fisica è la stessa
    controller.credentials[token]["UserToken"] = anna["axis_user_token"]
    await service.sync_from_controller()
    card = card_of(db, "04aa")
    assert card["person_id"] == anna["id"] and card["printed_code"] == "Ciccio"

    # Tolta sul controller e scritta di nuovo, con un token nuovo, per un utente nuovo: è sempre quella tessera
    controller.credentials.pop(token)
    controller.add_existing("utente_ugo", "Ugo Neri", "04AA")
    await service.sync_from_controller()
    card = card_of(db, "04aa")
    assert card["axis_credential_token"] != token and card["printed_code"] == "Ciccio"
    assert db.get_person(card["person_id"])["name"] == "Ugo Neri"

    # Il numero di Anna passato sul controller a un'altra tessera: il codice scritto su quella tessera passa con lui
    ugo_token = card["axis_credential_token"]
    anna_card = card_of(db, "04bb")
    controller.credentials.pop(anna_card["axis_credential_token"])
    controller.credentials[ugo_token]["IdData"] = [{"Name": "Card", "Value": "04bb"}]
    await service.sync_from_controller()
    assert codes(db) == {"04bb": "1234"} and db.card_by_token(ugo_token)["printed_code"] == "1234"

    # Un numero corretto sul controller, senza un'altra tessera nell'app: il codice resta
    controller.credentials[ugo_token]["IdData"] = [{"Name": "Card", "Value": "04bc"}]
    await service.sync_from_controller()
    assert codes(db) == {"04bc": "1234"}

    # Giri senza novità: niente cambia, neanche l'Ultima Modifica
    with watch(db, clock) as step:
        await service.sync_from_controller()
        await service.sync_from_controller()
    assert step.changed() == set() and codes(db) == {"04bc": "1234"}


async def test_rewriting_a_missing_card_keeps_the_code_and_removing_it_takes_it_away(service, controller, db):
    mario = await service.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    await service.add_card(mario["id"], "04ab", "admin", printed_code="1234")
    first = card_of(db, "04aa")
    controller.credentials.pop(first["axis_credential_token"])
    await service.sync_from_controller()
    assert card_of(db, "04aa")["missing_since"]
    # Riscrivi: token nuovo, codice uguale
    result = await service.restore_missing("admin")
    assert result["restored"] == 1
    again = card_of(db, "04aa")
    assert again["axis_credential_token"] != first["axis_credential_token"] and again["printed_code"] == "Ciccio"
    # Non presente e tolta dall'app: il codice se ne va con la riga, e un'altra tessera lo può avere
    controller.credentials.pop(again["axis_credential_token"])
    await service.sync_from_controller()
    service.forget_missing("admin", [again["id"]])
    assert codes(db) == {"04ab": "1234"}
    await service.add_card(mario["id"], "04ac", "admin", printed_code="ciccio")
    # Eliminata dalla scheda: lo stesso
    await service.delete_card(card_of(db, "04ab")["id"], "admin")
    assert codes(db) == {"04ac": "ciccio"}
    await service.add_card(mario["id"], "04ad", "admin", printed_code="1234")


# Backup e ripristino


async def restore_with(plant: Plant, saved: dict, sections=("persone",), keys=None):
    """Anteprima e ripristino da un backup dato (per esempio come l'avrebbe scritto una versione di prima)."""
    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, list(sections), plant.controller_id)
    chosen = [i["key"] for i in items if i["default"]] if keys is None else keys
    runtime = Runtime(plant.db, plant.access, plant.rules, plant.programming, plant.controller_id)
    return items, await apply(items, chosen, runtime, "admin")


def changes_of(items: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    return {i["label"]: [(c["label"], c["old"], c["new"]) for c in i["changes"]] for i in items}


async def test_the_code_is_in_new_backups_and_comes_back(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    await plant.access.add_card(mario["id"], "04ab", "admin")
    backup = await plant.backup()
    raw = json.dumps(plant.backups.load(backup["id"]))
    assert '"printed_code": "Ciccio"' in raw
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "Altro", "admin")
    plant.access.set_card_printed_code(card_of(db, "04ab")["id"], "Nuovo", "admin")
    items, result = await plant.restore(backup["id"], ["persone"])
    # Cosa Cambierà come gli altri campi della tessera, con i valori
    assert changes_of(items)["Mario Rossi"] == [
        ("Tessera 04aa: codice stampato", "Altro", "Ciccio"),
        ("Tessera 04ab: codice stampato", "Nuovo", "—"),
    ]
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio", "04ab": ""}
    # Il ripristino lo ha scritto in Attività, e sul controller non c'è niente da fare
    assert "Mario Rossi, codice stampato della tessera 04aa: Altro → Ciccio" in audit_details(db)

    # Persona eliminata e ricreata dal backup: tessere con i loro codici
    await plant.access.delete_person(mario["id"], "admin")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert ("Tessera 04aa: codice stampato", "—", "Ciccio") in changes_of(items)["Mario Rossi"]
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio", "04ab": ""}

    # Una tessera tolta dopo il backup e rimessa: anche lei con il suo codice
    await plant.access.delete_card(card_of(db, "04aa")["id"], "admin")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert ("Tessera 04aa: codice stampato", "—", "Ciccio") in changes_of(items)["Mario Rossi"]
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio", "04ab": ""}


async def test_an_old_backup_without_codes_keeps_the_current_ones(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", company="Ditta")
    await plant.access.add_card(mario["id"], "04ab", "admin")
    backup = await plant.backup()
    _, saved = plant.backups.load(backup["id"])
    for card in saved[COMMON]["cards"]:
        del card["printed_code"]
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "Ciccio", "admin")
    await plant.access.delete_card(card_of(db, "04ab")["id"], "admin")
    await plant.access.update_person(mario["id"], "admin", company="Altra")
    items, result = await restore_with(plant, saved)
    shown = changes_of(items)["Mario Rossi"]
    assert all("codice stampato" not in label for label, _, _ in shown)
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio", "04ab": ""}
    # Il confronto dei backup non lo dà per tolto
    current, _ = await plant.backups.snapshot(plant.clients)
    assert all("printed_code" not in json.dumps(i) for i in plan(saved, current, ["persone"], plant.controller_id))


async def test_a_code_given_to_another_card_stays_there_and_the_restore_goes_on(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio", company="Ditta")
    anna = await plant.access.create_person("Anna Bianchi", "04bb", "admin", printed_code="Pippo")
    backup = await plant.backup()
    # Dopo il backup: Ciccio ad Anna, e Pippo di Anna a Ugo, registrato dopo
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "Altro", "admin")
    plant.access.set_card_printed_code(card_of(db, "04bb")["id"], "Ciccio", "admin")
    await plant.access.create_person("Ugo Neri", "04cc", "admin", printed_code="Pippo")
    await plant.access.update_person(mario["id"], "admin", company="Altra")
    items, result = await plant.restore(backup["id"], ["persone"])
    rows = {i["label"]: i for i in items}
    assert rows["Mario Rossi"]["warnings"] == [
        "il Codice Stampato Ciccio della tessera 04aa ora è della tessera 04bb di Anna Bianchi: resta suo, e la "
        "tessera tiene quello di adesso"
    ]
    assert all("codice stampato" not in c["label"] for c in rows["Mario Rossi"]["changes"])
    assert rows["Anna Bianchi"]["warnings"] == [
        "il Codice Stampato Pippo della tessera 04bb ora è della tessera 04cc di Ugo Neri: torna a questa tessera "
        "solo eliminando anche quella tessera"
    ]
    # Ugo, registrato dopo il backup, non si elimina di serie: tutti tengono il loro codice, il ripristino va avanti
    assert rows["Ugo Neri"]["default"] is False
    assert not result["failed"] and codes(db) == {"04aa": "Altro", "04bb": "Ciccio", "04cc": "Pippo"}
    assert db.get_person(mario["id"])["company"] == "Ditta" and db.get_person(anna["id"])["name"] == "Anna Bianchi"


async def test_a_code_left_by_the_other_row_comes_back_when_both_are_restored(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    await plant.access.create_person("Anna Bianchi", "04bb", "admin", printed_code="Pippo")
    backup = await plant.backup()
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "", "admin")
    plant.access.set_card_printed_code(card_of(db, "04bb")["id"], "Ciccio", "admin")
    # Solo la riga di Mario: Anna tiene il codice, Mario quello di adesso, e il ripristino va avanti
    items, result = await plant.restore(
        backup["id"], ["persone"], keys=[f"persona:{mario['id']}:{mario['created_at']}"]
    )
    [row] = [i for i in items if i["label"] == "Mario Rossi"]
    assert row["warnings"] == [
        "il Codice Stampato Ciccio della tessera 04aa ora è della tessera 04bb di Anna Bianchi: torna a questa tessera "
        "solo ripristinando anche Anna Bianchi"
    ]
    assert ("Tessera 04aa: codice stampato", "—", "Ciccio") in changes_of(items)["Mario Rossi"]
    assert not result["failed"] and codes(db) == {"04aa": "", "04bb": "Ciccio"}
    # Tutte e due: tornano tutti e due, la riga di Mario dopo quella di Anna, che lascia il codice
    items, result = await plant.restore(backup["id"], ["persone"])
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio", "04bb": "Pippo"}
    assert [a["label"] for a in result["applied"]] == ["Anna Bianchi", "Mario Rossi"]


async def test_codes_of_the_same_person_moved_around_come_back_and_swapped_ones_stay(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", printed_code="X1")
    await plant.access.add_card(mario["id"], "04ab", "admin", printed_code="Y1")
    backup = await plant.backup()
    # Il codice di 04aa dato a 04ab, che ha perso il suo
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "", "admin")
    plant.access.set_card_printed_code(card_of(db, "04ab")["id"], "X1", "admin")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert [i["warnings"] for i in items] == [[]]
    assert not result["failed"] and codes(db) == {"04aa": "X1", "04ab": "Y1"}
    # Scambiati: restano come sono (come due matricole scambiate), e la riga lo dice
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "Z1", "admin")
    plant.access.set_card_printed_code(card_of(db, "04ab")["id"], "X1", "admin")
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "Y1", "admin")
    await plant.access.update_person(mario["id"], "admin", notes="Dopo il backup")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert len(items[0]["warnings"]) == 2 and all("resta suo" in w for w in items[0]["warnings"])
    assert not result["failed"] and codes(db) == {"04aa": "Y1", "04ab": "X1"}
    assert db.get_person(mario["id"])["notes"] == ""


async def test_a_code_of_a_card_added_after_the_backup_comes_back_removing_that_card(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", printed_code="Ciccio")
    backup = await plant.backup()
    plant.access.set_card_printed_code(card_of(db, "04aa")["id"], "", "admin")
    await plant.access.add_card(mario["id"], "04ab", "admin", printed_code="Ciccio")
    items, _ = await plant.restore(backup["id"], ["persone"], keys=[])
    [row] = [i for i in items if i["key"].startswith("persona:")]
    assert row["warnings"] == [
        "il Codice Stampato Ciccio della tessera 04aa ora è della tessera 04ab di Mario Rossi: torna a questa tessera "
        "solo eliminando anche quella tessera"
    ]
    keys = [i["key"] for i in items]
    _, result = await plant.restore(backup["id"], ["persone"], keys=keys)
    assert not result["failed"] and codes(db) == {"04aa": "Ciccio"}


# CSV esportato


async def test_exported_file_has_the_code_last_and_imports_as_before(service, db):
    mario = await service.create_person("Mario Rossi", "04aa", "admin", printed_code="0042", company="Ditta")
    await service.add_card(mario["id"], "04ab", "admin", printed_code='Ospite "B"')
    await service.add_card(mario["id"], "04ac", "admin", printed_code="=1+1")
    await service.add_card(mario["id"], "04ad", "admin")
    await service.create_person("Anna Bianchi", "", "admin")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses())
    lines = text.lstrip("﻿").splitlines()
    assert lines[0].split(";")[-3:] == ["matricola", "referente", "codice_stampato"]
    by_card = {line.split(";")[1]: line for line in lines[1:]}
    # Come formula di testo: il foglio di calcolo tiene gli zeri e non calcola niente
    assert by_card["04aa"].endswith(';"=""0042"""')
    # Con le virgolette non si può: testo normale; vuoto senza codice e per chi non ha tessere
    assert by_card["04ab"].endswith(';"Ospite ""B"""')
    assert by_card["04ac"].endswith(';"=""=1+1"""')
    assert by_card["04ad"].endswith(";")
    assert next(line for line in lines if line.startswith("Anna Bianchi")).endswith(";;;;;")
    rows = {row["tessera"]: row for row in read_import(text)}
    assert [rows[n]["codice_stampato"] for n in ("04aa", "04ab", "04ac", "04ad")] == ["0042", 'Ospite "B"', "=1+1", ""]
    # Reimportato com'è: niente da fare; un codice cambiato nel file non cambia niente
    result = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert result["riepilogo"]["errori"] == 0 and result["riepilogo"]["saltate"] == 5
    changed = text.replace("0042", "9999")
    result = await import_persons(service, changed, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["saltate"] == 5 and codes(db)["04aa"] == "0042"


# Registro


def test_the_log_shows_the_current_code_with_one_query_per_page(client):
    db = client.app.state.db
    mario = post(
        client, "/api/persons", {"first_name": "Mario", "last_name": "Rossi", "card": "04AA", "printed_code": "Ciccio"}
    ).json()
    post(client, f"/api/persons/{mario['id']}/cards", {"card": "04AB"})
    for n in range(60):
        db.add_event(
            ts=f"2026-11-13T08:{n:02d}:00+00:00",
            kind="concesso",
            card="04aa",
            person_id=mario["id"],
            person_name="Mario Rossi",
        )
        db.add_event(
            ts=f"2026-11-13T09:{n:02d}:00+00:00",
            kind="concesso",
            card="04AB",
            person_id=mario["id"],
            person_name="Mario Rossi",
        )
    db.add_event(ts="2026-11-13T10:00:00+00:00", kind="sconosciuta", card="04FF")
    db.add_event(ts="2026-11-13T10:01:00+00:00", kind="apertura_remota", source="Home Assistant")
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    try:
        events = client.get("/api/events?limit=200", headers=LOG_ONLY).json()
    finally:
        db._conn.set_trace_callback(None)
    assert len(events) == 122
    assert len([s for s in statements if "printed_code" in s]) == 1
    by_card = {(e["card"], e["printed_code"]) for e in events}
    # Il numero del registro anche scritto in un altro modo: il codice è quello della tessera di adesso
    assert by_card == {("04aa", "Ciccio"), ("04AB", ""), ("04FF", ""), ("", "")}
    # Codice cambiato: il registro mostra quello di adesso
    client.patch(f"/api/cards/{mario['cards'][0]['id']}", json={"printed_code": "1234"}, headers=ADMIN)
    event = next(e for e in client.get("/api/events?limit=5&kind=concesso", headers=LOG_ONLY).json())
    detail = client.get(f"/api/events/{events[-1]['id']}", headers=LOG_ONLY).json()
    assert detail["card"] == "04aa" and detail["printed_code"] == "1234"
    assert event["printed_code"] == ""  # l'ultimo concesso è della 04AB, senza codice
    # Anche nel cruscotto, per il Dettaglio Evento
    recent = client.get("/api/dashboard", headers=ADMIN).json()["recent"]
    assert recent and all("printed_code" in e for e in recent)


# Le righe e il Dettaglio Evento della pagina, com'è: il codice è testo scritto da chi usa l'app, sempre come testo
PAGE_NAMES = (
    *NAMES,
    "esc",
    "LOG_ROW_WHAT",
    "LOG_SOURCES",
    "DETAIL_HINTS",
    "KIND_DOT",
    "props",
    "logRow",
    "eventDetail",
)
PAGE_STUBS = """
const KINDS = {concesso: ["Accesso Concesso", "k-ok", "check"], sconosciuta: ["Tessera Sconosciuta", "k-warn", "q"]};
let logSelected = 0;
const multiDoor = () => false, inputDenied = () => false, icon = () => "", can = () => true, doorByToken = () => null;
const allDoors = () => [], controllerEvent = () => false, placeOf = () => "", eventPlace = () => "";
"""


@needs_node
def test_the_page_shows_the_code_next_to_the_card_in_the_log_as_text():
    named = {"id": 1, "ts": TS, "kind": "concesso", "card": "04aa", "person_id": 3, "person_name": "Mario Rossi"}
    unknown = {"id": 2, "ts": TS, "kind": "sconosciuta", "card": "04ff", "person_name": ""}
    out = run_page(
        "Europe/Rome",
        {
            "row": f"logRow({json.dumps({**named, 'printed_code': '<b>Ciccio</b>'})})",
            "plain": f"logRow({json.dumps({**named, 'printed_code': ''})})",
            "card": f"logRow({json.dumps({**unknown, 'printed_code': '1234'})})",
            "detail": f"eventDetail({json.dumps({**named, 'printed_code': '<b>Ciccio</b>'})})",
            "without": f"eventDetail({json.dumps(named)})",
        },
        names=PAGE_NAMES,
        prelude=PAGE_STUBS,
    )
    assert (
        '<span class="pcode" title="Codice Stampato della tessera 04aa">&lt;b&gt;Ciccio&lt;/b&gt;</span>' in out["row"]
    )
    assert "<b>Ciccio" not in out["row"] and "pcode" not in out["plain"]
    # Accanto al numero di una tessera senza persona
    chip = '<span class="pcode" title="Codice Stampato della tessera 04ff">1234</span>'
    assert f"<b>Tessera 04ff</b> {chip}" in out["card"]
    assert "<dt>Codice Stampato</dt><dd>&lt;b&gt;Ciccio&lt;/b&gt; <span" in out["detail"]
    assert "quello di adesso della tessera" in out["detail"] and "Codice Stampato" not in out["without"]


# Controller e Home Assistant


async def test_controller_and_shadow_comparison_never_see_the_code(plant, controller, db, settings):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin")
    await plant.access.add_card(mario["id"], "04ab", "admin")
    before = build(load_conf(db, settings.access_profile), 1, CARDS_KINDS)
    reading = await read_strict(controller, CARDS_KINDS)
    calls = list(controller.calls)
    for card in db.get_person(mario["id"])["cards"]:
        plant.access.set_card_printed_code(card["id"], f"Codice {card['number']}", "admin")
    after = build(load_conf(db, settings.access_profile), 1, CARDS_KINDS)
    assert after == before
    assert compare(after, reading) == compare(before, reading)
    assert {row["action"] for row in compare(after, reading)["rows"]} == {"uguale"}
    assert controller.calls == calls
    assert "Codice 04" not in json.dumps([controller.users, controller.credentials])


async def test_home_assistant_never_gets_the_code(service, controller, db, settings, monkeypatch):
    person = await service.create_person("Mario Rossi", "04aa", "admin", printed_code="CodiceSegreto")
    token = person["cards"][0]["axis_credential_token"]
    at = datetime(2026, 11, 13, 8, tzinfo=UTC)
    service.record_event(Event("IdPoint", "IdPoint/Request/IdData/x", at, {"Card": "04aa"}))
    service.record_event(
        Event("AccessControl", "AccessControl/AccessGranted/Credential/x", at, {"CredentialToken": token})
    )
    bridge = HaBridge(settings, service.monitor, db)
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, payload))
        return True

    monkeypatch.setattr(service.monitor, "publish", publish)
    rows = db.list_events()
    # Il registro salva solo il numero: il codice non è nelle righe che arrivano a Home Assistant
    assert rows and all("printed_code" not in row for row in rows)
    for row in rows:
        await bridge.publish_event(row)
    client = FakeClient()
    await bridge.on_connect(client)
    published = json.dumps([[t, str(p)] for t, p in sent] + [[t, str(p)] for t, p in client.published.items()])
    assert "Mario Rossi" in published and "CodiceSegreto" not in published
