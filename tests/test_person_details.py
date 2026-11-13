"""Anagrafica della 0.28.3: nome e cognome separati, telefono ed email. Il nome completo (persons.name) non cambia mai
per le persone che ci sono, ed è sempre «Nome Cognome»: sul controller, nel registro e in Home Assistant resta tutto
com'era. Telefono ed email restano nell'app: mai sul controller, in Home Assistant, nel registro o nel log."""

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_backup import Plant
from test_ha_mqtt import FakeClient
from test_page_time_zone import needs_node, run_page

from app import db as db_module
from app.backup import COMMON, compare_parts
from app.compare import compare, read_strict
from app.db import SCHEMA_VERSION, Database, compose_name, nocase, split_name
from app.desired import build, load_conf
from app.events import Event, EventMonitor
from app.ha_mqtt import HaBridge
from app.main import create_app
from app.restore import Runtime, apply, plan, public
from app.service import (
    AccessService,
    ServiceError,
    new_credential,
    normalize_email,
    normalize_employee_id,
    normalize_phone,
    normalize_referent,
)
from app.transfer import import_persons, persons_csv, read_import

DB_NAME = "axis_access_manager.db"
TS = "2026-10-01T08:00:00+00:00"
PHONE = "+39 333 987 6543"
EMAIL = "riservata@example.it"
EMPLOYEE_ID = "MAT-000917"
REFERENT = "Dott.ssa Ines Fantasia"
GROUPS = [{"token": "profilo_sempre", "name": "Sempre"}]
ACCESS = {"access_profile": "profilo_sempre", "valid_from": "", "valid_to": ""}
CARDS_KINDS = ["utente", "tessera"]


def assert_invariant(db: Database) -> None:
    """Il nome completo di ogni persona è nome e cognome ricomposti."""
    for person in db.all_persons():
        assert compose_name(person["first_name"], person["last_name"]) == person["name"], person


def parts(db: Database) -> dict[str, tuple[str, str]]:
    return {p["name"]: (p["first_name"], p["last_name"]) for p in db.all_persons()}


@pytest.fixture
def service(db, controller, settings):
    return AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


# La regola di divisione


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Anna", ("Anna", "")),
        ("Mario Rossi", ("Mario", "Rossi")),
        ("Maria Rosa Bianchi", ("Maria", "Rosa Bianchi")),
        # Come arrivano dal controller (solo senza spazi ai lati): interi nel nome
        ("Luca  Verdi", ("Luca  Verdi", "")),
        ("Ugo\tNeri", ("Ugo\tNeri", "")),
        (" Eva Blu", (" Eva Blu", "")),
        ("(Senza nome)", ("(Senza nome)", "")),
        ("", ("", "")),
    ],
)
def test_split_name_never_changes_the_full_name(name, expected):
    assert split_name(name) == expected
    assert compose_name(*split_name(name)) == name


def test_database_refuses_names_that_do_not_make_the_full_name(db):
    with pytest.raises(ValueError):
        db.add_person("Mario Rossi", None, first_name="Mario", last_name="Bianchi")
    person = db.add_person("Mario Rossi", None)
    with pytest.raises(ValueError):
        db.rename_person(person, "Mario Rossi", "Mario Rossi", "Rossi")
    db.rename_person(person, "Maria Rosa Bianchi")
    assert parts(db) == {"Maria Rosa Bianchi": ("Maria", "Rosa Bianchi")}


# Migrazione 10 (0.28.3) e riparazione all'avvio

V0282_SCHEMA = 9
OLD_NAMES = ["Anna", "Mario Rossi", "Maria Rosa Bianchi", "Luca  Verdi", "(Senza nome)", "Ugo\tNeri"]


def database_0_28_2(path: Path, names=OLD_NAMES) -> dict[int, str]:
    """Database come lo lascia la 0.28.2: schema 9, persone con il solo nome completo (anche come arrivano dal
    controller, con spazi doppi o tabulazioni)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db_module, "SCHEMA_STEPS", [s for s in db_module.SCHEMA_STEPS if s[0] <= V0282_SCHEMA])
        patch.setattr(db_module, "SCHEMA_VERSION", V0282_SCHEMA)
        old = Database(path, "0.28.2")
    ids = {}
    with old._lock:
        for n, name in enumerate(names):
            cursor = old._conn.execute(
                "INSERT INTO persons (name, axis_user_token, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, f"utente_{n}", TS, TS),
            )
            ids[cursor.lastrowid] = name
        old._conn.commit()
    old.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V0282_SCHEMA
        assert "first_name" not in {row[1] for row in conn.execute("PRAGMA table_info(persons)")}
    finally:
        conn.close()
    return ids


def test_database_of_0_28_2_gets_first_and_last_names_without_changing_names(tmp_path):
    path = tmp_path / DB_NAME
    ids = database_0_28_2(path)
    db = Database(path, "0.28.3")
    assert db.schema_version == SCHEMA_VERSION == 10
    persons = {p["id"]: p for p in db.all_persons()}
    # Il nome completo di ogni persona è quello di prima, carattere per carattere
    assert {i: p["name"] for i, p in persons.items()} == ids
    assert parts(db) == {
        "Anna": ("Anna", ""),
        "Mario Rossi": ("Mario", "Rossi"),
        "Maria Rosa Bianchi": ("Maria", "Rosa Bianchi"),
        "Luca  Verdi": ("Luca  Verdi", ""),
        "(Senza nome)": ("(Senza nome)", ""),
        "Ugo\tNeri": ("Ugo\tNeri", ""),
    }
    assert {(p["phone"], p["email"]) for p in persons.values()} == {("", "")}
    db.close()
    # La copia prima della migrazione è il database com'era
    assert db.copied.endswith("_v0.28.2.db")
    copy = sqlite3.connect(tmp_path / "copie_database" / db.copied)
    try:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == V0282_SCHEMA
        assert "first_name" not in {row[1] for row in copy.execute("PRAGMA table_info(persons)")}
        assert sorted(row[0] for row in copy.execute("SELECT name FROM persons")) == sorted(OLD_NAMES)
    finally:
        copy.close()
    # Già migrato: niente copia, niente da riparare, tutto uguale
    db = Database(path, "0.28.3")
    assert db.copied == "" and {p["id"]: p["name"] for p in db.all_persons()} == ids
    assert_invariant(db)
    db.close()


def test_migration_runs_once_even_on_a_database_put_back_to_schema_9(tmp_path):
    """Un database riportato a mano allo schema 9 ha già le colonne: la migrazione non le aggiunge due volte e non
    tocca nome e cognome che valgono già."""
    path = tmp_path / DB_NAME
    db = Database(path, "0.28.3")
    db.add_person("Maria Rosa Bianchi", None, first_name="Maria Rosa", last_name="Bianchi", phone=PHONE)
    db._execute("PRAGMA user_version = 9")
    db.close()
    db = Database(path, "0.28.3")
    assert db.schema_version == SCHEMA_VERSION
    [person] = db.all_persons()
    assert (person["first_name"], person["last_name"], person["phone"]) == ("Maria Rosa", "Bianchi", PHONE)
    db.close()


def test_rows_left_by_an_older_version_are_repaired_at_startup(tmp_path, caplog):
    """Ritorno alla 0.28.2 e di nuovo alla 0.28.3: le persone che la 0.28.2 ha rinominato o creato non hanno nome e
    cognome giusti. All'avvio si ricavano dal nome completo, che non cambia; le altre restano come sono."""
    path = tmp_path / DB_NAME
    db = Database(path, "0.28.3")
    db.add_person("Maria Rosa Bianchi", "u1", first_name="Maria Rosa", last_name="Bianchi")
    renamed = db.add_person("Mario Rossi", "u2")
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE persons SET name = 'Mario Bianchi Rossi' WHERE id = ?", (renamed,))
    conn.execute(
        "INSERT INTO persons (name, created_at, updated_at) VALUES ('Nuova Persona', ?, ?), ('Ospite', ?, ?)",
        (TS, TS, TS, TS),
    )
    conn.execute("UPDATE app_settings SET value = '0.28.2' WHERE key = 'app_version'")
    conn.commit()
    conn.close()

    with caplog.at_level(logging.WARNING):
        db = Database(path, "0.28.3")
    assert parts(db) == {
        "Maria Rosa Bianchi": ("Maria Rosa", "Bianchi"),
        "Mario Bianchi Rossi": ("Mario", "Bianchi Rossi"),
        "Nuova Persona": ("Nuova", "Persona"),
        "Ospite": ("Ospite", ""),
    }
    # Nel log quante, mai i nomi
    assert "Nome e cognome di 3 persone ricavati" in caplog.text
    assert "Mario" not in caplog.text and "Ospite" not in caplog.text
    db.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        Database(path, "0.28.3").close()
    assert "Nome e cognome" not in caplog.text


