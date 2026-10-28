import pytest

from app.rules import normalize_validity
from app.service import AccessService, ServiceError
from app.transfer import events_csv, import_persons, parse_date, persons_csv, read_import

GROUPS = [{"token": "profilo_sempre", "name": "Sempre"}, {"token": "p2", "name": "Pulizie"}]


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
    assert mario["cards"][0]["valid_to"].startswith("2026-12-3")
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
    assert all(c["valid_to"].startswith("2026-12-3") for c in anna["cards"])

    # Reimportare lo stesso file non cambia più niente; valori diversi per la stessa persona sono un errore
    again = await import_persons(service, text, GROUPS, "admin", apply=False)
    assert again["riepilogo"]["saltate"] == 2
    clash = text.replace("Anna;04ab;Pulizie", "Anna;04ab;Sempre")
    rows = (await import_persons(service, clash, GROUPS, "admin", apply=False))["righe"]
    assert rows[1]["esito"] == "errore" and "stessa persona" in rows[1]["dettaglio"]


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


def test_events_csv_is_excel_friendly():
    text = events_csv([{"ts": "2026-10-25T08:00:00+00:00", "kind": "concesso", "person_name": "Anna", "card": "04aa"}])
    assert text.startswith("﻿Data e Ora;Evento;Persona")
    assert "Accesso Concesso;Anna;04aa" in text
