from datetime import UTC, datetime

import pytest

from app.rules import normalize_validity
from app.service import AccessService, ServiceError
from app.transfer import events_csv, import_persons, parse_date, persons_csv, read_import

GROUPS = [{"token": "profilo_sempre", "name": "Sempre"}, {"token": "p2", "name": "Pulizie"}]


def _local_day(value: str) -> str:
    """Giorno locale di una validità salvata in UTC: il 31/12 a mezzanotte in UTC può essere già il 1° gennaio."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().date().isoformat()


@pytest.fixture
def service(db, controller, settings):
    controller.profiles["p2"] = {"token": "p2", "Name": "Pulizie", "AccessPolicy": []}
    return AccessService(db, controller, settings)


def test_read_import_with_header_and_without():
    rows = read_import("﻿Nome;Tessera;Gruppo\nMario;04aa;Pulizie\n\n")
    assert rows == [{"nome": "Mario", "tessera": "04aa", "gruppo": "Pulizie", "riga": 2}]
    rows = read_import("Anna,04bb")
    assert rows[0]["nome"] == "Anna" and rows[0]["tessera"] == "04bb" and rows[0]["riga"] == 1
    # Senza intestazione le celle vuote non ci sono: lasciano i valori attuali invece di svuotarli
    rows = read_import("Anna;04aa;;;;;;;\r\nLuca;04bb;;;31/12/2026\r\n")
    assert rows[0] == {"nome": "Anna", "tessera": "04aa", "riga": 1}
    assert rows[1]["valida_al"] == "31/12/2026" and "ditta" not in rows[1]
    with pytest.raises(ServiceError, match="vuoto"):
        read_import(";;;;\r\n;;;;\r\n")


def test_parse_date():
    assert parse_date("25/10/2026") == "2026-10-25T00:00:00"
    assert parse_date("2026-10-25 08:30") == "2026-10-25T08:30:00"
    # Con i secondi, come nel file esportato
    assert parse_date("31/12/2026 23:59:59") == "2026-12-31T23:59:59"
    assert parse_date("") == ""
    # "Valida al 31/12" comprende tutto il 31
    assert parse_date("31/12/2026", end_of_day=True) == "2026-12-31T23:59:59"
    assert parse_date("31/12/2026 08:00", end_of_day=True) == "2026-12-31T08:00:00"
    with pytest.raises(ServiceError):
        parse_date("domani")


async def test_import_preview_then_apply(service, controller, db):
    await service.create_person("Luca", "04cc", "admin")
    text = "\n".join(
        [
            "nome;tessera;gruppo;valida_dal;valida_al;ditta",
            "Mario;04aa;Pulizie;;31/12/2026;Brillante",
            "Luca;04dd",
            "X;04cc",
            "Y;zz",
            "Z;04ee;Boh",
        ]
    )

    preview = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert preview["riepilogo"] == {"righe": 5, "ok": 2, "aggiornate": 0, "saltate": 1, "errori": 2}
    assert len(db.all_persons()) == 1

    result = await import_persons(service, text, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["ok"] == 2
    mario = next(p for p in db.list_persons() if p["name"] == "Mario")
    assert mario["company"] == "Brillante"
    assert mario["cards"][0]["access_profile"] == "p2"
    assert _local_day(mario["cards"][0]["valid_to"]) == "2026-12-31"
    luca = next(p for p in db.list_persons() if p["name"] == "Luca")
    assert len(luca["cards"]) == 2


async def test_import_updates_registered_cards(service, controller, db):
    """Tessere già registrate: si aggiornano gruppo, validità e ditta della persona se nel file sono diversi."""
    await service.create_person("Anna", "04aa", "admin")
    await service.add_card(db.all_persons()[0]["id"], "04ab", "admin")
    text = "\n".join(
        [
            "nome;tessera;gruppo;valida_al;ditta",
            "Anna;04aa;Pulizie;31/12/2026;Brillante",
            "Anna;04ab;Pulizie;31/12/2026;Brillante",
        ]
    )
    preview = await import_persons(service, text, GROUPS, "admin", apply=False)
    first, second = preview["righe"]
    assert first["esito"] == "da aggiornare"
    assert "gruppo Sempre → Pulizie" in first["dettaglio"] and "ditta — → Brillante" in first["dettaglio"]
    # Seconda tessera della stessa persona, stessi valori: già coperta dalla prima riga
    assert second["esito"] == "saltata"
    assert db.list_persons()[0]["company"] == ""

    result = await import_persons(service, text, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["aggiornate"] == 1
    anna = db.list_persons()[0]
    assert anna["company"] == "Brillante"
    assert {c["access_profile"] for c in anna["cards"]} == {"p2"}
    assert all(_local_day(c["valid_to"]) == "2026-12-31" for c in anna["cards"])

    # Reimportare lo stesso file non cambia più niente; valori diversi per la stessa persona sono un errore
    again = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert again["riepilogo"]["saltate"] == 2
    clash = text.replace("Anna;04ab;Pulizie", "Anna;04ab;Sempre")
    rows = (await import_persons(service, clash, GROUPS, "admin", apply=False))["righe"]
    assert rows[1]["esito"] == "errore" and "stessa persona" in rows[1]["dettaglio"]


async def test_import_names_the_owner_of_a_card_with_another_name(service, db):
    """Tessera registrata a Paolo in una riga con il nome di Giulia: le modifiche vanno a Paolo, e la riga lo dice."""
    paolo = await service.create_person("Paolo Verdi", "04aa", "admin", company="Edilnord", notes="storico")
    text = "nome;tessera;gruppo;valida_dal;valida_al;ditta;note\nGiulia Neri;04aa;;;;Pulizie Srl;"
    for apply in (False, True):
        [row] = (await import_persons(service, text, GROUPS, "admin", apply=apply))["righe"]
        assert row["dettaglio"].startswith("tessera di Paolo Verdi (il nome del file non si usa); ditta Edilnord")
    assert db.get_person(paolo["id"])["company"] == "Pulizie Srl"
    # Stesso nome scritto in un altro modo: nessuna avvertenza
    text = "nome;tessera;ditta\n  paolo   VERDI ;04aa;Brillante"
    [row] = (await import_persons(service, text, GROUPS, "admin", apply=False))["righe"]
    assert row["dettaglio"] == "ditta Pulizie Srl → Brillante"


async def test_export_then_reimport_changes_nothing(service, db):
    """Il file esportato si reimporta com'è, anche risalvato da un foglio di calcolo senza secondi."""
    await service.create_person("Marco", "04123456", "admin", valid_to="2026-12-31T23:59:59", notes="=1+1")
    bruno = await service.create_person("Bruno", "04bb", "admin")
    await service.set_card_enabled(bruno["cards"][0]["id"], False, "admin")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses())
    # Tessere di sole cifre e testi come formule protetti per il foglio di calcolo
    assert '"=""04123456"""' in text and "'=1+1" in text
    for variant in (text, text.replace("23:59:59", "23:59"), text.replace('"=""04123456"""', "04123456")):
        result = await import_persons(service, variant, GROUPS, "admin", apply=False)
        assert result["riepilogo"]["saltate"] == 2
    # Tessere rovinate dal foglio di calcolo: errore, non una tessera nuova
    mangled = "nome;tessera\nMarco;4123456\nMarco;6,33031E+15"
    rows = (await import_persons(service, mangled, GROUPS, "admin", apply=False))["righe"]
    assert [r["esito"] for r in rows] == ["errore", "errore"]