async def test_the_controller_gets_the_same_names_before_and_after_the_migration(db, controller, settings):
    """Stato voluto (desired.build) e confronto in ombra di utenti e tessere identici prima e dopo la migrazione, per le
    stesse persone: nomi di una, due e tre parole, e un nome con lo spazio doppio cambiato a mano sul controller."""
    service = AccessService(db, controller, settings)
    for name, card in (("Anna", "04a1"), ("Mario Rossi", "04a2"), ("Maria Rosa Bianchi", "04a3")):
        await service.create_person(name, card, "admin")
    luca = await service.create_person("Luca Verdi", "04a4", "admin")
    # Rinominato a mano sul controller, come fa l'app: utente e descrizione della tessera
    controller.users[luca["axis_user_token"]]["Name"] = "Luca  Verdi"
    controller.credentials[luca["cards"][0]["axis_credential_token"]]["Description"] = "Luca  Verdi"
    await service.sync_from_controller()
    assert "Luca  Verdi" in parts(db)
    before = build(load_conf(db, settings.access_profile), 1, CARDS_KINDS)
    reading = await read_strict(controller, CARDS_KINDS)
    assert {row["action"] for row in compare(before, reading)["rows"]} == {"uguale"}
    path = db.path
    db.close()
    # Lo stesso database com'era con la 0.28.2: senza le colonne della migrazione 10, l'indice della matricola e le foto
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX persons_employee_id")
    conn.execute("DROP TABLE person_photos")
    for column, _ in db_module.PERSON_COLUMNS:
        conn.execute(f"ALTER TABLE persons DROP COLUMN {column}")
    conn.execute(f"PRAGMA user_version = {V0282_SCHEMA}")
    conn.commit()
    conn.close()

    migrated = Database(path, "0.28.3")
    try:
        after = build(load_conf(migrated, settings.access_profile), 1, CARDS_KINDS)
        assert after == before
        assert compare(after, reading) == compare(before, reading)
        names = {p["Name"] for p in after["objects"]["utente"].values()}
        assert names == {"Anna", "Mario Rossi", "Maria Rosa Bianchi", "Luca  Verdi"}
        assert_invariant(migrated)
    finally:
        migrated.close()


# Ogni percorso che crea o rinomina persone


async def test_new_person_with_first_and_last_name(service, controller, db):
    maria = await service.create_person(
        "", "04aa", "admin", first_name="  Maria   Rosa ", last_name=" Bianchi ", phone=f" {PHONE} ", email=EMAIL
    )
    assert (maria["name"], maria["first_name"], maria["last_name"]) == ("Maria Rosa Bianchi", "Maria Rosa", "Bianchi")
    assert (maria["phone"], maria["email"]) == (PHONE, EMAIL)
    user = controller.users[maria["axis_user_token"]]
    credential = controller.credentials[maria["cards"][0]["axis_credential_token"]]
    assert user["Name"] == credential["Description"] == "Maria Rosa Bianchi"
    # Solo il cognome, o solo il nome
    rossi = await service.create_person("", "", "admin", last_name="Rossi")
    assert (rossi["name"], rossi["first_name"], rossi["last_name"]) == ("Rossi", "", "Rossi")
    # Solo il nome completo, come le richieste delle versioni precedenti: diviso al primo spazio
    luca = await service.create_person(" Luca   Verdi  Neri ", "", "admin")
    assert (luca["name"], luca["first_name"], luca["last_name"]) == ("Luca Verdi Neri", "Luca", "Verdi Neri")
    with pytest.raises(ServiceError, match="almeno uno"):
        await service.create_person("Mario", "", "admin", first_name=" ", last_name="")
    with pytest.raises(ServiceError, match="100 caratteri"):
        await service.create_person("", "", "admin", first_name="A" * 60, last_name="B" * 40)
    assert_invariant(db)
    secrets = json.dumps([controller.users, controller.credentials, db.list_audit()])
    # Il telefono con e senza spazi (non «333», che può capitare nei token casuali del controller)
    assert "987 6543" not in secrets and "9876543" not in secrets and EMAIL not in secrets


async def test_changing_first_or_last_name_renames_on_the_controller(service, controller, db):
    person = await service.create_person("", "04aa", "admin", first_name="Maria", last_name="Rosa Bianchi")
    token = person["axis_user_token"]
    credential = person["cards"][0]["axis_credential_token"]

    # Solo la divisione: stesso nome completo, niente sul controller
    controller.calls.clear()
    person = await service.update_person(person["id"], "admin", first_name="Maria Rosa", last_name="Bianchi")
    assert (person["name"], person["first_name"], person["last_name"]) == (
        "Maria Rosa Bianchi",
        "Maria Rosa",
        "Bianchi",
    )
    assert controller.calls == []
    assert db.list_audit()[0]["detail"] == "Maria Rosa Bianchi: nome e cognome"

    # Il cognome cambia: cambia il nome completo, anche sul controller
    person = await service.update_person(person["id"], "admin", last_name="Verdi")
    assert (person["name"], person["first_name"], person["last_name"]) == ("Maria Rosa Verdi", "Maria Rosa", "Verdi")
    assert controller.users[token]["Name"] == "Maria Rosa Verdi"
    assert controller.credentials[credential]["Description"] == "Maria Rosa Verdi"
    assert db.list_audit()[0]["action"] == "persona rinominata"

    # Solo il nome completo (richieste delle versioni precedenti): diviso al primo spazio
    person = await service.update_person(person["id"], "admin", name="Anna Maria Neri")
    assert (person["first_name"], person["last_name"]) == ("Anna", "Maria Neri")
    # Lo stesso nome completo non cambia la divisione
    await service.update_person(person["id"], "admin", first_name="Anna Maria", last_name="Neri")
    person = await service.update_person(person["id"], "admin", name="Anna Maria Neri")
    assert (person["first_name"], person["last_name"]) == ("Anna Maria", "Neri")

    # Telefono ed email: solo nell'app
    controller.calls.clear()
    person = await service.update_person(person["id"], "admin", phone=PHONE, email=EMAIL)
    assert (person["phone"], person["email"]) == (PHONE, EMAIL) and controller.calls == []
    assert assert_no_secret(db.list_audit())
    assert db.list_audit()[0]["detail"] == "Anna Maria Neri: telefono, email"
    # Un valore non valido non lascia mezza modifica
    with pytest.raises(ServiceError):
        await service.update_person(person["id"], "admin", company="Teatro", email="non valida")
    assert db.get_person(person["id"])["company"] == ""
    assert_invariant(db)


def assert_no_secret(value) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return PHONE not in text and "9876543" not in text.replace(" ", "") and EMAIL not in text


async def test_name_read_from_the_controller_is_not_rewritten_by_saving_the_same_parts(service, controller, db):
    """Nome con lo spazio doppio arrivato dal controller: salvare i Dati con nome e cognome com'erano (per esempio per
    aggiungere il telefono) non lo riscrive sul controller."""
    person = await service.create_person("Luca Verdi", "04aa", "admin")
    controller.users[person["axis_user_token"]]["Name"] = "Luca  Verdi"
    await service.sync_from_controller()
    person = db.get_person(person["id"])
    assert (person["name"], person["first_name"], person["last_name"]) == ("Luca  Verdi", "Luca  Verdi", "")
    controller.calls.clear()
    person = await service.update_person(
        person["id"], "admin", first_name="Luca  Verdi", last_name="", phone=PHONE, company="Teatro"
    )
    assert person["name"] == "Luca  Verdi" and person["phone"] == PHONE and controller.calls == []


async def test_persons_read_from_the_controller_are_split_by_the_rule(service, controller, db):
    person = await service.create_person("Paolo Neri", "04aa", "admin")
    # Utente e tessera fatti fuori dall'app, e una tessera senza utente né descrizione
    await controller.set_user({"token": "esterno", "Name": "Maria Rosa Bianchi", "Description": "", "Attribute": []})
    await controller.set_credential(new_credential("esterno", "Maria Rosa Bianchi", "04bb", ACCESS))
    await controller.set_credential(new_credential("", "", "04cc", ACCESS))
    controller.users[person["axis_user_token"]]["Name"] = "Paolo Neri Bianchi"
    await service.sync_from_controller()
    assert parts(db) == {
        "Paolo Neri Bianchi": ("Paolo", "Neri Bianchi"),
        "Maria Rosa Bianchi": ("Maria", "Rosa Bianchi"),
        "(Senza nome)": ("(Senza nome)", ""),
    }
    # Rinominato di nuovo sul controller, con le tabulazioni: intero nel nome
    controller.users[person["axis_user_token"]]["Name"] = "Paolo\tNeri"
    await service.sync_from_controller()
    assert parts(db)["Paolo\tNeri"] == ("Paolo\tNeri", "")
    assert_invariant(db)


