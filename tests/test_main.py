import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.db import Database
from app.ha import HomeAssistant
from app.main import create_app


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as test_client:
        yield test_client


def test_status_without_controller_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "", "mqtt_topic_prefix": "accessi/x"})
    with TestClient(create_app(settings, allow_any_client=True, db_path=tmp_path / "a.db")) as client:
        data = client.get("/api/status").json()
    assert data["controller"] == {"ok": False, "error": "indirizzo del controller non configurato"}
    assert data["mqtt"]["configured"] is False


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Controllo Accessi" in response.text


def test_requests_outside_ingress_are_refused(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=False, controller=controller, db_path=tmp_path / "b.db")
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 403


def test_startup_imports_existing_cards(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    controller.add_existing("utente_mario", "Mario Rossi", "04aa")
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "c.db")
    with TestClient(app) as client:
        client.post("/api/sync")
        persons = client.get("/api/persons").json()
    assert persons[0]["name"] == "Mario Rossi"


def test_person_lifecycle_through_api(client, controller):
    headers = {"X-Remote-User-Display-Name": "Rising Coder"}
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=headers).json()
    card_id = person["cards"][0]["id"]

    assert client.patch(f"/api/persons/{person['id']}", json={"name": "Anna B"}).json()["name"] == "Anna B"
    assert client.patch(f"/api/cards/{card_id}", json={"enabled": False}).json()["cards"][0]["enabled"] is False
    assert client.delete(f"/api/cards/{card_id}").json() == {"ok": True, "person": None}
    assert client.get("/api/persons").json() == []


def test_errors_are_reported_in_italian(client):
    response = client.post("/api/persons", json={"name": "Anna", "card": "non-hex"})
    assert response.status_code == 400
    assert "numero tessera non valido" in response.json()["detail"]


def test_door_cannot_be_opened_from_the_app(client, controller):
    # L'apertura resta in Home Assistant (pulsanti MQTT), non nell'interfaccia dell'add-on
    assert client.post("/api/door/open", json={"extended": False}).status_code in (404, 405)
    assert controller.calls == []


def test_version_matches_config_yaml(client):
    config = Path(__file__).parent.parent / "axis_access_manager" / "config.yaml"
    version = re.search(r'^version:\s*"([^"]+)"', config.read_text(), re.MULTILINE).group(1)
    assert client.get("/api/me").json()["version"] == version


def test_first_user_is_admin_and_others_get_status_only(client, controller):
    admin = {"X-Remote-User-Name": "Rising", "X-Remote-User-Display-Name": "Rising Coder"}
    guest = {"X-Remote-User-Name": "ospite", "X-Remote-User-Display-Name": "Ospite"}
    me = client.get("/api/me", headers=admin).json()
    assert me["admin"] is True and me["username"] == "rising"

    me = client.get("/api/me", headers=guest).json()
    assert me["admin"] is False and me["perms"] == ["stato"] and me["role"] == "Solo Stato"
    assert client.get("/api/status", headers=guest).status_code == 403
    assert client.get("/api/summary", headers=guest).status_code == 200
    assert client.get("/api/door-overview", headers=guest).status_code == 200
    assert client.get("/api/persons", headers=guest).status_code == 403
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=guest).status_code == 403
    assert client.get("/api/events", headers=guest).status_code == 403
    assert client.get("/api/users", headers=guest).status_code == 403
    dashboard = client.get("/api/dashboard", headers=guest).json()
    assert dashboard["recent"] == [] and dashboard["expiring"] == []

    # L'amministratore dà all'ospite il ruolo di consultazione
    users = client.get("/api/users", headers=admin).json()
    assert {u["username"]: u["role"] for u in users["users"]} == {"rising": "amministratore", "ospite": "solo_stato"}
    consult = users["roles"]["consultazione"]["perms"]
    assert client.put("/api/users/ospite", json={"perms": consult}, headers=admin).status_code == 200
    assert client.get("/api/persons", headers=guest).status_code == 200
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=guest).status_code == 403
    assert controller.calls == []

    # Permesso di modifica: implica la consultazione
    response = client.post("/api/users", json={"username": "mario", "perms": ["persone_modifica"]}, headers=admin)
    assert response.json()["perms"] == ["persone", "persone_modifica"]
    mario = {"X-Remote-User-Name": "Mario"}
    assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=mario).status_code == 200
    assert (
        client.post("/api/groups", json={"name": "G", "schedule": "standard_always"}, headers=mario).status_code == 403
    )

    # Sempre almeno un amministratore
    response = client.put("/api/users/rising", json={"perms": []}, headers=admin)
    assert response.status_code == 400 and "amministratore" in response.json()["detail"]
    assert client.delete("/api/users/rising", headers=admin).status_code == 400
    assert client.delete("/api/users/mario", headers=admin).status_code == 200


