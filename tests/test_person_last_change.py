"""Ultima Modifica di una persona (persons.updated_at, 0.28.3): cambia quando cambiano i suoi dati (anagrafica, foto,
tessere, accesso e tipologia), da qualunque strada (scheda, sincronizzazione, importazione, Da Registrare, ripristino,
tessere non presenti, presa in carico dei gruppi). Non cambia per i passaggi, i giri di sincronizzazione senza novità,
il confronto in ombra, le tessere segnate o tornate presenti sul controller, le riscritture sul controller, le modifiche
delle tipologie che non cambiano a chi appartiene la persona e i salvataggi senza differenze. created_at resta la data
di inserimento."""

from datetime import UTC, datetime, timedelta

import pytest
from test_backup import Plant
from test_person_photo import JPEG, PNG

from app import db as db_module
from app.compare import compare, read_strict
from app.desired import build, load_conf
from app.restore import plan
from app.service import ServiceError
from app.transfer import import_persons, persons_csv

GROUPS = [{"token": "profilo_sempre", "name": "Sempre"}]


class Clock:
    """Orologio del database, che va avanti di un minuto alla volta: due modifiche nello stesso secondo non si
    distinguerebbero."""

    def __init__(self) -> None:
        self.moment = datetime(2026, 11, 13, 8, tzinfo=UTC)

    def __call__(self) -> str:
        return self.moment.isoformat(timespec="seconds")

    def tick(self) -> str:
        self.moment += timedelta(minutes=1)
        return self()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(db_module, "now_iso", clock)
    return clock


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


class Watch:
    """Ultima Modifica e data di inserimento delle persone prima di un passo."""

    def __init__(self, db, clock) -> None:
        self.db, self.clock = db, clock
        self.before: dict[int, tuple[str, str]] = {}

    def __enter__(self):
        self.before = {p["id"]: (p["updated_at"], p["created_at"]) for p in self.db.all_persons()}
        self.clock.tick()
        return self

    def __exit__(self, *exc) -> None:
        return None

    def changed(self) -> set[int]:
        """Persone la cui Ultima Modifica è adesso; la data di inserimento non cambia mai."""
        result = set()
        for person in self.db.all_persons():
            updated, created = self.before.get(person["id"], ("", person["created_at"]))
            assert person["created_at"] == created
            if person["updated_at"] != updated:
                assert person["updated_at"] == self.clock()
                result.add(person["id"])
        return result


def watch(db, clock) -> Watch:
    return Watch(db, clock)


def rule(plant, schedule="standard_always") -> dict:
    return {"target": "door", "target_id": plant.scala, "schedule": schedule}


async def test_new_person_has_the_same_insertion_and_last_change(plant, db, clock):
    person = await plant.access.create_person("Mario Rossi", "04aa", "admin", company="Ditta")
    assert person["created_at"] == person["updated_at"] == clock()
    without = await plant.access.create_person("Anna Bianchi", "", "admin", notes="Note")
    assert without["created_at"] == without["updated_at"] == clock()