async def test_import_suspended_and_new_card_of_existing_person(service, controller, db):
    bruno = await service.create_person("Bruno", "04bb", "admin")
    await service.set_card_enabled(bruno["cards"][0]["id"], False, "admin")
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses())
    await service.delete_person(bruno["id"], "admin")
    rows = (await import_persons(service, text, GROUPS, "admin", apply=True))["righe"]
    assert rows[0]["dettaglio"] == "nuova persona, tessera sospesa" and rows[0]["etichetta"] == "Importata"
    card = db.list_persons()[0]["cards"][0]
    assert not card["enabled"] and controller.credentials[card["axis_credential_token"]]["Enabled"] is False

    # Tessera nuova con un'altra validità: aggiorna la persona invece di ignorarla
    text = "nome;tessera;valida_al\nBruno;04bc;15/11/2026 12:00"
    row = (await import_persons(service, text, GROUPS, "admin", apply=False))["righe"][0]
    assert row["esito"] == "da aggiornare" and row["dettaglio"].startswith("tessera aggiunta; valida al")
    await import_persons(service, text, GROUPS, "admin", apply=True)
    assert [c["valid_to"] for c in db.list_persons()[0]["cards"]] == [normalize_validity("2026-11-15T12:00")] * 2


async def test_persons_without_card_are_exported_and_reimported(service, controller, db):
    category = db.save_category(
        None, {"name": "Ospite", "color": "blue", "group_token": "p2", "valid_days": 0, "description": ""}
    )
    anna = await service.create_person("Anna", "", "admin", company="Pulizie", category_id=category)
    text = persons_csv(db.list_persons(), {"profilo_sempre": "Sempre"}, db.last_accesses(), {category: "Ospite"})
    # Una riga anche per chi non ha tessere, con tessera, gruppo, validità e stato vuoti
    assert "Anna;;;;;Pulizie;;Ospite;;" in text

    rows = (await import_persons(service, text, GROUPS, "admin", apply=False))["righe"]
    assert [(r["esito"], r["dettaglio"]) for r in rows] == [("saltata", "già registrata, senza tessera")]
    await service.delete_person(anna["id"], "admin")
    controller.calls.clear()
    rows = (await import_persons(service, text, GROUPS, "admin", apply=True))["righe"]
    assert [(r["esito"], r["dettaglio"]) for r in rows] == [("ok", "nuova persona senza tessera")]
    [person] = db.list_persons()
    assert (person["name"], person["company"], person["category_id"], person["cards"]) == (
        "Anna",
        "Pulizie",
        category,
        [],
    )
    assert controller.calls == []