async def test_import_splits_the_full_name_and_registration_from_the_reader(service, controller, db):
    rows = await import_persons(service, "nome;tessera\nMaria Rosa Bianchi;04aa\nAnna;", GROUPS, "admin", True)
    assert [r["esito"] for r in rows["righe"]] == ["ok", "ok"]
    assert parts(db) == {"Maria Rosa Bianchi": ("Maria", "Rosa Bianchi"), "Anna": ("Anna", "")}
    assert_invariant(db)


def test_registration_from_da_registrare_and_the_old_requests(settings, controller, tmp_path, monkeypatch):
    """Da Registrare manda nome e cognome con il numero della tessera letta; una richiesta con il solo nome completo
    (versioni precedenti, script) vale ancora."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as client:
        db = client.app.state.db
        db.note_unknown_card("04dd", TS)
        body = {"first_name": "Gian Luca", "last_name": "De Santis", "card": "04dd", "phone": PHONE}
        person = client.post("/api/persons", json=body).json()
        assert (person["name"], person["first_name"], person["last_name"]) == (
            "Gian Luca De Santis",
            "Gian Luca",
            "De Santis",
        )
        assert db.list_unknown_cards() == []
        old = client.post("/api/persons", json={"name": "Mario Rossi", "card": "04ee"}).json()
        assert (old["first_name"], old["last_name"], old["phone"], old["email"]) == ("Mario", "Rossi", "", "")
        response = client.patch(f"/api/persons/{old['id']}", json={"name": "Mario Rossi Bianchi"})
        assert (response.json()["first_name"], response.json()["last_name"]) == ("Mario", "Rossi Bianchi")
        response = client.post("/api/persons", json={"first_name": "", "last_name": ""})
        assert response.status_code == 400 and "almeno uno" in response.json()["detail"]
        listed = {p["name"]: p for p in client.get("/api/persons").json()}
        assert listed["Gian Luca De Santis"]["phone"] == PHONE and listed["Mario Rossi Bianchi"]["email"] == ""
        assert_invariant(db)


# Ripristino dai backup dell'app


async def test_restore_brings_back_names_phone_and_email(plant, controller, db):
    maria = await plant.access.create_person(
        "", "04aa", "admin", first_name="Maria Rosa", last_name="Bianchi", phone=PHONE, email=EMAIL
    )
    backup = await plant.backup()
    await plant.access.update_person(maria["id"], "admin", first_name="Maria", last_name="Rosa Bianchi", phone="")
    controller.calls.clear()

    items, result = await plant.restore(backup["id"], ["persone"])

    [item] = items
    assert [(c["label"], c["old"], c["new"]) for c in item["changes"]] == [
        ("Nome", "Maria Rosa Bianchi", "Maria Rosa Bianchi (nome Maria Rosa, cognome Bianchi)"),
        ("Telefono", "—", "indicato"),
    ]
    # Alla pagina vanno solo i campi pubblici delle righe: la persona del backup resta nel server
    assert assert_no_secret(public(items)) and not result["failed"]
    restored = db.get_person(maria["id"])
    assert (restored["first_name"], restored["last_name"], restored["phone"], restored["email"]) == (
        "Maria Rosa",
        "Bianchi",
        PHONE,
        EMAIL,
    )
    # Stesso nome completo: sul controller niente
    assert controller.calls == []
    items, _ = await plant.restore(backup["id"], ["persone"])
    assert items == []

    # Eliminata e ricreata dal backup: tutto come prima
    await plant.access.delete_person(maria["id"], "admin")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert [i["action"] for i in items] == ["ricrea"] and not result["failed"]
    again = db.get_person(maria["id"])
    assert (again["name"], again["first_name"], again["last_name"], again["phone"], again["email"]) == (
        "Maria Rosa Bianchi",
        "Maria Rosa",
        "Bianchi",
        PHONE,
        EMAIL,
    )
    assert controller.users[again["axis_user_token"]]["Name"] == "Maria Rosa Bianchi"
    assert_invariant(db)


async def test_backup_of_0_28_2_restores_names_from_the_full_name(plant, controller, db):
    """Un backup della 0.28.2 non ha nome e cognome né telefono ed email: nome e cognome dal nome completo, al primo
    spazio, e telefono ed email restano quelli di adesso."""
    anna = await plant.access.create_person("Anna Rossi", "04aa", "admin")
    backup = await plant.backup()
    await plant.access.update_person(anna["id"], "admin", first_name="Anna", last_name="Bianchi", phone=PHONE)
    _, saved = plant.backups.load(backup["id"])
    for person in saved[COMMON]["persons"]:
        for key in ("first_name", "last_name", "phone", "email"):
            del person[key]
    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)

    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = plan(saved, current, ["persone"], plant.controller_id)
    assert [(c["label"], c["old"], c["new"]) for c in item["changes"]] == [("Nome", "Anna Bianchi", "Anna Rossi")]
    result = await apply([item], [item["key"]], runtime, "admin")
    assert not result["failed"]
    restored = db.get_person(anna["id"])
    assert (restored["name"], restored["first_name"], restored["last_name"], restored["phone"]) == (
        "Anna Rossi",
        "Anna",
        "Rossi",
        PHONE,
    )
    assert controller.users[restored["axis_user_token"]]["Name"] == "Anna Rossi"

    await plant.access.delete_person(anna["id"], "admin")
    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = plan(saved, current, ["persone"], plant.controller_id)
    await apply([item], [item["key"]], runtime, "admin")
    again = db.get_person(anna["id"])
    assert (again["first_name"], again["last_name"], again["phone"], again["email"]) == ("Anna", "Rossi", "", "")
    assert_invariant(db)


@pytest.mark.parametrize("old_backup", [False, True])
async def test_restore_keeps_a_name_read_from_the_controller(plant, controller, db, old_backup):
    """Nome con lo spazio doppio arrivato dal controller: ripristinare la persona per un'altra modifica (la tessera
    sospesa) non riscrive il nome sul controller, con un backup nuovo e con uno della 0.28.2."""
    luca = await plant.access.create_person("Luca Verdi", "04aa", "admin")
    card = luca["cards"][0]
    controller.users[luca["axis_user_token"]]["Name"] = "Luca  Verdi"
    controller.credentials[card["axis_credential_token"]]["Description"] = "Luca  Verdi"
    await plant.access.sync_from_controller()
    backup = await plant.backup()
    await plant.access.set_card_enabled(card["id"], False, "admin")
    _, saved = plant.backups.load(backup["id"])
    if old_backup:
        for person in saved[COMMON]["persons"]:
            for key in ("first_name", "last_name", "phone", "email"):
                del person[key]
    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = plan(saved, current, ["persone"], plant.controller_id)
    assert [c["label"] for c in item["changes"]] == ["Tessera 04aa: stato"]
    controller.calls.clear()

    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    result = await apply([item], [item["key"]], runtime, "admin")

    assert not result["failed"]
    assert [call[0] for call in controller.calls] == ["enable_credential"]
    person = db.get_person(luca["id"])
    assert (person["name"], person["first_name"], person["last_name"]) == ("Luca  Verdi", "Luca  Verdi", "")


def without_parts(saved: dict) -> None:
    """Il backup come l'avrebbe fatto la 0.28.2: senza nome e cognome, telefono ed email."""
    for person in saved[COMMON]["persons"]:
        for key in ("first_name", "last_name", "phone", "email"):
            del person[key]


@pytest.mark.parametrize("old_backup", [False, True])
async def test_restore_of_a_name_with_double_spaces_keeps_the_split_of_the_rule(plant, controller, db, old_backup):
    """Backup con il nome letto dal controller con lo spazio doppio, «Luca  Verdi» (tutto nel nome); poi la persona è
    diventata «Luca Verdi», nome Luca e cognome Verdi. Ripristinarla non cambia niente, come nella 0.28.2 (il nome
    del backup compattato è quello di adesso): non le toglie il cognome lasciando tutto nel nome."""
    controller.add_existing("utente_luca", "Luca  Verdi", "04aa")
    await plant.access.sync_from_controller()
    [luca] = db.all_persons()
    assert (luca["first_name"], luca["last_name"]) == ("Luca  Verdi", "")
    backup = await plant.backup()
    await plant.access.update_person(luca["id"], "admin", first_name="Luca", last_name="Verdi")
    before = db.get_person(luca["id"])
    assert (before["name"], before["first_name"], before["last_name"]) == ("Luca Verdi", "Luca", "Verdi")
    _, saved = plant.backups.load(backup["id"])
    if old_backup:
        without_parts(saved)
    controller.calls.clear()

    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, ["persone"], plant.controller_id)
    [person] = saved[COMMON]["persons"]
    await plant.access.restore_person(person, [], 0, "admin")

    after = db.get_person(luca["id"])
    assert (after["name"], after["first_name"], after["last_name"]) == ("Luca Verdi", "Luca", "Verdi")
    assert after["updated_at"] == before["updated_at"] and controller.calls == []
    assert "nome e cognome" not in json.dumps(db.list_audit())
    # E Cosa Cambierà non lo propone: non dice «Luca Verdi» → «Luca  Verdi», che il ripristino non fa
    assert items == []