async def test_every_field_of_the_details_changes_it_and_the_same_values_do_not(plant, db, clock):
    mario = (await plant.access.create_person("Mario Rossi", "04aa", "admin"))["id"]
    anna = (await plant.access.create_person("Anna Bianchi", "", "admin"))["id"]
    fields = [
        {"company": "Pulizie Srl"},
        {"notes": "Turno di notte"},
        {"phone": "+39 333 000 1111"},
        {"email": "finto@example.it"},
        {"employee_id": "MAT-001"},
        {"referent": "Ufficio Tecnico"},
        {"first_name": "Mariano"},
        {"last_name": "Rossini"},
        # Stesso nome completo diviso in un altro modo: solo nell'app, ma è una modifica
        {"first_name": "Mariano Rossini", "last_name": ""},
    ]
    for change in fields:
        with watch(db, clock) as step:
            await plant.access.update_person(mario, "admin", **change)
        assert step.changed() == {mario}, change
        # Salvata di nuovo uguale (come fa Salva nei Dati senza cambiare niente): niente
        with watch(db, clock) as step:
            await plant.access.update_person(mario, "admin", **change)
        assert step.changed() == set(), change
    person = db.get_person(mario)
    with watch(db, clock) as step:
        await plant.access.update_person(
            mario,
            "admin",
            company=person["company"],
            notes=person["notes"],
            phone=person["phone"],
            email=person["email"],
            employee_id=person["employee_id"],
            referent=person["referent"],
            first_name=person["first_name"],
            last_name=person["last_name"],
        )
        await plant.access.rename_person(
            mario, person["name"], "admin", first_name=person["first_name"], last_name=person["last_name"]
        )
    assert step.changed() == set()
    # Un valore rifiutato non cambia niente, neanche gli altri della stessa richiesta
    await plant.access.update_person(anna, "admin", employee_id="MAT-002")
    with watch(db, clock) as step, pytest.raises(ServiceError):
        await plant.access.update_person(mario, "admin", company="Altra", employee_id="mat-002")
    assert step.changed() == set() and db.get_person(mario)["company"] == "Pulizie Srl"


async def test_photo_changes_it(plant, db, clock):
    mario = (await plant.access.create_person("Mario Rossi", "", "admin"))["id"]
    for action in (
        lambda: plant.access.set_person_photo(mario, PNG, "admin"),
        lambda: plant.access.set_person_photo(mario, JPEG, "admin"),
        lambda: plant.access.delete_person_photo(mario, "admin"),
    ):
        with watch(db, clock) as step:
            action()
        assert step.changed() == {mario}
    with watch(db, clock) as step:
        plant.access.delete_person_photo(mario, "admin")
    assert step.changed() == set()


async def test_cards_and_access_of_the_person_change_it(plant, db, clock, controller):
    category = await plant.rules.save_category(None, {"name": "Personale", "rules": [rule(plant)]}, "admin")
    ospiti = await plant.rules.save_category(None, {"name": "Ospiti", "rules": [rule(plant)]}, "admin")
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", category_id=category["id"])
    anna = await plant.access.create_person("Anna Bianchi", "04bb", "admin", category_id=category["id"])
    pid = mario["id"]

    async def card_id(number):
        return next(c["id"] for c in db.get_person(pid)["cards"] if c["number"].lower() == number)

    steps = [
        ("tessera aggiunta", lambda: plant.access.add_card(pid, "04ab", "admin"), True),
        ("sospesa", lambda: _toggle(plant, card_id, "04ab", False), True),
        ("di nuovo sospesa", lambda: _toggle(plant, card_id, "04ab", False), False),
        ("riattivata", lambda: _toggle(plant, card_id, "04ab", True), True),
        (
            "validità",
            lambda: plant.access.set_person_access(pid, category["token"], "", "2030-01-01T00:00:00Z", "admin"),
            True,
        ),
        (
            "stessa validità",
            lambda: plant.access.set_person_access(pid, category["token"], "", "2030-01-01T00:00:00Z", "admin"),
            False,
        ),
        (
            "tipologia",
            lambda: plant.access.set_person_access(pid, "", "", "2030-01-01T00:00:00Z", "admin", ospiti["id"]),
            True,
        ),
        # Accesso Personalizzato: il profilo di un'altra tipologia
        (
            "accesso personalizzato",
            lambda: plant.access.set_person_access(pid, category["token"], "", "2030-01-01T00:00:00Z", "admin"),
            True,
        ),
        ("allineata alla tipologia", lambda: plant.rules.align_person(pid, "admin"), True),
        ("tessera eliminata", lambda: _delete(plant, card_id, "04ab"), True),
    ]
    for label, action, expected in steps:
        with watch(db, clock) as step:
            await action()
        assert step.changed() == ({pid} if expected else set()), label
    assert db.get_person(anna["id"])["updated_at"] == anna["updated_at"]


async def _toggle(plant, card_id, number, enabled):
    await plant.access.set_card_enabled(await card_id(number), enabled, "admin")


async def _delete(plant, card_id, number):
    await plant.access.delete_card(await card_id(number), "admin")


