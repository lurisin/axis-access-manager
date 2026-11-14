"""Preferenze di ogni utente (api/me/preferences): le colonne dell'elenco Persone scelte e ordinate da chi guarda,
salvate sul server per nome utente. Le stesse da Home Assistant e dall'accesso diretto, mai di un altro utente, solo
colonne conosciute, con il permesso Anagrafica, fuori dai backup dell'app. Niente cambia sul controller."""

import json

from test_direct import ADMIN, SAFE, clients, enable, fast_hash, login, ready, staff  # noqa: F401

from app.backup import common_snapshot
from app.db import preferences_key
from app.preferences import PERSON_COLUMNS, PERSON_COLUMNS_DEFAULT, PREFERENCES_MAX, clean_person_columns

URL = "/api/me/preferences"
MARIA = {"X-Remote-User-Name": "maria", "X-Remote-User-Display-Name": "Maria"}
CHOICE = ["cognome", "nome", "telefono", "email", "ditta", "foto", "stato"]


def test_columns_start_from_the_defaults_and_stay_per_user(clients):  # noqa: F811
    ingress, _, _ = clients
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": None}
    response = ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"person_columns": CHOICE}
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": CHOICE}
    # Un altro utente non vede la scelta dell'amministratore, e la sua non tocca quella dell'amministratore
    staff(ingress, "maria", ("persone",))
    assert ingress.get(URL, headers=MARIA).json() == {"person_columns": None}
    mine = ["nome", "cognome", "note"]
    assert ingress.put(URL, json={"person_columns": mine}, headers=MARIA).json() == {"person_columns": mine}
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": CHOICE}
    assert ingress.get(URL, headers=MARIA).json() == {"person_columns": mine}
    # null torna alle predefinite; anche la scelta uguale alle predefinite si salva come «predefinite», così chi non
    # ha cambiato niente segue quelle di una versione futura
    assert ingress.put(URL, json={"person_columns": None}, headers=ADMIN).json() == {"person_columns": None}
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": None}
    default = list(PERSON_COLUMNS_DEFAULT)
    assert ingress.put(URL, json={"person_columns": default}, headers=MARIA).json() == {"person_columns": None}
    # Un PUT senza preferenze non cambia niente
    ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN)
    assert ingress.put(URL, json={}, headers=ADMIN).json() == {"person_columns": CHOICE}


def test_only_known_columns_once_with_surname_and_name(clients):  # noqa: F811
    ingress, _, app = clients
    ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN)
    refused = {
        "colonna sconosciuta: meteo": ["cognome", "nome", "meteo"],
        "colonna ripetuta: ditta": ["cognome", "nome", "ditta", "ditta"],
        "Cognome e Nome restano sempre nell'elenco": ["cognome", "ditta"],
    }
    for detail, columns in refused.items():
        response = ingress.put(URL, json={"person_columns": columns}, headers=ADMIN)
        assert response.status_code == 400 and response.json()["detail"] == detail, columns
    assert ingress.put(URL, json={"person_columns": ["foto"]}, headers=ADMIN).status_code == 400
    # Tipi e dimensioni sbagliati: dati non validi, con il nome del campo in parole
    for body in (
        {"person_columns": "cognome,nome"},
        {"person_columns": ["cognome", "nome", 3]},
        {"person_columns": ["cognome", "nome", "x" * 31]},
        {"person_columns": [*PERSON_COLUMNS, "cognome"]},
        {"colonne": ["cognome", "nome"]},
    ):
        response = ingress.put(URL, json=body, headers=ADMIN)
        assert response.status_code == 422, body
        assert response.json()["detail"].startswith("dati non validi"), body
    assert "colonne dell'elenco" in ingress.put(URL, json={"person_columns": "x"}, headers=ADMIN).json()["detail"]
    bad = ingress.put(URL, content=b"{non json", headers={**ADMIN, "Content-Type": "application/json"})
    assert bad.status_code == 422
    # Dimensione massima: oltre si risponde subito, senza leggere tutto
    big = json.dumps({"person_columns": ["cognome", "nome"], "x": "y" * PREFERENCES_MAX})
    response = ingress.put(URL, content=big, headers={**ADMIN, "Content-Type": "application/json"})
    assert response.status_code == 413
    # Niente di tutto questo è stato salvato
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": CHOICE}
    assert json.loads(app.state.db.get_setting(preferences_key("admin"))) == {"person_columns": CHOICE}
    # Tutte le colonne, in qualsiasi ordine, si salvano
    every = list(reversed(PERSON_COLUMNS))
    assert ingress.put(URL, json={"person_columns": every}, headers=ADMIN).json() == {"person_columns": every}


def test_a_choice_saved_by_a_future_version_is_cleaned_without_errors(clients):  # noqa: F811
    ingress, _, app = clients
    db = app.state.db
    saved = {"person_columns": ["foto", "reparto", "nome", "ditta", "ditta", 7, "stato"], "event_columns": ["ora"]}
    db.set_setting(preferences_key("admin"), json.dumps(saved))
    # Colonna sconosciuta e doppioni tolti, Cognome rimesso accanto a Nome
    assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": ["foto", "cognome", "nome", "ditta", "stato"]}
    # Le preferenze che questa versione non conosce restano quando si salvano le colonne
    ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN)
    assert json.loads(db.get_setting(preferences_key("admin"))) == {"person_columns": CHOICE, "event_columns": ["ora"]}
    # Salvataggi rotti: le predefinite, senza errori
    for text in ("{rotto", "[1, 2]", '"testo"', json.dumps({"person_columns": "cognome"})):
        db.set_setting(preferences_key("admin"), text)
        assert ingress.get(URL, headers=ADMIN).json() == {"person_columns": None}, text
    # E da lì si salva di nuovo
    assert ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN).json() == {"person_columns": CHOICE}