@pytest.mark.parametrize("old_backup", [False, True])
async def test_recreated_name_with_double_spaces_is_split_like_a_new_one(plant, controller, db, old_backup):
    """Persona eliminata, nel backup con il nome letto dal controller con lo spazio doppio: torna con il nome
    compattato, come nella 0.28.2, e con nome e cognome della regola, come se la si scrivesse a mano."""
    controller.add_existing("utente_luca", "Luca  Verdi", "04aa")
    await plant.access.sync_from_controller()
    [luca] = db.all_persons()
    backup = await plant.backup()
    await plant.access.delete_person(luca["id"], "admin")
    _, saved = plant.backups.load(backup["id"])
    if old_backup:
        without_parts(saved)
    current, _ = await plant.backups.snapshot(plant.clients)
    [item] = plan(saved, current, ["persone"], plant.controller_id)
    assert item["action"] == "ricrea"

    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    result = await apply([item], [item["key"]], runtime, "admin")

    assert not result["failed"]
    [again] = db.all_persons()
    assert (again["name"], again["first_name"], again["last_name"]) == ("Luca Verdi", "Luca", "Verdi")
    assert controller.users[again["axis_user_token"]]["Name"] == "Luca Verdi"
    assert_invariant(db)


async def test_backup_comparison_says_phone_and_email_changed_without_the_values(plant, db):
    anna = await plant.access.create_person("", "04aa", "admin", first_name="Anna", last_name="Rossi", phone=PHONE)
    await plant.backup()
    await plant.access.update_person(anna["id"], "admin", phone="06 1234567", email=EMAIL)
    second = await plant.backup()

    result = await plant.backups.compare(second["id"], "precedente", plant.clients)

    assert assert_no_secret(result) and "1234567" not in json.dumps(result)
    fields = [
        (f["label"], f["old"], f["new"])
        for group in result["groups"]
        for section in group["sections"]
        for changed in section["changed"]
        for f in changed["fields"]
    ]
    assert fields == [("Telefono", "indicato", "cambiato"), ("Email", "—", "indicato")]


async def test_backup_of_0_28_2_against_a_new_one_shows_only_what_changed(plant, db):
    """Il primo backup dopo l'aggiornamento confrontato con uno della 0.28.2 (senza nome e cognome, telefono ed email):
    nessuna persona risulta cambiata per la divisione del nome fatta dalla migrazione; cambiata solo la divisione fatta
    a mano e il telefono aggiunto."""
    await plant.access.create_person("Maria Rosa Bianchi", "04aa", "admin")
    await plant.access.create_person("", "", "admin", first_name="Anna Maria", last_name="Neri", phone=PHONE)
    await plant.access.create_person("Ospite", "", "admin")
    backup = await plant.backup()
    _, new = plant.backups.load(backup["id"])
    old = json.loads(json.dumps(new))
    for person in old[COMMON]["persons"]:
        for key in ("first_name", "last_name", "phone", "email"):
            del person[key]

    groups = compare_parts(old, new)

    [persons] = [s for g in groups for s in g["sections"] if s["key"] == "persone"]
    assert persons["added"] == persons["removed"] == []
    assert [(c["label"], [(f["label"], f["old"], f["new"]) for f in c["fields"]]) for c in persons["changed"]] == [
        (
            "Anna Maria Neri",
            [
                ("Nome", "Anna Maria Neri", "Anna Maria Neri (nome Anna Maria, cognome Neri)"),
                ("Telefono", "—", "indicato"),
            ],
        )
    ]
    assert assert_no_secret(groups)


# Telefono ed email


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("   ", ""),
        (f"  {PHONE} ", PHONE),
        ("(06) 123-45.67/8", "(06) 123-45.67/8"),
        ("123456", "123456"),
        ("1" * 30, "1" * 30),
        # Incollati da una pagina web, da un PDF o da Word: spazi non separabili (anche stretti), tabulazioni e spazi
        # doppi, trattini tipografici, segni di direzione del testo (WhatsApp Web) e spazi di larghezza zero
        ("333\u00a01234567", "333 1234567"),
        ("+39\u202f333\u2007123\u00a04567", "+39 333 123 4567"),
        ("333\t1234567", "333 1234567"),
        ("  333   123  4567 ", "333 123 4567"),
        ("06 \u2013 1234567", "06 - 1234567"),
        ("06\u20101234\u2011567\u2212890\u2014", "06-1234-567-890-"),
        ("\u202a+39 333 1234567\u202c", "+39 333 1234567"),
        ("\ufeff333\u200b123\u00ad4567\u200e", "3331234567"),
        # Il massimo di 30 caratteri vale dopo la pulizia
        ("+39  333  1234  5678  9012  345", "+39 333 1234 5678 9012 345"),
    ],
)
def test_valid_phones(value, expected):
    assert normalize_phone(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("12345", "almeno 6 cifre"),
        ("+39 333 abc 4567", "ammessi"),
        ("333#1234567", "ammessi"),
        ("333\u20201234567", "ammessi"),
        ("1" * 31, "massimo 30"),
        ("+-./() ", "almeno 6 cifre"),
    ],
)
def test_invalid_phones_say_why_without_the_value(value, reason):
    with pytest.raises(ServiceError, match=reason) as err:
        normalize_phone(value)
    assert value.strip() not in str(err.value)


async def test_pasted_phone_and_email_are_saved_clean(service, db):
    """Telefono ed email copiati da una pagina web o da Word, con lo spazio non separabile e il trattino lungo: si
    salvano puliti, come scritti a mano, invece di un errore che dice che gli spazi sono ammessi."""
    person = await service.create_person(
        "", "", "admin", first_name="Giulia", last_name="Bianchi", phone="333\u00a01234567", email=f"{EMAIL}\u00a0"
    )
    assert (person["phone"], person["email"]) == ("333 1234567", EMAIL)
    person = await service.update_person(person["id"], "admin", phone="06 \u2013 1234567", email=f"\u00a0{EMAIL}")
    assert (person["phone"], person["email"]) == ("06 - 1234567", EMAIL)
    # Lo stesso telefono scritto con lo spazio normale non è una modifica
    await service.update_person(person["id"], "admin", phone="06\u00a0-\u00a01234567")
    assert [a["detail"] for a in db.list_audit()].count("Giulia Bianchi: telefono") == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (f"  {EMAIL} ", EMAIL),
        ("nome.cognome+ufficio@sub.example.com", "nome.cognome+ufficio@sub.example.com"),
        # Copiata con uno spazio non separabile o un carattere invisibile ai lati o in mezzo
        (f"\u00a0{EMAIL}\u00a0", EMAIL),
        (f"\u2003{EMAIL}\u3000\n", EMAIL),
        (f"\ufeff{EMAIL}\u200b", EMAIL),
        ("riservata@\u200bexample.it", EMAIL),
    ],
)
def test_valid_emails(value, expected):
    assert normalize_email(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("mario", "nome@esempio.it"),
        ("mario@example", "nome@esempio.it"),
        ("mario@@example.it", "nome@esempio.it"),
        ("a@b@example.it", "nome@esempio.it"),
        ("@example.it", "nome@esempio.it"),
        ("mario@.it", "nome@esempio.it"),
        ("mario@example.", "nome@esempio.it"),
        ("mario rossi@example.it", "spazi"),
        ("mario\u00a0rossi@example.it", "spazi"),
        ("x" * 250 + "@a.it", "massimo 254"),
    ],
)
def test_invalid_emails_say_why_without_the_value(value, reason):
    with pytest.raises(ServiceError, match=reason) as err:
        normalize_email(value)
    assert value not in str(err.value)