def test_sections_follow_permissions(client):
    admin = {"X-Remote-User-Name": "rising"}
    client.get("/api/me", headers=admin)
    roles = client.get("/api/users", headers=admin).json()["roles"]
    client.post("/api/users", json={"username": "log", "perms": roles["solo_registro"]["perms"]}, headers=admin)
    client.post("/api/users", json={"username": "tessere", "perms": roles["tessere"]["perms"]}, headers=admin)

    # Solo registro: niente cruscotto, porte e notifiche; i nomi delle porte restano per leggere il registro
    log = {"X-Remote-User-Name": "log"}
    assert client.get("/api/me", headers=log).json()["role"] == "Solo Registro"
    assert client.get("/api/events", headers=log).status_code == 200
    for path in ("/api/dashboard", "/api/door-overview", "/api/notifications", "/api/status", "/api/persons"):
        assert client.get(path, headers=log).status_code == 403, path
    summary = client.get("/api/summary", headers=log).json()
    assert summary["notifications"] == 0 and summary["door_mode"] is None and summary["unknown_cards"] == 0
    assert summary["doors"] and all(d["mode"] is None for d in summary["doors"])

    # Tessere: anagrafica, tipologie e fasce orarie, senza registro né stato
    cards = {"X-Remote-User-Name": "tessere"}
    for path in ("/api/persons", "/api/categories", "/api/schedules", "/api/locations"):
        assert client.get(path, headers=cards).status_code == 200, path
    for path in ("/api/events", "/api/dashboard", "/api/door-overview"):
        assert client.get(path, headers=cards).status_code == 403, path


def test_existing_users_keep_status_after_update(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre"})
    db = Database(tmp_path / "u.db")
    db.save_app_user("admin", "", ["utenti"])
    db.save_app_user("vecchio", "", ["registro"])
    db.save_app_user("stato", "", [])
    db.close()
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "u.db")
    with TestClient(app) as client:
        perms = {
            u["username"]: u["perms"]
            for u in client.get("/api/users", headers={"X-Remote-User-Name": "admin"}).json()["users"]
        }
        assert perms["vecchio"] == ["stato", "registro"] and perms["stato"] == ["stato"]
        # Una volta sola: se l'amministratore toglie "stato", al riavvio non torna
        client.put("/api/users/vecchio", json={"perms": ["registro"]}, headers={"X-Remote-User-Name": "admin"})
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "u.db")
    with TestClient(app) as client:
        users = client.get("/api/users", headers={"X-Remote-User-Name": "admin"}).json()["users"]
        assert next(u for u in users if u["username"] == "vecchio")["perms"] == ["registro"]


def test_readonly_users_option_starts_as_consultation(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre", "readonly_users": ["Ospite"]}
    )
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "r.db")
    with TestClient(app) as client:
        client.get("/api/me", headers={"X-Remote-User-Name": "admin"})
        headers = {"X-Remote-User-Name": "ospite"}
        assert client.get("/api/persons", headers=headers).status_code == 200
        assert client.post("/api/persons", json={"name": "A", "card": "04aa"}, headers=headers).status_code == 403
        assert controller.calls == []


def test_rules_and_dashboard_endpoints(client):
    schedule = client.post(
        "/api/schedules",
        json={"name": "Ufficio", "rules": [{"days": ["MO"], "start": "08:00", "end": "12:00"}], "holidays": True},
    ).json()["token"]
    group = client.post("/api/groups", json={"name": "Dipendenti", "schedule": schedule}).json()["token"]
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "group": group}).json()
    assert client.put(f"/api/persons/{person['id']}/access", json={"group": "profilo_sempre"}).status_code == 200
    assert client.post("/api/holidays/italia", json={"year": 2026}).status_code == 200
    assert len(client.get("/api/holidays").json()) == 12

    groups = client.get("/api/groups").json()
    assert {g["name"] for g in groups["groups"]} == {"Sempre", "Dipendenti"}
    assert client.delete(f"/api/groups/{group}").status_code == 200

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["persons"] == 1 and len(dashboard["week"]) == 7
    detail = client.get(f"/api/persons/{person['id']}").json()
    assert detail["events"] == []