def test_cleaning_keeps_surname_and_name():
    assert clean_person_columns(None) is None
    assert clean_person_columns({"cognome": 1}) is None
    assert clean_person_columns([]) == ["cognome", "nome"]
    assert clean_person_columns(["foto", "stato"]) == ["foto", "cognome", "nome", "stato"]
    assert clean_person_columns(["stato", "foto"]) == ["cognome", "nome", "stato", "foto"]
    assert clean_person_columns(["ditta", "nome"]) == ["ditta", "cognome", "nome"]
    assert clean_person_columns(["cognome", "ditta"]) == ["cognome", "nome", "ditta"]
    assert clean_person_columns(["nome", "cognome", "NOME", "nome"]) == ["nome", "cognome"]


def test_columns_need_the_registry_permission(clients):  # noqa: F811
    ingress, _, _ = clients
    staff(ingress, "maria", ("stato",))
    # Senza Anagrafica le colonne dell'elenco non ci sono, e non si cambiano
    assert ingress.get(URL, headers=MARIA).json() == {}
    response = ingress.put(URL, json={"person_columns": CHOICE}, headers=MARIA)
    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "il tuo utente non ha il permesso «Vedere l'anagrafica: persone, tessere e tipologie»"
    )
    # Basta vedere l'anagrafica: le colonne sono un modo di guardarla, non una modifica
    assert ingress.put("/api/users/maria", json={"perms": ["persone"]}, headers=ADMIN).status_code == 200
    assert ingress.put(URL, json={"person_columns": CHOICE}, headers=MARIA).json() == {"person_columns": CHOICE}


def test_the_same_choice_from_home_assistant_and_from_direct_access(clients):  # noqa: F811
    ingress, site, _ = clients
    enable(ingress)
    # Senza login niente preferenze dall'accesso diretto
    assert site.get(URL).status_code == 401
    ready(ingress, site, "maria", ("persone",))
    assert site.get(URL).json() == {"person_columns": None}
    response = site.put(URL, json={"person_columns": CHOICE}, headers=SAFE)
    assert response.status_code == 200, response.text
    # La ritrova da Home Assistant, con lo stesso nome utente, e da un altro browser dell'accesso diretto
    assert ingress.get(URL, headers=MARIA).json() == {"person_columns": CHOICE}
    other = type(site)(site.app, base_url=str(site.base_url))
    other.portal = site.portal
    assert login(other, "maria", "Reception-Verde-7").json()["ok"] is True
    assert other.get(URL).json() == {"person_columns": CHOICE}
    mine = ["nome", "cognome"]
    assert ingress.put(URL, json={"person_columns": mine}, headers=MARIA).json() == {"person_columns": mine}
    assert site.get(URL).json() == {"person_columns": mine}
    # Le modifiche dall'accesso diretto passano dalle protezioni di sempre
    assert site.put(URL, json={"person_columns": CHOICE}).status_code == 403
    # Un altro utente dell'accesso diretto, nello stesso browser dopo l'uscita, ha le sue
    assert site.post("/api/logout", headers=SAFE).status_code == 200
    ready(ingress, site, "luca", ("persone",))
    assert site.get(URL).json() == {"person_columns": None}


def test_preferences_leave_with_the_user_and_stay_out_of_app_backups(clients):  # noqa: F811
    ingress, _, app = clients
    db = app.state.db
    staff(ingress, "maria", ("persone",))
    ingress.put(URL, json={"person_columns": CHOICE}, headers=MARIA)
    ingress.put(URL, json={"person_columns": CHOICE}, headers=ADMIN)
    # Non sono Dati Comuni: i backup dell'app non le hanno, e un backup uguale resta uguale
    snapshot = common_snapshot(db, [])
    assert not any(key.startswith("preferences:") for key in snapshot["settings"])
    assert "preferenze" not in json.dumps(snapshot) and "telefono" not in json.dumps(snapshot["settings"])
    # Utente eliminato: se ne vanno con lui, e chi arriva dopo con lo stesso nome parte dalle predefinite
    assert ingress.delete("/api/users/maria", headers=ADMIN).status_code == 200
    assert db.get_setting(preferences_key("maria")) == ""
    staff(ingress, "maria", ("persone",))
    assert ingress.get(URL, headers=MARIA).json() == {"person_columns": None}


def test_preferences_follow_a_username_changed_in_home_assistant(clients):  # noqa: F811
    ingress, _, app = clients
    first = {"X-Remote-User-Name": "giulia", "X-Remote-User-Id": "id-giulia", "X-Remote-User-Display-Name": "Giulia"}
    assert ingress.get("/api/me", headers=first).status_code == 200
    assert ingress.put("/api/users/giulia", json={"perms": ["persone"]}, headers=ADMIN).status_code == 200
    ingress.put(URL, json={"person_columns": CHOICE}, headers=first)
    # Stessa persona di Home Assistant, nome utente nuovo: i permessi e le colonne la seguono
    moved = {**first, "X-Remote-User-Name": "giulia.r"}
    assert ingress.get(URL, headers=moved).json() == {"person_columns": CHOICE}
    assert app.state.db.get_setting(preferences_key("giulia")) == ""