def test_phone_and_email_are_seen_only_with_the_persons_permission(settings, controller, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    admin = {"X-Remote-User-Name": "admin"}
    viewer = {"X-Remote-User-Name": "consulta"}
    log_only = {"X-Remote-User-Name": "portiere"}
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        client.get("/api/me", headers=admin)
        client.post("/api/users", json={"username": "consulta", "perms": ["persone"]}, headers=admin)
        client.post("/api/users", json={"username": "portiere", "perms": ["stato", "registro"]}, headers=admin)
        body = {"first_name": "Mario", "last_name": "Rossi", "card": "04aa", "phone": PHONE, "email": EMAIL}
        person = client.post("/api/persons", json=body, headers=admin).json()
        # Un passaggio nel registro, come lo salva l'app (record_event: vedi il test di registro e Home Assistant)
        client.app.state.db.add_event(
            ts=datetime.now(tz=UTC).isoformat(timespec="seconds"),
            kind="concesso",
            card="04aa",
            person_id=person["id"],
            person_name=person["name"],
        )

        # Chi vede le persone vede telefono ed email, ma non li cambia
        [seen] = client.get("/api/persons", headers=viewer).json()
        assert (seen["phone"], seen["email"]) == (PHONE, EMAIL)
        assert client.get(f"/api/persons/{person['id']}", headers=viewer).json()["phone"] == PHONE
        response = client.patch(f"/api/persons/{person['id']}", json={"phone": ""}, headers=viewer)
        assert response.status_code == 403
        # Chi non vede le persone: niente persone; registro, cruscotto e riepilogo senza telefono ed email
        assert client.get("/api/persons", headers=log_only).status_code == 403
        assert client.get(f"/api/persons/{person['id']}", headers=log_only).status_code == 403
        events = client.get("/api/events", headers=log_only)
        assert "Mario Rossi" in events.text and assert_no_secret(events.json())
        for path, headers in (
            ("/api/dashboard", log_only),
            ("/api/summary", log_only),
            ("/api/dashboard", admin),
            ("/api/export/events.csv", admin),
        ):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, path
            assert PHONE not in response.text and EMAIL not in response.text, path

        # Valori non validi: il motivo, mai il valore, né nella risposta né nel log
        response = client.patch(f"/api/persons/{person['id']}", json={"phone": "333 abc"}, headers=admin)
        assert response.status_code == 400 and "abc" not in response.text
        response = client.post("/api/persons", json={"first_name": "Anna", "phone": 39333987654}, headers=admin)
        assert response.status_code == 422 and "telefono" in response.json()["detail"]
        response = client.post("/api/persons", json={"first_name": ["x"], "email": EMAIL}, headers=admin)
        assert response.status_code == 422
        # Errore di tutta la richiesta: nel log i dati mandati, senza telefono ed email
        response = client.post(f"/api/persons/{person['id']}/cards", json={"phone": PHONE}, headers=admin)
        assert response.status_code == 422 and "tessera" in response.json()["detail"]
        response = client.patch(f"/api/persons/{person['id']}", json={"email": "altra@example.it"}, headers=admin)
        assert response.status_code == 200
        audit = client.get("/api/audit", headers=admin).json()
        assert assert_no_secret(audit) and "altra@example.it" not in json.dumps(audit)
        assert "Mario Rossi: email" in [a["detail"] for a in audit]
    log = caplog.text
    assert "Dati non validi" in log
    for secret in (PHONE, EMAIL, "39333987654", "altra@example.it", "333 abc"):
        assert secret not in log, secret


# Registro, Home Assistant e controller


async def test_events_and_home_assistant_never_carry_phone_or_email(service, controller, db, settings, monkeypatch):
    person = await service.create_person(
        "",
        "04aa",
        "admin",
        first_name="Mario",
        last_name="Rossi",
        phone=PHONE,
        email=EMAIL,
        employee_id=EMPLOYEE_ID,
        referent=REFERENT,
    )
    token = person["cards"][0]["axis_credential_token"]
    at = datetime(2026, 11, 13, 8, tzinfo=UTC)
    service.record_event(Event("IdPoint", "IdPoint/Request/IdData/x", at, {"Card": "04aa"}))
    service.record_event(
        Event("AccessControl", "AccessControl/AccessGranted/Credential/x", at, {"CredentialToken": token})
    )
    rows = db._query("SELECT * FROM events")
    assert {r["person_name"] for r in rows} == {"Mario Rossi"} and assert_no_secret(rows)

    bridge = HaBridge(settings, service.monitor, db)
    sent = []

    async def publish(topic, payload, retain=False):
        sent.append((topic, payload))
        return True

    monkeypatch.setattr(service.monitor, "publish", publish)
    for row in db.list_events():
        await bridge.publish_event(row)
    client = FakeClient()
    await bridge.on_connect(client)
    published = json.dumps([[t, str(p)] for t, p in sent] + [[t, str(p)] for t, p in client.published.items()])
    assert "Mario Rossi" in published
    assert PHONE not in published and EMAIL not in published
    assert EMPLOYEE_ID not in published and REFERENT not in published
    assert all(EMPLOYEE_ID not in json.dumps(r) and REFERENT not in json.dumps(r) for r in rows)
    # Sul controller solo il nome completo
    assert assert_no_secret([controller.users, controller.credentials])
    assert EMPLOYEE_ID not in json.dumps([controller.users, controller.credentials])
    assert REFERENT not in json.dumps([controller.users, controller.credentials])
    assert controller.users[person["axis_user_token"]]["Name"] == "Mario Rossi"


# CSV


async def test_exported_file_has_phone_and_email_and_imports_as_before(service, db):
    await service.create_person(
        "", "04aa", "admin", first_name="Mario", last_name="Rossi", phone="0612345678", email="mario@example.it"
    )
    anna = await service.create_person("", "", "admin", first_name="Anna", last_name="Maria Bianchi", phone=PHONE)
    await service.create_person("", "", "admin", first_name="Ugo", last_name="Neri", phone="06.123.456")
    await service.create_person("", "", "admin", first_name="Ada", last_name="Neri", phone="(06) 123-4567")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses())
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0].split(";")[-5:] == ["ultimo_accesso", "telefono", "email", "matricola", "referente"]
    # Ogni telefono come formula di testo, perché il foglio di calcolo lo mostri com'è: lo zero iniziale resta, il + non
    # ha davanti l'apostrofo che resterebbe in vista, «06.123.456» non diventa il numero 6123456. In fondo matricola e
    # referente, qui vuoti
    by_name = {line.split(";")[0]: line for line in lines[1:]}
    assert by_name["Mario Rossi"].endswith(';"=""0612345678""";mario@example.it;;')
    assert by_name["Anna Maria Bianchi"] == f'Anna Maria Bianchi;;;;;;;;;;"=""{PHONE}""";;;'
    assert by_name["Ugo Neri"] == 'Ugo Neri;;;;;;;;;;"=""06.123.456""";;;'
    assert by_name["Ada Neri"] == 'Ada Neri;;;;;;;;;;"=""(06) 123-4567""";;;'
    rows = {row["nome"]: row for row in read_import(text)}
    assert rows["Mario Rossi"]["telefono"] == "0612345678" and rows["Anna Maria Bianchi"]["telefono"] == PHONE
    assert (rows["Ugo Neri"]["telefono"], rows["Ada Neri"]["telefono"]) == ("06.123.456", "(06) 123-4567")

    # Reimportato com'è: niente da fare; telefono ed email del file non contano
    result = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert result["riepilogo"] == {"righe": 4, "ok": 0, "aggiornate": 0, "saltate": 4, "errori": 0}
    changed = text.replace("mario@example.it", "altro@example.it").replace("0612345678", "0698765432")
    result = await import_persons(service, changed, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["saltate"] == 4
    mario = next(p for p in db.all_persons() if p["name"] == "Mario Rossi")
    assert (mario["phone"], mario["email"]) == ("0612345678", "mario@example.it")
    # Una persona nuova dal file: il nome completo diviso al primo spazio, telefono ed email non si importano
    await service.delete_person(anna["id"], "admin")
    result = await import_persons(service, text, GROUPS, "admin", apply=True)
    assert sorted(r["esito"] for r in result["righe"]) == ["ok", "saltata", "saltata", "saltata"]
    again = next(p for p in db.all_persons() if p["name"] == "Anna Maria Bianchi")
    assert (again["first_name"], again["last_name"], again["phone"]) == ("Anna", "Maria Bianchi", "")
    assert_invariant(db)


# Elenco Persone nella pagina

PERSONS = [
    {"id": 1, "name": "Mario Rossi", "first_name": "Mario", "last_name": "Rossi", "phone": PHONE, "email": EMAIL},
    {"id": 2, "name": "Zeno Abate", "first_name": "Zeno", "last_name": "Abate"},
    {"id": 3, "name": "Bruno Bianchi", "first_name": "Bruno", "last_name": "Bianchi", "phone": "(06) 123-4567"},
    {"id": 4, "name": "Ospite", "first_name": "Ospite", "last_name": ""},
    {"id": 5, "name": "anna bianchi", "first_name": "anna", "last_name": "bianchi"},
    {"id": 6, "name": "Élodie Àlvarez", "first_name": "Élodie", "last_name": "Àlvarez", "company": "Pulizie Srl"},
    {"id": 7, "name": "Luca  Verdi", "first_name": "Luca  Verdi", "last_name": ""},
    {"id": 8, "name": "Rossi", "first_name": "", "last_name": "Rossi"},
]


@needs_node
def test_persons_are_sorted_by_last_name_and_found_by_phone_and_email():
    persons = [{"phone": "", "email": "", "company": "", "cards": [], **p} for p in PERSONS]
    persons[0]["cards"] = [{"number": "04AB12"}]
    searches = {
        "phone": "3339876543",
        "spaced": "333 987",
        "prefix": "+39333",
        "landline": "061234567",
        "email": "RISERVATA@example",
        "reversed": "rossi mario",
        "accent": "alvarez",
        "company": "pulizie",
        "card": "04ab",
        "surname": "BIANCHI",
        "nothing": "Verdi Luca",
        "blank": "  ",
    }
    calls = {"order": "[...persons].sort(comparePersons).map((p) => p.id)"}
    for key, text in searches.items():
        calls[key] = f"persons.filter((p) => personMatches(p, {json.dumps(text)})).map((p) => p.id)"
    out = run_page(
        "Europe/Rome",
        calls,
        names=(
            "personSortKey",
            "nameCollator",
            "comparePersons",
            "foldText",
            "PHONE_MARKS",
            "personTexts",
            "personHaystack",
            "personMatches",
        ),
        prelude=f"const persons = {json.dumps(persons)};",
    )
    # Abate, Àlvarez, bianchi anna, Bianchi Bruno, «Luca  Verdi» al nome, Ospite al nome, Rossi (solo cognome), Rossi
    # Mario: maiuscole e accenti non contano
    assert out.pop("order") == [2, 6, 5, 3, 7, 4, 8, 1]
    assert out == {
        "phone": [1],
        "spaced": [1],
        "prefix": [1],
        "landline": [3],
        "email": [1],
        "reversed": [1],
        "accent": [6],
        "company": [6],
        "card": [1],
        "surname": [3, 5],
        "nothing": [],
        "blank": [1, 2, 3, 4, 5, 6, 7, 8],
    }


@needs_node
def test_page_cleans_pasted_phone_and_email_like_the_server():
    """Telefono ed email incollati nella pagina: puliti mentre si scrivono come li pulisce il server (spazio non
    separabile, trattino lungo di Word, segni invisibili), l'email anche senza spazi ai lati, che il browser
    rifiuterebbe prima dell'invio. Il telefono che si sta scrivendo tiene i suoi spazi, anche in fondo."""
    values = {
        "nbsp": ("phone", "333 1234567"),
        "endash": ("phone", "06 – 1234567"),
        "marks": ("phone", "‪+39 333 1234567‬"),
        "typing": ("phone", "333 "),
        "email": ("email", f" {EMAIL} "),
        "invisible": ("email", f"﻿{EMAIL}​"),
    }
    calls = {key: f"cleanContact({json.dumps(kind)}, {json.dumps(text)})" for key, (kind, text) in values.items()}
    out = run_page("Europe/Rome", calls, names=("INVISIBLE_MARKS", "cleanContact"))
    assert out == {
        "nbsp": "333 1234567",
        "endash": "06 - 1234567",
        "marks": "+39 333 1234567",
        "typing": "333 ",
        "email": EMAIL,
        "invisible": EMAIL,
    }
    # Ripulito dalla pagina, il server lo salva com'è
    for key in ("nbsp", "endash", "marks"):
        assert normalize_phone(out[key]) == out[key]
    for key in ("email", "invisible"):
        assert normalize_email(out[key]) == out[key]


@needs_node
def test_mailto_link_writes_to_the_whole_address():
    """Collegamento mailto: della scheda: «#» e «?», validi in un indirizzo, non tagliano il destinatario."""
    emails = ["ufficio#2@example.it", "info?x@example.it", "nome.cognome+ufficio@sub.example.com", "a%b&c@example.it"]
    calls = {
        email: f"(() => {{ const u = new URL(mailtoHref({json.dumps(email)})); "
        "return [decodeURIComponent(u.pathname), u.search, u.hash]; })()"
        for email in emails
    }
    out = run_page("Europe/Rome", calls, names=("mailtoHref",))
    assert out == {email: [email, "", ""] for email in emails}
    for email in emails:
        assert normalize_email(email) == email


# Matricola e referente interno (0.28.3, seconda parte). Come telefono ed email: solo nell'app


def database_first_part(path: Path) -> int:
    """Database come lo lasciava la prima parte della 0.28.3: schema 10 con nome, cognome, telefono ed email, senza
    matricola, referente, indice della matricola e foto."""
    db = Database(path, "0.28.3")
    person = db.add_person("Maria Rosa Bianchi", "u1", first_name="Maria Rosa", last_name="Bianchi", phone=PHONE)
    db.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX persons_employee_id")
    conn.execute("DROP TABLE person_photos")
    conn.execute("ALTER TABLE persons DROP COLUMN employee_id")
    conn.execute("ALTER TABLE persons DROP COLUMN referent")
    conn.commit()
    conn.close()
    return person


def table_names(db: Database) -> set[str]:
    return {row["name"] for row in db._query("SELECT name FROM sqlite_master")}


def test_database_of_the_first_part_gets_employee_id_referent_and_photos(tmp_path, caplog):
    path = tmp_path / DB_NAME
    person = database_first_part(path)
    with caplog.at_level(logging.INFO):
        db = Database(path, "0.28.3")
    try:
        assert db.schema_version == SCHEMA_VERSION == 10
        [row] = db.all_persons()
        assert (row["id"], row["first_name"], row["last_name"], row["phone"]) == (
            person,
            "Maria Rosa",
            "Bianchi",
            PHONE,
        )
        assert (row["employee_id"], row["referent"]) == ("", "")
        assert {"person_photos", "person_photos_version", "persons_employee_id"} <= table_names(db)
        assert "Matricola e referente aggiunti" in caplog.text
        db.update_person_details(person, "", "", employee_id=EMPLOYEE_ID, referent=REFERENT)
    finally:
        db.close()
    # Di nuovo: niente da aggiungere, tutto com'è
    caplog.clear()
    with caplog.at_level(logging.INFO):
        db = Database(path, "0.28.3")
    try:
        [row] = db.all_persons()
        assert (row["employee_id"], row["referent"]) == (EMPLOYEE_ID, REFERENT)
        assert "Matricola e referente" not in caplog.text
    finally:
        db.close()


def test_database_of_0_28_2_gets_every_new_column(tmp_path):
    path = tmp_path / DB_NAME
    database_0_28_2(path, ["Anna", "Mario Rossi"])
    db = Database(path, "0.28.3")
    try:
        assert {(p["employee_id"], p["referent"]) for p in db.all_persons()} == {("", "")}
        assert {"person_photos", "persons_employee_id"} <= table_names(db)
        assert all(p["photo"] == "" for p in db.list_persons())
    finally:
        db.close()


def test_two_equal_employee_ids_put_in_by_hand_do_not_stop_the_app(tmp_path, caplog):
    """Due matricole uguali scritte a mano nel database: l'indice unico non si crea, l'app parte lo stesso e lo dice
    nel log (senza la matricola); si riprova a ogni avvio."""
    path = tmp_path / DB_NAME
    database_first_part(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE persons ADD COLUMN employee_id TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE persons ADD COLUMN referent TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "INSERT INTO persons (name, first_name, employee_id, created_at, updated_at) VALUES "
        "('Anna', 'Anna', 'ab-1', ?, ?), ('Bruno', 'Bruno', 'AB-1', ?, ?)",
        (TS, TS, TS, TS),
    )
    conn.commit()
    conn.close()
    with caplog.at_level(logging.WARNING):
        db = Database(path, "0.28.3")
    try:
        assert "Indice della matricola non creato" in caplog.text and "ab-1" not in caplog.text.lower()
        assert "persons_employee_id" not in table_names(db) and len(db.all_persons()) == 3
    finally:
        db.close()


def test_the_database_refuses_the_same_employee_id_in_other_letters(db):
    anna = db.add_person("Anna", None, employee_id="Ab-12")
    db.add_person("Bruno", None)
    db.add_person("Carla", None)
    with pytest.raises(sqlite3.IntegrityError):
        db.add_person("Dario", None, employee_id="aB-12")
    assert db.person_by_employee_id("AB-12") == {"id": anna, "name": "Anna"}
    assert db.person_by_employee_id("ab-12", anna) is None and db.person_by_employee_id("") is None
    assert nocase("ÀB-12") == "Àb-12"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        (None, ""),
        ("  MAT  001 ", "MAT 001"),
        ("000123", "000123"),
        ("A\u00a0\u00a0B", "A B"),
        ("\u200bMAT-9\u200e", "MAT-9"),
        ("x" * 30, "x" * 30),
    ],
)
def test_valid_employee_ids(value, expected):
    assert normalize_employee_id(value) == expected


