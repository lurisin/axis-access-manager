import pytest

from app.service import AccessService, ServiceError
from app.transfer import events_csv, import_persons, parse_date, read_import

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


def test_parse_date():
    assert parse_date("25/10/2026") == "2026-10-25T00:00:00"
    assert parse_date("2026-10-25 08:30") == "2026-10-25T08:30:00"
    assert parse_date("") == ""
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
    assert preview["riepilogo"] == {"righe": 5, "ok": 2, "saltate": 1, "errori": 2}
    assert len(db.all_persons()) == 1

    result = await import_persons(service, text, GROUPS, "admin", apply=True)
    assert result["riepilogo"]["ok"] == 2
    mario = next(p for p in db.list_persons() if p["name"] == "Mario")
    assert mario["company"] == "Brillante"
    assert mario["cards"][0]["access_profile"] == "p2"
    assert mario["cards"][0]["valid_to"].startswith("2026-12-3")
    luca = next(p for p in db.list_persons() if p["name"] == "Luca")
    assert len(luca["cards"]) == 2


def test_events_csv_is_excel_friendly():
    text = events_csv([{"ts": "2026-10-25T08:00:00+00:00", "kind": "concesso", "person_name": "Anna", "card": "04aa"}])
    assert text.startswith("﻿Data e Ora;Evento;Persona")
    assert "Accesso Concesso;Anna;04aa" in text