async def test_passages_shadow_comparison_and_category_edits_do_not_change_it(plant, db, clock, controller):
    category = await plant.rules.save_category(None, {"name": "Personale", "rules": [rule(plant)]}, "admin")
    other = await plant.rules.save_category(None, {"name": "Esterni", "rules": [rule(plant)]}, "admin")
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", category_id=category["id"])
    # Anna, di un'altra tipologia, apre come Personale (Accesso Personalizzato): il profilo è in comune
    anna = await plant.access.create_person("Anna Bianchi", "04bb", "admin", category_id=other["id"])
    await plant.access.set_person_access(anna["id"], category["token"], "", "", "admin")

    with watch(db, clock) as step:
        plant.access.log_event(
            ts=clock(), kind="concesso", card="04aa", person_id=mario["id"], person_name="Mario Rossi"
        )
        plant.access.log_event(ts=clock(), kind="negato", card="04aa", person_id=mario["id"], person_name="Mario Rossi")
        conf = load_conf(db, plant.access.settings.access_profile)
        compare(build(conf, plant.controller_id, ["utente", "tessera"]), await read_strict(controller, ["utente"]))
    assert step.changed() == set()

    # Tipologia modificata (orari, righe): il profilo di Personale era in comune, ne riceve uno suo e Mario passa al
    # nuovo. A chi appartiene Mario non cambia: la sua Ultima Modifica resta
    with watch(db, clock) as step:
        saved = await plant.rules.save_category(
            category["id"], {"name": "Personale", "description": "Orari nuovi", "rules": [rule(plant)]}, "admin"
        )
    assert saved["token"] != category["token"] and saved["updated"] == 1
    assert db.get_person(mario["id"])["cards"][0]["access_profile"] == saved["token"]
    assert step.changed() == set()
    with watch(db, clock) as step:
        await plant.rules.save_category(
            category["id"], {"name": "Personale", "description": "Di nuovo", "rules": [rule(plant)]}, "admin"
        )
        await plant.rules.refresh_category_profiles()
    assert step.changed() == set()

    # Tipologia eliminata con le persone dentro (nel database): per loro sì, restano senza tipologia
    with pytest.raises(ServiceError, match="ci sono persone"):
        await plant.rules.delete_category(other["id"], "admin")
    with watch(db, clock) as step:
        db.delete_category(other["id"])
    assert step.changed() == {anna["id"]} and db.get_person(anna["id"])["category_id"] == 0


async def test_synchronization_changes_it_only_for_real_news(plant, db, clock, controller):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin")
    anna = await plant.access.create_person("Anna Bianchi", "04bb", "admin")
    pid, token = mario["id"], mario["cards"][0]["axis_credential_token"]
    user_token = mario["axis_user_token"]

    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == set()

    # Tessera nuova arrivata dal controller, a un utente dell'app
    controller.add_existing(user_token, "Mario Rossi", "04ac")
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == {pid} and len(db.get_person(pid)["cards"]) == 2

    # Rinominato sul controller
    controller.users[user_token]["Name"] = "Mario Rossini"
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == {pid} and db.get_person(pid)["name"] == "Mario Rossini"

    # Sospesa sul controller
    controller.credentials[token]["Enabled"] = False
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == {pid}

    # Sparita dal controller: è uno stato del controller, non un dato della persona
    saved = controller.credentials.pop(token)
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == set() and db.get_card(db.card_by_token(token)["id"])["missing_since"]
    # Tornata uguale: neanche
    controller.credentials[token] = saved
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == set() and not db.card_by_token(token)["missing_since"]

    # Riscritta sul controller con Riscrivi: token nuovo, dati della persona uguali
    controller.credentials.pop(token)
    await plant.access.sync_from_controller()
    with watch(db, clock) as step:
        result = await plant.access.restore_missing("admin")
        await plant.access.sync_from_controller()
    assert result["restored"] == 1 and step.changed() == set()

    # Tolta dall'app perché non c'è più sul controller: la persona ha una tessera in meno
    gone = db.get_person(pid)["cards"][0]
    controller.credentials.pop(gone["axis_credential_token"])
    await plant.access.sync_from_controller()
    with watch(db, clock) as step:
        plant.access.forget_missing("admin", [gone["id"]])
    assert step.changed() == {pid}

    # Tessera passata sul controller da Anna a Mario: tutti e due
    anna_card = db.get_person(anna["id"])["cards"][0]["axis_credential_token"]
    controller.credentials[anna_card]["UserToken"] = user_token
    with watch(db, clock) as step:
        await plant.access.sync_from_controller()
    assert step.changed() == {pid, anna["id"]}