@pytest.mark.parametrize(
    ("value", "reason"),
    [("x" * 31, r"matricola troppo lunga \(massimo 30 caratteri\)$"), ("MAT\x07001", "caratteri di controllo")],
)
def test_invalid_employee_ids_say_why_without_the_value(value, reason):
    with pytest.raises(ServiceError, match=reason) as err:
        normalize_employee_id(value)
    assert value not in str(err.value)


def test_referent_is_free_text_up_to_100_characters():
    assert normalize_referent("  Ufficio   Tecnico\t(int. 12) ") == "Ufficio Tecnico (int. 12)"
    assert normalize_referent("y" * 100) == "y" * 100
    with pytest.raises(ServiceError, match="massimo 100"):
        normalize_referent("y" * 101)
    with pytest.raises(ServiceError, match="caratteri di controllo"):
        normalize_referent("Ufficio\x00Tecnico")


async def test_employee_id_is_unique_without_minding_letters_and_names_who_has_it(service, controller, db):
    mario = await service.create_person(
        "", "04aa", "admin", first_name="Mario", last_name="Rossi", employee_id="ab-12", referent=REFERENT
    )
    assert (mario["employee_id"], mario["referent"]) == ("ab-12", REFERENT)
    users, credentials = dict(controller.users), dict(controller.credentials)
    for card in ("04bb", ""):
        with pytest.raises(ServiceError) as err:
            await service.create_person("", card, "admin", first_name="Anna", last_name="Bianchi", employee_id="AB-12")
        assert str(err.value) == "matricola già usata da Mario Rossi"
    # Rifiutata prima di scrivere sul controller
    assert (controller.users, controller.credentials) == (users, credentials)
    anna = await service.create_person("", "", "admin", first_name="Anna", last_name="Bianchi", employee_id="AB-13")
    with pytest.raises(ServiceError, match="^matricola già usata da Mario Rossi$"):
        await service.update_person(anna["id"], "admin", employee_id=" Ab-12 ", company="Ditta")
    assert db.get_person(anna["id"])["company"] == "" and db.get_person(anna["id"])["employee_id"] == "AB-13"
    # La propria in altre lettere, e senza matricola quante si vuole
    mario = await service.update_person(mario["id"], "admin", employee_id="AB-12")
    assert mario["employee_id"] == "AB-12"
    for name in ("Carla", "Dario"):
        await service.create_person(name, "", "admin")
    assert sorted(p["employee_id"] for p in db.all_persons()) == ["", "", "AB-12", "AB-13"]