def test_csv_export_and_import(client):
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"})
    response = client.get("/api/export/persons.csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert "Anna;04aa;Sempre" in response.text
    preview = client.post("/api/import/persons", json={"csv": "Mario;04bb;Sempre", "apply": False}).json()
    assert preview["riepilogo"]["ok"] == 1
    assert client.get("/api/export/events.csv").status_code == 200


def test_categories_hidden_and_default_group_endpoints(client):
    door = client.get("/api/installation").json()["controllers"][0]["doors"][0]["id"]
    rules = [{"target": "door", "target_id": door, "schedule": "standard_office_hours"}]
    category = client.post("/api/categories", json={"name": "Pulizie", "color": "teal", "rules": rules}).json()
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "category_id": category["id"]}).json()
    assert person["cards"][0]["access_profile"] == category["token"]
    listed = client.get("/api/categories").json()[0]
    assert listed["persons"] == 1 and listed["rules"] == rules
    assert "Pulizie" in client.get("/api/export/persons.csv").text.splitlines()[1]

    assert (
        client.put("/api/hidden", json={"kind": "schedule", "token": "standard_always", "hidden": True}).status_code
        == 200
    )
    schedules = {s["token"]: s for s in client.get("/api/schedules").json()}
    assert schedules["standard_always"]["hidden"] is True
    assert client.put("/api/default-category", json={"category_id": category["id"]}).status_code == 200
    assert client.get("/api/me").json()["default_group"] == category["token"]
    # Porta usata da una tipologia: non si elimina
    assert client.delete(f"/api/doors/{door}").status_code == 400
    assert client.post(f"/api/persons/{person['id']}/align").status_code == 200
    assert client.delete(f"/api/categories/{category['id']}").status_code == 400


def test_status_lists_controller_door_names(client):
    controller = client.get("/api/status").json()["controller"]
    assert controller["doors"] == ["porta", "porta2"]
    assert controller["door_names"] == ["Ingresso scala", "Cantina"]


def test_old_denial_reasons_are_translated_at_startup(settings, controller, tmp_path, monkeypatch):
    """Dinieghi salvati prima della traduzione con il codice del controller: tradotti al riavvio."""
    from app.db import Database

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    path = tmp_path / "old.db"
    old = Database(path)
    old.add_event(ts="2026-10-25T10:00:00+00:00", kind="negato", detail="NotPermittedAtThisTime")
    old.add_event(ts="2026-10-25T10:00:01+00:00", kind="citofono", detail="Other")
    old.close()
    with TestClient(create_app(settings, allow_any_client=True, controller=controller, db_path=path)) as client:
        details = {e["kind"]: e["detail"] for e in client.get("/api/events").json()}
    assert details == {"negato": "fuori fascia oraria", "citofono": "Other"}


class FakeCamera(HomeAssistant):
    """Home Assistant finto: ogni istantanea è un file con un nome nuovo."""

    def __init__(self, snapshot_dir):
        super().__init__(token="x", snapshot_dir=snapshot_dir)
        self.shots = []

    async def save_snapshot(self, camera, name):
        self.shots.append((camera, name))
        if camera == "camera.spenta":
            return "", "telecamera non disponibile (risposta 503 di Home Assistant)"
        return f"{name}.jpg", ""


def test_one_snapshot_per_passage_and_always_on_remote_open(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre", "snapshot_camera": "camera.x"}
    )
    ha = FakeCamera(tmp_path / "shots")
    app = create_app(
        settings, allow_any_client=True, controller=controller, db_path=tmp_path / "s.db", home_assistant=ha
    )
    with TestClient(app) as client:

        def event(kind, card="", ts="2026-10-25T14:38:28Z", door="porta"):
            row = {"ts": ts, "kind": kind, "card": card, "door": door}
            row["id"] = app.state.db.add_event(**row)
            client.portal.call(app.state.service.after_event, row)
            return row["id"]

        # Tessera sconosciuta: foto solo sull'esito negato, non sulla lettura
        event("sconosciuta", "c0110a99")
        denied = event("negato", "c0110a99")
        assert len(ha.shots) == 1 and ha.shots[0][0] == "camera.x"
        # Il nome del file usa l'ora dell'evento (ora locale), non quella di Home Assistant
        stamp = datetime(2026, 10, 25, 14, 38, 28, tzinfo=UTC).astimezone().strftime("%Y%m%d_%H%M%S")
        assert ha.shots[0][1] == f"{denied}_{stamp}"

        # Apertura remota: istantanea anche senza "istantanea sugli accessi concessi"; lettura e concesso no
        event("apertura_remota")
        event("lettura", "04a11a34")
        event("concesso", "04a11a34")
        assert len(ha.shots) == 2

        # Coperchio del controller aperto: evento senza porta, niente foto; la manomissione di una porta sì
        event("manomissione", door="")
        assert len(ha.shots) == 2
        event("manomissione")
        assert len(ha.shots) == 3
        # Con la telecamera del controller il coperchio ha la sua foto, sempre da quella telecamera
        ctrl = app.state.db.list_controllers()[0]
        assert client.put(f"/api/controllers/{ctrl['id']}/camera", json={"camera": "quadro"}).status_code == 400
        assert client.put(f"/api/controllers/{ctrl['id']}/camera", json={"camera": "camera.quadro"}).status_code == 200
        event("manomissione", door="")
        assert len(ha.shots) == 4 and ha.shots[-1][0] == "camera.quadro"
        assert app.state.db.list_controllers()[0]["snapshot_camera"] == "camera.quadro"

        # Telecamera spenta: l'evento dice perché manca la foto
        door = app.state.db.list_doors()[0]
        client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_camera": "camera.spenta"})
        remote = event("apertura_remota")
        row = next(e for e in client.get("/api/events?limit=5").json() if e["id"] == remote)
        assert row["snapshot"] == "" and "503" in row["snapshot_error"]
        assert app.state.snapshot_errors["camera.spenta"]["error"] == row["snapshot_error"]


