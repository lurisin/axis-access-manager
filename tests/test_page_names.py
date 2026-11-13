"""Nomi del menu dalla 0.28.3: Persone diventa Anagrafica, Regole diventa Fasce Orarie (titolo Fasce Orarie e
Festività), Da Registrare resta nel menu con il titolo Tessere da Registrare. Gli indirizzi (#persone, #regole,
#da-registrare) e le chiavi dei permessi (salvate nel database e nei backup) restano quelle di prima; cambiano i nomi
brevi dei permessi, le descrizioni e i testi che nominano le pagine."""

import dataclasses
import re
from pathlib import Path

import pytest

from app.events import EventMonitor
from app.service import AccessService, ServiceError
from app.users import PERMISSION_NAMES, PERMISSIONS, ROLES, normalize_perms, role_label

ROOT = Path(__file__).resolve().parents[1] / "axis_access_manager"
PAGE = ROOT / "app" / "static" / "index.html"


def test_permission_keys_stay_and_names_follow_the_pages():
    # Le chiavi sono nel database e nei backup: le stesse, nello stesso ordine
    assert list(PERMISSIONS) == [
        "stato",
        "persone",
        "persone_modifica",
        "registro",
        "regole",
        "regole_modifica",
        "import_export",
        "impianti",
        "backup",
        "utenti",
    ]
    assert list(PERMISSION_NAMES) == list(PERMISSIONS)
    assert PERMISSION_NAMES["persone"] == "Anagrafica"
    assert PERMISSION_NAMES["persone_modifica"] == "Modifica Anagrafica"
    assert PERMISSION_NAMES["regole"] == "Fasce Orarie"
    assert PERMISSION_NAMES["regole_modifica"] == "Modifica Fasce Orarie"
    # Descrizioni della pagina Utenti con il nome della pagina, senza i nomi vecchi
    assert PERMISSIONS["persone"] == "Vedere l'anagrafica: persone, tessere e tipologie"
    assert PERMISSIONS["persone_modifica"].startswith("Modificare l'anagrafica: ")
    assert PERMISSIONS["regole"] == "Vedere fasce orarie, festività e tipologie"
    assert PERMISSIONS["regole_modifica"] == "Modificare fasce orarie, festività e tipologie"
    names = " ".join([*PERMISSION_NAMES.values(), *PERMISSIONS.values()])
    assert "Persone" not in names and "Regole" not in names
    # I ruoli danno gli stessi permessi di prima
    assert ROLES["operatore"][1] == ["stato", "persone", "persone_modifica", "registro", "regole", "import_export"]
    assert ROLES["tessere"][1] == ["persone", "persone_modifica", "regole"]
    assert ROLES["consultazione"][1] == ["stato", "persone", "registro", "regole"]


def test_custom_role_in_activity_and_backups_uses_the_new_names():
    assert role_label(["stato", "persone"]) == "Personalizzato: Stato, Anagrafica"
    # Modifica implica la consultazione
    assert normalize_perms(["regole_modifica"]) == ["regole", "regole_modifica"]
    assert role_label(["regole_modifica"]) == "Personalizzato: Fasce Orarie, Modifica Fasce Orarie"
    assert role_label(["persone_modifica", "registro"]) == "Personalizzato: Anagrafica, Modifica Anagrafica, Registro"
    assert role_label(["persone", "persone_modifica", "regole"]) == "Tessere"


async def test_missing_default_category_points_to_anagrafica(db, controller, settings):
    service = AccessService(
        db, controller, dataclasses.replace(settings, access_profile=""), EventMonitor(prefix="x", door_token="porta")
    )
    with pytest.raises(ServiceError, match="impostala in Anagrafica → Tipologie$"):
        await service.create_person("Mario Rossi", "04aa", "test")
    assert controller.calls == []


def _route(page: str, key: str) -> str:
    match = re.search(rf'^  {re.escape(key)}: \["([^"]*)", (\w+), ', page, re.M)
    assert match, key
    return match.group(1)


def test_menu_and_titles_with_the_old_addresses():
    page = PAGE.read_text(encoding="utf-8")
    nav = dict(
        re.findall(r'<a href="#([\w-]+)" data-route="[\w-]+" title="[^"]*">.*?<span class="lbl">([^<]*)</span>', page)
    )
    assert nav["persone"] == "Anagrafica"
    assert nav["regole"] == "Fasce Orarie"
    assert nav["da-registrare"] == "Da Registrare"
    assert 'data-route="persone" title="Anagrafica"' in page and 'data-route="regole" title="Fasce Orarie"' in page
    # Titolo della pagina (anche quello del browser, «Anagrafica · Controllo Accessi»)
    assert _route(page, "persone") == "Anagrafica"
    assert _route(page, '"da-registrare"') == "Tessere da Registrare"
    assert _route(page, "regole") == "Fasce Orarie e Festività"
    assert "document.title = `${title} · Controllo Accessi`;" in page
    # Le schede dentro le pagine restano con i loro nomi
    assert '[["", "Persone"], ["tipologie", "Tipologie"]]' in page
    assert (
        '[["fasce", "Fasce Orarie"], ["festivita", "Festività"], '
        '...(can("persone") ? [] : [["tipologie", "Tipologie"]])]' in page
    )
    # Nessun testo con i nomi vecchi delle pagine
    for old in (
        "Regole di Accesso",
        "Persone →",
        "Regole →",
        "in Persone",
        '"Persone" : "Regole"',
        "Regole Non Leggibili",
    ):
        assert old not in page, old
    assert "Le tipologie si gestiscono in Anagrafica → Tipologie." in page
    assert (
        'La tipologia predefinita si sceglie in ${can("persone") ? "Anagrafica" : "Fasce Orarie"} → Tipologie.' in page
    )
    assert page.count('href="#persone">Anagrafica</a>') == 2