def test_employee_id_and_referent_through_the_api(settings, controller, tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    admin = {"X-Remote-User-Name": "admin"}
    viewer = {"X-Remote-User-Name": "consulta"}
    log_only = {"X-Remote-User-Name": "portiere"}
    secrets = (EMPLOYEE_ID, REFERENT, "MAT-SEGRETA", "Referente Segreto", PHONE, EMAIL)
    with caplog.at_level(logging.DEBUG), TestClient(app) as client:
        client.get("/api/me", headers=admin)
        client.post("/api/users", json={"username": "consulta", "perms": ["persone"]}, headers=admin)
        client.post("/api/users", json={"username": "portiere", "perms": ["stato", "registro"]}, headers=admin)
        body = {"first_name": "Mario", "last_name": "Rossi", "card": "04aa", "employee_id": EMPLOYEE_ID}
        person = client.post("/api/persons", json={**body, "referent": REFERENT}, headers=admin).json()
        assert (person["employee_id"], person["referent"]) == (EMPLOYEE_ID, REFERENT)
        response = client.post("/api/persons", json={**body, "first_name": "Anna", "card": ""}, headers=admin)
        assert response.status_code == 400 and response.json()["detail"] == "matricola già usata da Mario Rossi"

        [seen] = client.get("/api/persons", headers=viewer).json()
        assert (seen["employee_id"], seen["referent"]) == (EMPLOYEE_ID, REFERENT)
        response = client.patch(f"/api/persons/{person['id']}", json={"referent": ""}, headers=viewer)
        assert response.status_code == 403
        assert client.get(f"/api/persons/{person['id']}", headers=log_only).status_code == 403
        for path, headers in (("/api/events", log_only), ("/api/dashboard", log_only), ("/api/summary", log_only)):
            response = client.get(path, headers=headers)
            assert response.status_code == 200
            assert EMPLOYEE_ID not in response.text and REFERENT not in response.text, path

        # Rifiutati da FastAPI (tipo sbagliato, corpo a lista, valori annidati): nel log mai i valori
        bad = [
            ("patch", f"/api/persons/{person['id']}", {"employee_id": ["MAT-SEGRETA"]}),
            ("post", "/api/persons", [{"first_name": "Anna", "employee_id": "MAT-SEGRETA", "phone": PHONE}]),
            ("post", "/api/persons", {"first_name": {"referent": "Referente Segreto", "email": EMAIL}}),
            ("post", "/api/persons", {"first_name": [{"x": {"employee_id": "MAT-SEGRETA"}}], "referent": 3}),
            ("post", f"/api/persons/{person['id']}/cards", {"items": [{"referent": "Referente Segreto"}]}),
        ]
        for method, path, payload in bad:
            response = getattr(client, method)(path, json=payload, headers=admin)
            assert response.status_code == 422, (path, response.text)
        response = client.patch(f"/api/persons/{person['id']}", json={"employee_id": "x" * 31}, headers=admin)
        assert response.status_code == 400 and "massimo 30" in response.json()["detail"]
        response = client.patch(
            f"/api/persons/{person['id']}", json={"employee_id": "MAT-2", "referent": "Altro"}, headers=admin
        )
        assert response.status_code == 200
        audit = client.get("/api/audit", headers=admin).json()
        assert "Mario Rossi: matricola, referente interno" in [a["detail"] for a in audit]
        assert all(secret not in json.dumps(audit) for secret in (EMPLOYEE_ID, REFERENT, "MAT-2", "Altro"))
    assert "Dati non validi" in caplog.text
    for secret in secrets:
        assert secret not in caplog.text, secret


def test_masked_log_values_at_any_depth():
    from app.main import _loggable

    error = {
        "type": "missing",
        "loc": ("body", "card"),
        "input": [{"phone": PHONE, "inner": {"email": EMAIL, "list": [{"employee_id": "M1"}]}, "referent": "R"}],
    }
    text = str(_loggable(error))
    assert all(secret not in text for secret in (PHONE, EMAIL, "M1", "'R'")) and "inner" in text
    assert _loggable({"loc": ("body", "referent"), "input": "Segreto"})["input"] == "…"
    assert _loggable({"loc": ("body",), "input": b"\x89PNG segreto"})["input"] == "…"
    assert _loggable({"loc": ("query", "limit"), "input": "abc"}) == {"loc": ("query", "limit"), "input": "abc"}


async def test_exported_file_has_employee_id_and_referent_and_imports_as_before(service, db):
    await service.create_person("Mario Rossi", "04aa", "admin", employee_id="000123", referent="=Ufficio Tecnico")
    await service.create_person("Anna Bianchi", "", "admin", employee_id='MAT "A"', referent="Bruno Neri")
    await service.create_person("Ugo Neri", "", "admin")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses())
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0].split(";")[-5:] == ["ultimo_accesso", "telefono", "email", "matricola", "referente"]
    by_name = {line.split(";")[0]: line for line in lines[1:]}
    # La matricola come formula di testo: il foglio di calcolo tiene gli zeri; il referente non diventa una formula
    assert by_name["Mario Rossi"].endswith(';;"=""000123""";\'=Ufficio Tecnico')
    # Con le virgolette non si può: testo normale
    assert by_name["Anna Bianchi"].endswith(';;"MAT ""A""";Bruno Neri')
    assert by_name["Ugo Neri"].endswith(";;;;")
    rows = {row["nome"]: row for row in read_import(text)}
    assert (rows["Mario Rossi"]["matricola"], rows["Mario Rossi"]["referente"]) == ("000123", "=Ufficio Tecnico")
    # Reimportato com'è: niente da fare, e una matricola cambiata nel file non cambia niente
    result = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert result["riepilogo"]["saltate"] == 3 and result["riepilogo"]["errori"] == 0
    changed = text.replace("000123", "999999").replace("Bruno Neri", "Altro")
    result = await import_persons(service, changed, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["saltate"] == 3
    assert {p["name"]: p["employee_id"] for p in db.all_persons()}["Mario Rossi"] == "000123"


async def test_restore_brings_back_employee_id_and_referent_masked(plant, controller, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", employee_id="M1", referent=REFERENT)
    backup = await plant.backup()
    raw = json.dumps(plant.backups.load(backup["id"]))
    assert '"employee_id": "M1"' in raw and REFERENT in raw
    await plant.access.update_person(mario["id"], "admin", employee_id="M9", referent="")
    second = await plant.backup()
    compared = await plant.backups.compare(second["id"], "precedente", plant.clients)
    fields = [
        (f["label"], f["old"], f["new"])
        for group in compared["groups"]
        for section in group["sections"]
        for changed in section["changed"]
        for f in changed["fields"]
    ]
    assert fields == [("Matricola", "indicato", "cambiato"), ("Referente Interno", "indicato", "—")]
    assert "M9" not in json.dumps(compared) and REFERENT not in json.dumps(compared)

    items, result = await plant.restore(backup["id"], ["persone"])
    [item] = items
    assert [(c["label"], c["old"], c["new"]) for c in item["changes"]] == [
        ("Matricola", "indicato", "cambiato"),
        ("Referente Interno", "—", "indicato"),
    ]
    assert not result["failed"] and REFERENT not in json.dumps(public(items))
    restored = db.get_person(mario["id"])
    assert (restored["employee_id"], restored["referent"]) == ("M1", REFERENT)


async def test_old_backup_keeps_employee_id_and_referent(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", company="Ditta")
    backup = await plant.backup()
    _, saved = plant.backups.load(backup["id"])
    for person in saved[COMMON]["persons"]:
        del person["employee_id"], person["referent"]
    await plant.access.update_person(mario["id"], "admin", company="Altra", employee_id="M5", referent=REFERENT)
    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, ["persone"], plant.controller_id)
    assert [c["label"] for c in items[0]["changes"]] == ["Ditta o Reparto"]
    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    result = await apply(items, [i["key"] for i in items], runtime, "admin")
    assert not result["failed"]
    restored = db.get_person(mario["id"])
    assert (restored["company"], restored["employee_id"], restored["referent"]) == ("Ditta", "M5", REFERENT)


async def test_restore_leaves_an_employee_id_given_to_someone_else_meanwhile(plant, controller, db):
    """Matricola del backup data nel frattempo a un'altra persona: resta sua. Il ripristino non si ferma: la persona
    ripristinata tiene la sua di adesso (o, ricreata, nessuna), e l'anteprima lo dice senza mostrare la matricola. Se
    il backup ridà a quell'altra persona la matricola di prima, ripristinandole insieme tornano com'erano."""
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", employee_id="M1", company="Ditta")
    carla = await plant.access.create_person("Carla Verdi", "", "admin", employee_id="C1")
    anna = await plant.access.create_person("Anna Bianchi", "", "admin", employee_id="A1")
    backup = await plant.backup()
    await plant.access.update_person(mario["id"], "admin", employee_id="M2", company="Altra")
    await plant.access.update_person(anna["id"], "admin", employee_id="m1")
    await plant.access.delete_person(carla["id"], "admin")
    # Bruno è nuovo, non è nel backup: la C1 di Carla resta sua
    await plant.access.create_person("Bruno Neri", "", "admin", employee_id="c1")
    _, saved = plant.backups.load(backup["id"])
    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, ["persone"], plant.controller_id)

    rows = {i["label"]: i for i in items}
    assert rows["Mario Rossi"]["warnings"] == [
        "la matricola del backup ora è di Anna Bianchi: torna a questa persona solo ripristinando anche Anna Bianchi"
    ]
    assert [c["label"] for c in rows["Mario Rossi"]["changes"]] == ["Ditta o Reparto", "Matricola"]
    assert rows["Carla Verdi"]["warnings"] == [
        "la matricola del backup ora è di Bruno Neri: resta sua, e torna senza matricola"
    ]
    assert not rows["Anna Bianchi"]["warnings"]
    text = json.dumps(public(items))
    assert all(f'"{value}"' not in text for value in ("M1", "M2", "m1", "A1", "C1", "c1"))

    # Senza la riga di Anna: Anna tiene M1, Mario la sua di adesso, Carla torna senza; il ripristino va
    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    keys = [i["key"] for i in items if i["default"] and i["label"] != "Anna Bianchi"]
    result = await apply(items, keys, runtime, "admin")
    assert not result["failed"] and len(result["applied"]) == 2
    persons = {p["name"]: p for p in db.all_persons()}
    assert (persons["Mario Rossi"]["employee_id"], persons["Mario Rossi"]["company"]) == ("M2", "Ditta")
    assert persons["Anna Bianchi"]["employee_id"] == "m1" and persons["Bruno Neri"]["employee_id"] == "c1"
    assert persons["Carla Verdi"]["employee_id"] == "" and persons["Carla Verdi"]["id"] == carla["id"]


async def test_restoring_both_gives_each_employee_id_back(plant, db):
    """La matricola di Mario è andata ad Anna, che nel backup ne ha un'altra: ripristinandoli insieme la riga di Mario
    va dopo quella di Anna, che la lascia, e tornano com'erano. Due matricole scambiate tra loro invece restano come
    sono adesso, e l'anteprima lo dice: il ripristino non si ferma."""
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", employee_id="M1")
    anna = await plant.access.create_person("Anna Bianchi", "", "admin", employee_id="A1")
    backup = await plant.backup()
    await plant.access.update_person(mario["id"], "admin", employee_id="M2")
    await plant.access.update_person(anna["id"], "admin", employee_id="M1")

    items, result = await plant.restore(backup["id"], ["persone"])

    assert [i["label"] for i in items] == ["Mario Rossi", "Anna Bianchi"]
    assert not result["failed"] and [a["label"] for a in result["applied"]] == ["Anna Bianchi", "Mario Rossi"]
    assert {p["name"]: p["employee_id"] for p in db.all_persons()} == {"Mario Rossi": "M1", "Anna Bianchi": "A1"}

    # Scambiate
    await plant.access.update_person(mario["id"], "admin", employee_id="X")
    await plant.access.update_person(anna["id"], "admin", employee_id="M1")
    await plant.access.update_person(mario["id"], "admin", employee_id="A1")
    items, result = await plant.restore(backup["id"], ["persone"])
    # Nessuna delle due si libera: niente da ripristinare, e niente righe
    assert items == [] and not result["failed"]
    assert {p["name"]: p["employee_id"] for p in db.all_persons()} == {"Mario Rossi": "A1", "Anna Bianchi": "M1"}
    # Con un'altra modifica la riga c'è, e dice perché la matricola non torna
    await plant.access.update_person(mario["id"], "admin", company="Altra")
    items, result = await plant.restore(backup["id"], ["persone"])
    assert [(i["label"], i["warnings"]) for i in items] == [
        ("Mario Rossi", ["la matricola del backup ora è di Anna Bianchi: resta sua, e tiene quella di adesso"])
    ]
    assert not result["failed"] and db.get_person(mario["id"])["employee_id"] == "A1"


async def test_employee_id_taken_between_preview_and_restore_does_not_stop_it(plant, db):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", employee_id="M1")
    anna = await plant.access.create_person("Anna Bianchi", "", "admin")
    backup = await plant.backup()
    await plant.access.update_person(mario["id"], "admin", employee_id="", company="Altra")
    _, saved = plant.backups.load(backup["id"])
    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, ["persone"], plant.controller_id)
    assert not items[0]["warnings"]
    # Tra l'anteprima e Ripristina la matricola va ad Anna
    await plant.access.update_person(anna["id"], "admin", employee_id="M1")
    runtime = Runtime(db, plant.access, plant.rules, plant.programming, plant.controller_id)
    result = await apply(items, [i["key"] for i in items], runtime, "admin")
    assert not result["failed"]
    assert db.get_person(mario["id"])["employee_id"] == "" and db.get_person(mario["id"])["company"] == ""
    assert db.get_person(anna["id"])["employee_id"] == "M1"


async def test_controller_and_shadow_comparison_do_not_see_the_new_fields(plant, controller, db, settings):
    """desired.build e il confronto in ombra identici con e senza matricola, referente e foto."""
    from test_person_photo import PNG

    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin")
    anna = await plant.access.create_person("Anna Bianchi", "04bb", "admin")
    before = build(load_conf(db, settings.access_profile), 1, CARDS_KINDS)
    reading = await read_strict(controller, CARDS_KINDS)
    calls = list(controller.calls)
    await plant.access.update_person(mario["id"], "admin", employee_id=EMPLOYEE_ID, referent=REFERENT)
    await plant.access.update_person(anna["id"], "admin", employee_id="MAT-2")
    plant.access.set_person_photo(mario["id"], PNG, "admin")
    after = build(load_conf(db, settings.access_profile), 1, CARDS_KINDS)
    assert after == before
    assert compare(after, reading) == compare(before, reading)
    assert {row["action"] for row in compare(after, reading)["rows"]} == {"uguale"}
    # Sul controller non si è scritto niente
    assert controller.calls == calls