async def test_import_row_without_card(service, db):
    await service.create_person("Luca", "04cc", "admin", valid_to="2026-12-31T23:59:59")
    text = "\n".join(
        [
            "nome;tessera;gruppo;valida_al;ditta;stato",
            "Mario;;;;Brillante;",
            "Luca;;;;Teatro;",
            "Paolo;;Sempre;;;",
            "Sara;;;31/12/2026;;",
            "Gino;;;;;attiva",
        ]
    )
    rows = (await import_persons(service, text, GROUPS, "admin", apply=True))["righe"]
    assert [r["esito"] for r in rows] == ["ok", "aggiornata", "errore", "errore", "errore"]
    assert all("senza tessera" in r["dettaglio"] for r in rows[2:])
    persons = {p["name"]: p for p in db.list_persons()}
    assert set(persons) == {"Luca", "Mario"}
    assert persons["Mario"]["cards"] == [] and persons["Mario"]["company"] == "Brillante"
    # Ditta di chi ha già tessere: la validità delle sue tessere non cambia
    assert persons["Luca"]["company"] == "Teatro"
    assert persons["Luca"]["cards"][0]["valid_to"] == normalize_validity("2026-12-31T23:59:59")
    # Senza intestazione basta il nome
    rows = (await import_persons(service, "Nina", GROUPS, "admin", apply=False))["righe"]
    assert rows[0]["esito"] == "da importare" and rows[0]["dettaglio"] == "nuova persona senza tessera"


def test_events_csv_is_excel_friendly():
    text = events_csv([{"ts": "2026-10-25T08:00:00+00:00", "kind": "concesso", "person_name": "Anna", "card": "04aa"}])
    assert text.startswith("﻿Data e Ora;Evento;Persona")
    assert "Accesso Concesso;Anna;04aa" in text


class _Clock(datetime):
    """Ora ferma: la durata della tipologia conta da adesso, e due importazioni devono dare la stessa scadenza."""

    @classmethod
    def now(cls, tz=None):
        moment = datetime(2026, 11, 7, 9, 30, tzinfo=UTC)
        return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)