def test_snapshots_move_to_the_media_folder(tmp_path):
    old, new = tmp_path / "data" / "snapshots", tmp_path / "media" / "axis_access_manager"
    old.mkdir(parents=True)
    (old / "1_20261025_143828.jpg").write_bytes(b"jpg")
    ha = HomeAssistant(token="", snapshot_dir=new)
    assert ha.move_snapshots(old) == 1
    assert (new / "1_20261025_143828.jpg").read_bytes() == b"jpg" and not old.exists()
    assert ha.move_snapshots(old) == 0


class NamedCameras(FakeCamera):
    """Home Assistant finto con i nomi delle telecamere, per contare quante volte li rilegge."""

    def __init__(self, snapshot_dir):
        super().__init__(snapshot_dir)
        self.reads = 0

    async def states(self):
        self.reads += 1
        return [{"entity_id": "camera.x", "name": "Ingresso Teatro", "state": "streaming"}]


def test_summary_gives_camera_friendly_names(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options({"a1210_host": "a", "door_token": "porta", "snapshot_camera": "camera.x"})
    ha = NamedCameras(tmp_path / "shots")
    app = create_app(
        settings, allow_any_client=True, controller=controller, db_path=tmp_path / "n.db", home_assistant=ha
    )
    with TestClient(app) as client:
        assert client.get("/api/summary").json()["camera_names"] == {"camera.x": "Ingresso Teatro"}
        # Il riepilogo si legge a ogni evento: i nomi restano in memoria per qualche minuto
        summary = client.get("/api/summary").json()
        assert ha.reads == 1
        assert summary["faults"] == [] and summary["alarms"] == []
        # Foto non riuscita: un guasto nella barra in alto, con il nome della telecamera
        app.state.snapshot_errors["camera.x"] = {"ts": "2026-10-25T15:00:00Z", "error": "risposta 503"}
        [fault] = client.get("/api/summary").json()["faults"]
        assert fault["title"] == "Telecamera Non Disponibile" and fault["text"] == "Ingresso Teatro · risposta 503"
        # Resta un guasto anche dopo la presa visione, finché la telecamera non torna
        client.post("/api/notifications/seen", json={"ids": [fault["id"]]})
        assert len(client.get("/api/summary").json()["faults"]) == 1
        app.state.snapshot_errors.clear()
        assert client.get("/api/summary").json()["faults"] == []


def test_missing_cards_through_api(client, controller):
    client.post("/api/persons", json={"name": "Anna", "card": "04aa", "group": "profilo_sempre"})
    controller.credentials.clear()

    assert client.post("/api/sync").json()["missing"] == 1
    [card] = client.get("/api/missing-cards").json()
    assert card["person_name"] == "Anna"
    titles = [i["title"] for i in client.get("/api/notifications").json()["items"]]
    assert "Tessere Non Presenti Sul Controller" in titles

    assert client.post("/api/missing-cards/restore", json={}).json() == {"restored": 1, "failed": []}
    assert client.get("/api/missing-cards").json() == []


def test_changed_controller_through_api(client, controller):
    from app.main import _apply_pause

    state = client.app.state
    client.portal.call(state.health.check_all)
    controller.serial = "NUOVO"
    client.portal.call(state.health.check_all)
    _apply_pause(state)

    [ctrl] = client.get("/api/installation").json()["controllers"]
    assert "numero di serie X → NUOVO" in ctrl["health"]["identity_change"]
    response = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "group": "profilo_sempre"})
    assert response.status_code == 502 and "scritture sospese" in response.json()["detail"]
    assert "sincronizzazione sospesa" in client.post("/api/sync").json()["detail"]
    assert "Controller Cambiato" in [i["title"] for i in client.get("/api/summary").json()["faults"]]

    assert client.post(f"/api/controllers/{ctrl['id']}/identity/accept").json() == {"ok": True}
    response = client.post("/api/persons", json={"name": "Anna", "card": "04aa", "group": "profilo_sempre"})
    assert response.status_code == 200


def test_status_reports_database(client):
    data = client.get("/api/status").json()
    assert data["database"]["schema"] >= 2
    assert data["controllers"][0]["online"] is True