async def test_import_and_da_registrare_change_it(plant, db, clock, controller):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin")
    anna = await plant.access.create_person("Anna Bianchi", "", "admin")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, {})
    with watch(db, clock) as step:
        result = await import_persons(plant.access, text, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["saltate"] == 2 and step.changed() == set()

    edited = "nome;tessera;ditta\nMario Rossi;04aa;Pulizie Srl\nAnna Bianchi;;\n"
    with watch(db, clock) as step:
        result = await import_persons(plant.access, edited, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["aggiornate"] == 1 and step.changed() == {mario["id"]}
    with watch(db, clock) as step:
        result = await import_persons(plant.access, edited, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["saltate"] == 2 and step.changed() == set()

    # Da Registrare: la tessera letta dal lettore assegnata a una persona che c'è (A Persona Esistente)
    db.note_unknown_card("04ff", clock())
    with watch(db, clock) as step:
        await plant.access.add_card(anna["id"], "04ff", "admin")
    assert step.changed() == {anna["id"]} and db.list_unknown_cards() == []


async def test_restore_from_a_backup_changes_it_only_when_something_comes_back(plant, db, clock):
    mario = await plant.access.create_person("Mario Rossi", "04aa", "admin", company="Ditta")
    anna = await plant.access.create_person("Anna Bianchi", "", "admin")
    backup = await plant.backup()
    with watch(db, clock) as step:
        items, _ = await plant.restore(backup["id"], ["persone"])
    assert items == [] and step.changed() == set()

    await plant.access.update_person(mario["id"], "admin", company="Altra Ditta")
    await plant.access.delete_person(anna["id"], "admin")
    with watch(db, clock) as step:
        _, result = await plant.restore(backup["id"], ["persone"])
    assert not result["failed"] and step.changed() == {mario["id"], anna["id"]}
    again = db.get_person(anna["id"])
    # Ricreata: la data di inserimento di allora, l'Ultima Modifica di adesso
    assert again["created_at"] == anna["created_at"] and again["updated_at"] == clock()


async def test_groups_passed_to_categories_change_the_category_of_the_person(plant, db, clock, controller):
    db.set_setting("default_group", "profilo_sempre")
    anna = await plant.access.create_person("Anna", "04aa", "admin", group="profilo_sempre")
    db.set_person_category(anna["id"], 0)
    with watch(db, clock) as step:
        assert await plant.rules.adopt_groups()
    assert step.changed() == {anna["id"]} and db.get_person(anna["id"])["category_id"]
    with watch(db, clock) as step:
        assert await plant.rules.adopt_groups() == 0
    assert step.changed() == set()


async def test_controller_details_of_the_person_do_not_change_it(plant, db, clock):
    mario = await plant.access.create_person("Mario Rossi", "", "admin")
    with watch(db, clock) as step:
        db.set_person_user_token(mario["id"], "utente_mario_x")
        db.mark_cards_missing([], clock())
    assert step.changed() == set()


async def test_plan_of_a_restore_does_not_change_it(plant, db, clock):
    await plant.access.create_person("Mario Rossi", "04aa", "admin")
    backup = await plant.backup()
    _, saved = plant.backups.load(backup["id"])
    with watch(db, clock) as step:
        current, _ = await plant.backups.snapshot(plant.clients)
        plan(saved, current, ["persone"], plant.controller_id)
        await plant.backup(skip=True)
    assert step.changed() == set()