FIRST_GROUPS = [
    {"token": "tutte_porte", "name": "Tutte le Porte"},
    {"token": "solo_ingresso", "name": "Solo Ingresso"},
    {"token": "p2", "name": "Pulizie"},
]


@pytest.fixture
def first_card(db, controller, settings, monkeypatch):
    """Dipendenti (tutte le porte, senza scadenza) predefinita, Visitatori (solo ingresso, 1 giorno), Corsisti
    (Pulizie, 30 giorni). Il gruppo predefinito è quello della tipologia predefinita, come dopo set_default_category."""
    monkeypatch.setattr("app.service.datetime", _Clock)
    for group in FIRST_GROUPS:
        controller.profiles[group["token"]] = {"token": group["token"], "Name": group["name"], "AccessPolicy": []}
    categories = {}
    kinds = (("Dipendenti", "tutte_porte", 0), ("Visitatori", "solo_ingresso", 1), ("Corsisti", "p2", 30))
    for name, token, days in kinds:
        categories[name] = db.save_category(
            None, {"name": name, "color": "blue", "group_token": token, "valid_days": days, "description": ""}
        )
    db.set_setting("default_category", str(categories["Dipendenti"]))
    db.set_setting("default_group", "tutte_porte")
    return AccessService(db, controller, settings), categories


def _on_controller(controller, db, person_id: int) -> tuple:
    [card] = db.get_person(person_id)["cards"]
    credential = controller.credentials[card["axis_credential_token"]]
    return (
        (card["access_profile"], card["valid_from"], card["valid_to"]),
        (credential["CredentialAccessProfile"][0]["AccessProfile"], credential["ValidFrom"], credential["ValidTo"]),
    )


@pytest.mark.parametrize(
    "group, valid_to, category, profile, expiry, detail",
    [
        # Solo la scadenza: il gruppo resta quello della tipologia della persona, non il predefinito
        ("", "31/12/2027", "Visitatori", "solo_ingresso", "2027-12-31T23:59:59", "gruppo Solo Ingresso, valida al "),
        # Solo il gruppo: la durata della tipologia resta
        ("Pulizie", "", "Visitatori", "p2", "2026-11-08T09:30:00Z", "gruppo Pulizie, valida dal "),
        # Un'altra tipologia: il suo gruppo e la sua durata, e la persona passa a lei
        ("", "", "Corsisti", "p2", "2026-12-07T09:30:00Z", "(30 giorni, durata della tipologia Corsisti)"),
        # Niente nella riga: la tipologia della persona, non quella predefinita
        ("", "", "", "solo_ingresso", "2026-11-08T09:30:00Z", "(1 giorno, durata della tipologia Visitatori)"),
    ],
)
async def test_first_card_of_a_registered_person_is_like_a_new_person(
    first_card, controller, db, group, valid_to, category, profile, expiry, detail
):
    """Esporta, scrivi tessera (e scadenza, gruppo o tipologia) nella riga di chi non ha tessere, reimporta: la tessera
    nasce come per una persona nuova con la stessa riga, con una sola scrittura sul controller."""
    service, categories = first_card
    visitors = categories["Visitatori"]
    rossi = await service.create_person("Ospite Rossi", "", "admin", company="Ditta", category_id=visitors)
    names = {g["token"]: g["name"] for g in FIRST_GROUPS}
    exported = persons_csv(db.list_persons(), names, {}, {i: n for n, i in categories.items()})
    header, row = exported.lstrip("﻿").splitlines()[:2]
    # In fondo ultimo accesso, telefono ed email
    assert row == "Ospite Rossi;;;;;Ditta;;Visitatori;;;;"
    cells = row.split(";")
    cells[1], cells[2], cells[4], cells[7] = "04AA", group, valid_to, category
    text = "\n".join([header, ";".join(cells)])

    [preview] = (await import_persons(service, text, FIRST_GROUPS, "admin", apply=False))["righe"]
    assert preview["dettaglio"].startswith("tessera aggiunta; ") and detail in preview["dettaglio"]
    controller.calls.clear()
    [applied] = (await import_persons(service, text, FIRST_GROUPS, "admin", apply=True))["righe"]
    assert applied["esito"] != "errore" and detail in applied["dettaglio"]
    assert applied["dettaglio"] == preview["dettaglio"]

    # La stessa riga per una persona nuova (senza tipologia varrebbe la predefinita: si scrive quella di Rossi)
    cells[0], cells[1], cells[7] = "Ospite Bianchi", "04BB", category or "Visitatori"
    text = "\n".join([header, ";".join(cells)])
    [new] = (await import_persons(service, text, FIRST_GROUPS, "admin", apply=True))["righe"]
    assert new["esito"] == "ok"
    bianchi = next(p for p in db.list_persons() if p["name"] == "Ospite Bianchi")

    existing, created = _on_controller(controller, db, rossi["id"]), _on_controller(controller, db, bianchi["id"])
    assert existing == created
    assert existing[1][0] == profile and existing[1][2] == normalize_validity(expiry)
    assert db.get_person(rossi["id"])["category_id"] == bianchi["category_id"]
    token = db.get_person(rossi["id"])["cards"][0]["axis_credential_token"]
    # Scritta una volta sola, già con il suo accesso: mai con il gruppo predefinito, neanche per un momento
    assert [c for c in controller.calls if c == ("set_credential", token)] == [("set_credential", token)]
    assert db.get_person(rossi["id"])["company"] == "Ditta"


async def test_first_card_changes_the_category_and_the_company_together(first_card, controller, db):
    service, categories = first_card
    rossi = await service.create_person("Ospite Rossi", "", "admin", category_id=categories["Visitatori"])
    text = "nome;tessera;ditta;tipologia\nOspite Rossi;04AA;Brillante;Corsisti"

    [preview] = (await import_persons(service, text, FIRST_GROUPS, "admin", apply=False))["righe"]
    assert preview["esito"] == "da aggiornare"
    assert "; ditta — → Brillante" in preview["dettaglio"] and "; tipologia → Corsisti" in preview["dettaglio"]
    assert db.get_person(rossi["id"])["category_id"] == categories["Visitatori"]
    [row] = (await import_persons(service, text, FIRST_GROUPS, "admin", apply=True))["righe"]
    assert row["esito"] == "aggiornata"
    person = db.get_person(rossi["id"])
    assert (person["category_id"], person["company"], person["cards"][0]["access_profile"]) == (
        categories["Corsisti"],
        "Brillante",
        "p2",
    )
    actions = [a["action"] for a in db.list_audit()]
    # Nessuna riscrittura dell'accesso dopo la tessera
    assert "accesso modificato" not in actions and "tipologia cambiata" in actions


async def test_row_without_card_read_before_a_card_arrives(first_card, controller, db, monkeypatch):
    """Riga senza tessera con un'altra tipologia, letta quando la persona non aveva tessere: se intanto ne ha avuta
    una (dalla pagina Da Registrare) la riga non le toglie la scadenza."""
    service, categories = first_card
    rossi = await service.create_person("Ospite Rossi", "", "admin", category_id=categories["Visitatori"])
    before = db.get_person(rossi["id"])
    [card] = (await service.add_card(rossi["id"], "04AA", "admin"))["cards"]
    assert card["valid_to"]
    # L'importazione legge la persona prima della tessera
    stale = iter([before])
    real = db.get_person
    monkeypatch.setattr(db, "get_person", lambda person_id: next(stale, None) or real(person_id))
    controller.calls.clear()

    [row] = (await import_persons(service, "nome;tessera;tipologia\nOspite Rossi;;Corsisti", [], "admin", True))[
        "righe"
    ]

    assert row["esito"] == "errore" and row["dettaglio"].startswith("Ospite Rossi ha ora 1 tessera")
    [after] = real(rossi["id"])["cards"]
    assert (after["valid_from"], after["valid_to"]) == (card["valid_from"], card["valid_to"])
    assert real(rossi["id"])["category_id"] == categories["Visitatori"] and controller.calls == []
