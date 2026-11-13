"""Accesso diretto senza permessi tecnici (0.28.3): chi entra dall'accesso diretto senza i permessi Impianti o Backup
non legge mai Home Assistant, MQTT, broker, Mosquitto, add-on, Supervisor, Ingress né integrazione, nelle notifiche,
negli errori e nelle pagine dell'accesso diretto. Nel registro Home Assistant resta chi ha aperto la porta a distanza.
Con i permessi tecnici, e da Home Assistant, tutto come prima."""

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_direct import ADMIN, NEW_PASSWORD, SAFE, clients, enable, fast_hash, login, ready, staff  # noqa: F401
from test_page_time_zone import needs_node, run_page

from app import main
from app.backup_copy import STARTED_EMPTY
from app.config import MqttSettings
from app.direct import mark_direct
from app.notifications import EVENTS_PLAIN, recovery_hint, recovery_title
from app.plain import ASK_ADMIN, NEUTRAL_ERROR, once, plain_source, plain_text
from app.service import ServiceError
from app.vapix import VapixError

# Le parole vietate, scritte qui da capo: un errore in plain.FORBIDDEN non deve nascondere una parola sfuggita
WORDS = re.compile(r"home[ -]?assistant|mqtt|broker|mosquitto|add-?on|supervisor|\bingress\b|integrazion", re.I)
UPPER = re.compile(r"\bHA\b")
# Tutti i permessi che non sono tecnici: le pagine più ampie che si possono vedere senza Impianti e Backup
STAFF = ("stato", "persone", "persone_modifica", "registro", "regole", "regole_modifica", "import_export")
OPERATOR = {"X-Remote-User-Name": "operatore", "X-Remote-User-Id": "id-op", "X-Remote-User-Display-Name": "Operatore"}
HA_SOURCE = "Luca (Home Assistant)"
HA_FAILED = "apertura non eseguita: errore imprevisto, dettagli nel log dell'add-on"


def says(text) -> bool:
    return bool(WORDS.search(text or "") or UPPER.search(text or ""))


def leaks(listing: dict) -> list[str]:
    """Testi delle notifiche (e dei loro tipi) che nominano Home Assistant e simili."""
    found = []
    for item in listing["items"]:
        found += [item[k] for k in ("title", "text", "door", "snapshot_error") if says(item.get(k))]
    for kind in listing["types"]:
        found += [kind[k] for k in ("name", "description") if says(kind[k])]
    return found


@pytest.fixture
def plant(clients):  # noqa: F811
    """Accesso diretto acceso: personale senza permessi tecnici, amministratore dall'accesso diretto, operatore da Home
    Assistant con gli stessi permessi del personale."""
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site, "personale", STAFF)
    admin = TestClient(mark_direct(app), base_url="http://hotel.lan:8100")
    admin.portal = ingress.portal
    temporary = ingress.post("/api/users/admin/password", headers=ADMIN).json()["password"]
    assert login(admin, "admin", temporary).status_code == 200
    assert (
        admin.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE).status_code == 200
    )
    body = {"username": "operatore", "name": "Operatore", "perms": list(STAFF)}
    assert ingress.post("/api/users", json=body, headers=ADMIN).status_code == 200
    return ingress, site, admin, app


def faults(app, monkeypatch, *, intercom=True, clock="home_assistant"):
    """I guasti tipici: nessun broker, orologio, telecamera che non risponde, database ripartito vuoto, copia per il
    backup di Home Assistant non aggiornata, sincronizzazione con un errore imprevisto, apertura da Home Assistant non
    eseguita e foto non scattata."""
    state = app.state
    door = state.doors[0]
    if intercom:
        monkeypatch.setitem(door, "intercom_name", "Citofono Ingresso")
    monkeypatch.setitem(door, "snapshot_camera", "camera.ingresso")
    monkeypatch.setattr(state, "mqtt", None)
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    state.snapshot_errors["camera.ingresso"] = {"ts": now, "error": "Home Assistant non risponde (tempo scaduto)"}
    text = (
        f"database danneggiato spostato in prova.db e nessuna copia sana del backup da cui ripartire: {STARTED_EMPTY}"
    )
    state.db_recovery = {"at": now, "text": text, "title": recovery_title(text), "hint": recovery_hint(text)}
    monkeypatch.setattr(main, "ha_copy_error", lambda path: "copia non aggiornata (spazio su disco esaurito)")
    real = main._controllers_health

    def health(state):
        rows = real(state) or [{"id": (state.installation.primary or {}).get("id"), "name": "Quadro Prova"}]
        first = {**rows[0], "online": True, "unreachable": False, "checked_at": now}
        if clock:
            first.update(clock_fault=clock, clock_offset=-240.0 if clock == "controller" else 96.0)
        return [first, *rows[1:]]

    monkeypatch.setattr(main, "_controllers_health", health)
    state.db.add_event(ts=now, kind="negato", door=door["door_token"], source=HA_SOURCE, detail=HA_FAILED)
    alarm = state.db.add_event(ts=now, kind="allarme", door=door["door_token"], detail="porta forzata")
    state.db.set_event_snapshot(alarm, "", "telecamera non disponibile (risposta 503 di Home Assistant)")
    state.db.add_event(ts=now, kind="apertura_remota", door=door["door_token"], source=HA_SOURCE)


def by_title(listing: dict) -> dict[str, dict]:
    return {i["title"]: i for i in listing["items"]}


def test_notifications_without_home_assistant(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch)
    listing = site.get("/api/notifications").json()
    assert listing["items"] and leaks(listing) == []
    items = by_title(listing)
    # Il broker serve anche ai videocitofoni, che il personale usa: resta, senza MQTT né Mosquitto
    assert (
        items["Videocitofoni Senza Collegamento"]["text"] == f"Videocitofoni senza collegamento all'app: {ASK_ADMIN}."
    )
    clock = items["Orologio del Server"]["text"]
    assert clock.startswith("96 s indietro rispetto a ") and clock.endswith(f": {ASK_ADMIN}")
    assert items["Telecamera Non Disponibile"]["text"].endswith("telecamera non raggiungibile (tempo scaduto)")
    denied = [i for i in listing["items"] if i["type"] == "denied" and "apertura non eseguita" in i["text"]]
    assert [i["text"] for i in denied] == [
        f"Luca (apertura remota) · apertura non eseguita: errore imprevisto: {ASK_ADMIN}"
    ]
    alarm = next(i for i in listing["items"] if i["title"] == "Allarme Porta")
    assert alarm["snapshot_error"] == "telecamera non disponibile (risposta 503)"
    # Database e backup: solo a chi ha il permesso Backup, come prima
    assert {i["type"] for i in listing["items"]}.isdisjoint({"database", "backup"})
    # Sincronizzazione non riuscita per un errore imprevisto: «avvisa l'amministratore» invece del log dell'add-on
    app.state.last_sync = {"ok": False, "error": "errore imprevisto: dettagli nel log dell'add-on", "at": ""}
    monkeypatch.setattr(main, "_controllers_health", lambda state: [])
    listing = site.get("/api/notifications").json()
    assert by_title(listing)["Controller Non Raggiungibile"]["text"] == f"Errore imprevisto: {ASK_ADMIN}."
    assert leaks(listing) == []


def test_summary_counter_and_dot_without_home_assistant(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch, intercom=False)
    listing = site.get("/api/notifications").json()
    summary = site.get("/api/summary").json()
    # Nessun broker con il solo Home Assistant: per il personale non c'è, né nel conto né nella barra in alto
    assert all(i["type"] != "mqtt" for i in listing["items"])
    assert summary["notifications"] == listing["open"]
    assert [f["title"] for f in summary["faults"] + summary["alarms"] if says(f["title"] + f["text"])] == []
    full = admin.get("/api/notifications").json()
    assert "Nessun Broker MQTT" in by_title(full) and full["open"] > listing["open"]


def test_seen_from_staff_leaves_home_assistant_notifications_to_the_others(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch, intercom=False)
    shown = {i["id"] for i in site.get("/api/notifications").json()["items"]}
    # Segna Tutte come Viste: solo quelle che il personale vede
    assert site.post("/api/notifications/seen", json={}, headers=SAFE).json() == {"open": 0}
    full = admin.get("/api/notifications").json()["items"]
    hidden = [i for i in full if i["id"] not in shown]
    assert {i["title"] for i in hidden} >= {"Nessun Broker MQTT", "Database Ripartito Vuoto"}
    assert all(not i["seen"] for i in hidden)
    assert all(i["seen"] for i in full if i["id"] in shown)
    # Anche con l'id chiesto a mano: una notifica nascosta non si segna
    site.post("/api/notifications/seen", json={"ids": ["mqtt:none"]}, headers=SAFE)
    assert not by_title(admin.get("/api/notifications").json())["Nessun Broker MQTT"]["seen"]
    actions = [a["detail"] for a in app.state.db.list_audit() if a["action"].startswith("notific")]
    assert all("Broker" not in detail for detail in actions)


def test_seen_from_staff_saves_the_real_titles_in_the_activity(plant, monkeypatch):
    """La presa visione del personale finisce in Attività (database, letta solo dall'Amministratore) con titolo e testo
    veri della notifica, come quella di chiunque altro: non con «Orologio del Server» o «Videocitofoni Senza
    Collegamento», che per l'Amministratore non esistono."""
    ingress, site, admin, app = plant
    faults(app, monkeypatch)
    shown = {i["id"]: i for i in site.get("/api/notifications").json()["items"]}
    real = {i["id"]: i for i in ingress.get("/api/notifications", headers=ADMIN).json()["items"]}
    ids = [k for k, i in shown.items() if i["title"] in ("Orologio del Server", "Videocitofoni Senza Collegamento")]
    assert sorted(real[k]["title"] for k in ids) == ["Nessun Broker MQTT", "Orologio di Home Assistant"]
    assert site.post("/api/notifications/seen", json={"ids": ids}, headers=SAFE).json()["open"] >= 0
    [row] = [
        a for a in ingress.get("/api/audit", headers=ADMIN).json() if a["action"] == "notifiche segnate come viste"
    ]
    assert row["detail"].startswith("2 notifiche: ")
    for k in ids:
        assert f"{real[k]['title']} ({real[k]['text']})" in row["detail"]
    assert "Orologio del Server" not in row["detail"] and "Videocitofoni Senza Collegamento" not in row["detail"]
    # Per il personale e per l'Amministratore le stesse notifiche risultano viste
    assert all(i["seen"] for i in site.get("/api/notifications").json()["items"] if i["id"] in ids)
    assert all(i["seen"] for i in ingress.get("/api/notifications", headers=ADMIN).json()["items"] if i["id"] in ids)


def test_mqtt_lost_and_events_not_arriving(plant, monkeypatch):
    ingress, site, admin, app = plant
    state = app.state
    monkeypatch.setattr(state, "mqtt", MqttSettings(host="core-mosquitto"))
    monkeypatch.setattr(state.monitor, "connected", False)
    monkeypatch.setattr(state.monitor, "disconnected_since", "2026-11-13T08:00:00+00:00")
    # Collegamento MQTT perso con il solo Home Assistant: per il personale non c'è
    assert all(i["type"] != "mqtt" for i in site.get("/api/notifications").json()["items"])
    assert "Collegamento MQTT Perso" in by_title(admin.get("/api/notifications").json())
    monkeypatch.setitem(state.doors[0], "intercom_name", "Citofono Ingresso")
    listing = site.get("/api/notifications").json()
    assert "Videocitofoni Senza Collegamento" in by_title(listing) and leaks(listing) == []
    # Controller che risponde ma non manda gli eventi
    monkeypatch.setattr(state.monitor, "connected", True)
    monkeypatch.setattr(state.monitor, "controller_online", False)
    monkeypatch.setattr(state.monitor, "controller_offline_since", "2026-11-13T08:00:00+00:00")
    monkeypatch.setattr(state, "live_events", None)
    row = {"id": state.installation.primary["id"], "name": "Quadro", "online": True, "unreachable": False}
    monkeypatch.setattr(main, "_controllers_health", lambda state: [row])
    listing = site.get("/api/notifications").json()
    assert by_title(listing)["Eventi del Controller Non Arrivano"]["text"] == EVENTS_PLAIN
    assert leaks(listing) == []
    original = by_title(admin.get("/api/notifications").json())["Eventi del Controller Non Arrivano"]["text"]
    assert original == "MQTT: controller non collegato al broker"


def test_controller_clock_without_home_assistant(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch, clock="controller")
    clock = next(i for i in site.get("/api/notifications").json()["items"] if i["type"] == "clock")
    assert clock["title"] == "Orologio del Controller"
    assert clock["text"].endswith(f"4 min indietro rispetto al server: {ASK_ADMIN}")
    original = next(i for i in admin.get("/api/notifications").json()["items"] if i["type"] == "clock")
    assert original["text"].endswith("4 min indietro rispetto a Home Assistant, NTP non sincronizzato")


def test_technical_permissions_and_home_assistant_see_what_they_saw(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch)
    from_ingress = ingress.get("/api/notifications", headers=ADMIN).json()
    from_direct = admin.get("/api/notifications").json()
    # L'amministratore dall'accesso diretto: le stesse notifiche, con gli stessi testi, che da Home Assistant
    assert from_direct == from_ingress
    items = by_title(from_ingress)
    assert (
        items["Nessun Broker MQTT"]["text"]
        == "Home Assistant e videocitofoni senza collegamento all'app. Installa Mosquitto."
    )
    assert {"Orologio di Home Assistant", "Database Ripartito Vuoto", "Copia per il Backup Non Aggiornata"} <= set(
        items
    )
    assert any(t["name"] == "Eventi in Diretta e MQTT" for t in from_ingress["types"])
    # Da Home Assistant, con gli stessi permessi del personale: i testi di sempre
    operator = ingress.get("/api/notifications", headers=OPERATOR).json()
    assert "Nessun Broker MQTT" in by_title(operator) and "Orologio di Home Assistant" in by_title(operator)
    assert any(HA_SOURCE in i["text"] for i in operator["items"])
    # Chi ha un permesso tecnico, anche solo Impianti, dall'accesso diretto legge i testi tecnici
    tech = TestClient(mark_direct(app), base_url="http://hotel.lan:8100")
    tech.portal = ingress.portal
    ready(ingress, tech, "tecnico", ("stato", "impianti"))
    assert "Nessun Broker MQTT" in by_title(tech.get("/api/notifications").json())
    assert tech.get("/api/me").json()["hide_home_assistant"] is False
    assert site.get("/api/me").json()["hide_home_assistant"] is True
    assert admin.get("/api/me").json()["hide_home_assistant"] is False
    assert ingress.get("/api/me", headers=OPERATOR).json()["hide_home_assistant"] is False


def test_log_keeps_home_assistant_as_who_opened(plant, monkeypatch):
    ingress, site, admin, app = plant
    faults(app, monkeypatch)
    events = site.get("/api/events").json()
    # Chi ha aperto o provato ad aprire resta com'è: è il registro
    assert [e["source"] for e in events if e["kind"] == "apertura_remota"] == [HA_SOURCE]
    failed = next(e for e in events if e["kind"] == "negato" and e["source"] == HA_SOURCE)
    assert failed["detail"] == f"apertura non eseguita: errore imprevisto: {ASK_ADMIN}"
    alarm = next(e for e in events if e["kind"] == "allarme")
    assert alarm["snapshot_error"] == "telecamera non disponibile (risposta 503)"
    assert site.get(f"/api/events/{failed['id']}").json()["detail"] == failed["detail"]
    recent = site.get("/api/dashboard").json()["recent"]
    assert [e["detail"] for e in recent if e["id"] == failed["id"]] == [failed["detail"]]
    door = site.get(f"/api/door-detail/{app.state.doors[0]['id']}").json()
    assert [e["detail"] for e in door["events"] if e["id"] == failed["id"]] == [failed["detail"]]
    for event in events:
        assert not says(event["detail"]) and not says(event["snapshot_error"])
    # Anche gli eventi nella scheda della persona
    person = ingress.post("/api/persons", json={"first_name": "Anna", "last_name": "Verdi"}, headers=ADMIN).json()
    app.state.db.add_event(
        ts=failed["ts"], kind="negato", door=failed["door"], person_id=person["id"], person_name="Anna Verdi",
        detail=HA_FAILED,
    )  # fmt: skip
    sheet = site.get(f"/api/persons/{person['id']}").json()["events"]
    assert [e["detail"] for e in sheet] == [failed["detail"]]
    assert [e["detail"] for e in ingress.get(f"/api/persons/{person['id']}", headers=OPERATOR).json()["events"]] == [
        HA_FAILED
    ]
    # Con i permessi tecnici e da Home Assistant, come è salvato
    for client, headers in ((admin, {}), (ingress, OPERATOR)):
        original = client.get(f"/api/events/{failed['id']}", headers=headers).json()
        assert original["detail"] == HA_FAILED


def test_log_csv_without_home_assistant(plant, monkeypatch):
    """Il Registro CSV scaricato dal personale (Registro e Importa ed Esporta) ha il dettaglio come nella pagina: senza
    il log dell'add-on. Home Assistant resta solo nella colonna Origine, chi ha aperto o provato ad aprire."""
    ingress, site, admin, app = plant
    faults(app, monkeypatch)
    rows = [line for line in site.get("/api/export/events.csv").text.splitlines() if "apertura non eseguita" in line]
    assert len(rows) == 1
    assert f"apertura non eseguita: errore imprevisto: {ASK_ADMIN}" in rows[0] and HA_SOURCE in rows[0]
    assert not says(rows[0].replace(HA_SOURCE, ""))
    for line in site.get("/api/export/events.csv").text.splitlines():
        assert not says(line.replace(HA_SOURCE, "")), line
    # Con i permessi tecnici e da Home Assistant, come è salvato
    for client, headers in ((admin, {}), (ingress, OPERATOR)):
        text = client.get("/api/export/events.csv", headers=headers).text
        assert HA_FAILED in text and HA_SOURCE in text


def test_error_messages_without_home_assistant(plant, monkeypatch):
    ingress, site, admin, app = plant
    # Gestione degli utenti: per il personale il messaggio di sempre sul permesso che manca
    detail = site.get("/api/users").json()["detail"]
    assert detail.startswith("il tuo utente non ha il permesso") and not says(detail)
    assert admin.get("/api/users").json()["detail"] == "si fa solo dall'app dentro Home Assistant"
    body = {"first_name": "Anna", "last_name": "Verdi"}
    for error in (
        ServiceError("tessera non scritta: broker MQTT di Home Assistant non collegato"),
        VapixError("il controller ha rifiutato l'integrazione con il broker"),
        HTTPException(409, "riprova dopo il riavvio dell'add-on"),
    ):

        async def fail(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(app.state.service, "create_person", fail)
        assert site.post("/api/persons", json=body, headers=SAFE).json()["detail"] == NEUTRAL_ERROR
        for client, headers in ((admin, SAFE), (ingress, {**OPERATOR, **SAFE})):
            assert says(client.post("/api/persons", json=body, headers=headers).json()["detail"])

    # Disco pieno: liberare spazio è un lavoro tecnico
    def full(*args, **kwargs):
        err = sqlite3.OperationalError("database or disk is full")
        err.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise err

    monkeypatch.setattr(app.state.db, "list_persons", full)
    assert site.get("/api/persons").json()["detail"] == f"spazio su disco esaurito: {ASK_ADMIN}"
    assert admin.get("/api/persons").json()["detail"] == "spazio su disco esaurito: libera spazio"
    # Dati rifiutati: nessun nome di campo tecnico
    detail = site.post("/api/persons", json={"first_name": ["x"]}, headers=SAFE).json()["detail"]
    assert not says(detail)


def test_names_written_by_users_are_not_home_assistant(plant, monkeypatch):
    """«Supervisore», «VAN NAM HA», «Broker Assicurazioni», «Corso di Integrazione»: nomi scritti dagli utenti, non Home
    Assistant. Per il personale dall'accesso diretto messaggi d'errore, notifiche e registro restano quelli veri, come
    da Home Assistant. Prima il filtro trovava «supervisor» dentro «Supervisore» e «HA» dentro il cognome: l'errore
    diventava «operazione non riuscita: avvisa l'amministratore dell'impianto» e la Tessera in Scadenza restava senza
    testo."""
    ingress, site, admin, app = plant
    soon = (datetime.now(tz=UTC) + timedelta(days=3)).isoformat(timespec="seconds")
    people = (
        {"first_name": "Supervisore", "last_name": "Notte", "card": "04aa11bb", "valid_to": soon},
        {"first_name": "VAN NAM", "last_name": "HA", "card": "04cc22dd", "valid_to": soon, "company": "Broker Srl"},
    )
    for body in people:
        assert ingress.post("/api/persons", json=body, headers=ADMIN).status_code == 200

    def both(method: str, url: str, body: dict) -> tuple[str, str]:
        staff = site.request(method, url, json=body, headers=SAFE).json()["detail"]
        home = ingress.request(method, url, json=body, headers={**OPERATOR, **SAFE}).json()["detail"]
        return staff, home

    # Tessera già registrata: il motivo vero, con il nome
    staff, home = both("POST", "/api/persons", {"first_name": "Mario", "card": "04aa11bb"})
    assert staff == home == "la tessera 04aa11bb è già registrata a nome Supervisore Notte"
    staff, home = both("POST", "/api/persons", {"first_name": "Mario", "card": "04cc22dd"})
    assert staff == home == "la tessera 04cc22dd è già registrata a nome VAN NAM HA"
    # Tipologia che esiste già
    door = app.state.doors[0]["id"]
    rule = {"target": "door", "target_id": door, "schedule": "standard_always"}
    for name in ("Supervisori", "Corso di Integrazione", "Corso HA Base"):
        body = {"name": name, "color": "blue", "valid_days": 0, "description": "", "rules": [rule]}
        assert ingress.post("/api/categories", json=body, headers=ADMIN).status_code == 200
        staff, home = both("POST", "/api/categories", body)
        assert staff == home == f"esiste già la tipologia {name}"
    # Notifiche: Tessera in Scadenza con il nome della persona
    expiring = sorted(i["text"] for i in site.get("/api/notifications").json()["items"] if i["type"] == "expiring")
    assert [t.split(" · ")[0] for t in expiring] == ["Supervisore Notte", "VAN NAM HA"]
    full = ingress.get("/api/notifications", headers=OPERATOR).json()["items"]
    assert expiring == sorted(i["text"] for i in full if i["type"] == "expiring")
    # Del testo di una notifica si toglie solo la parte che nomina Home Assistant, non il nome del controller
    state = app.state
    state.db._execute("UPDATE controllers SET name = 'Quadro HA Nord'")
    row = {"id": 1, "name": "Quadro HA Nord", "unreachable": True, "error": "errore di Home Assistant"}
    monkeypatch.setattr(main, "_controllers_health", lambda state: [row])
    [down] = [i for i in site.get("/api/notifications").json()["items"] if i["type"] == "controller"]
    assert down["text"] == "Quadro HA Nord"
    [home] = [
        i for i in ingress.get("/api/notifications", headers=OPERATOR).json()["items"] if i["type"] == "controller"
    ]
    assert home["text"] == "Quadro HA Nord · errore di Home Assistant"
    # Registro: un dettaglio con il nome di una porta resta com'è
    state.db._execute("UPDATE doors SET name = 'Ingresso HA Ovest' WHERE id = ?", (door,))
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    token = state.doors[0]["door_token"]
    state.db.add_event(ts=now, kind="allarme", door=token, detail="porta Ingresso HA Ovest aperta troppo a lungo")
    state.db.add_event(ts=now, kind="allarme", door=token, detail="pulsante Supervisore premuto")
    details = [e["detail"] for e in site.get("/api/events").json() if e["kind"] == "allarme"]
    assert details == ["pulsante Supervisore premuto", "porta Ingresso HA Ovest aperta troppo a lungo"]
    # Le parole di Home Assistant scritte dall'app restano coperte anche accanto a un nome
    for error in (
        ServiceError("tessera di VAN NAM HA non scritta: broker non collegato"),
        ServiceError("tessera non scritta: broker MQTT di Home Assistant non collegato"),
    ):

        async def fail(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(state.service, "create_person", fail)
        assert site.post("/api/persons", json={"first_name": "X"}, headers=SAFE).json()["detail"] == NEUTRAL_ERROR


def test_direct_access_pages_without_home_assistant(plant, monkeypatch):
    ingress, site, admin, app = plant
    stranger = TestClient(mark_direct(app), base_url="http://hotel.lan:8100")
    stranger.portal = ingress.portal
    answers = []
    # Indirizzo con un nome pubblico, prima del login
    page = stranger.get("/", headers={"host": "evil.example:8100"})
    assert page.status_code == 421
    answers.append(page.text)
    answers.append(stranger.get("/api/me").json()["detail"])
    answers += [login(stranger, "personale", "sbagliata-123").json()["detail"] for _ in range(6)]
    assert answers[-1].startswith("troppi tentativi sbagliati")
    # Password provvisoria scaduta e da cambiare
    temporary = staff(ingress, "nuovo", ("stato",))
    fresh = TestClient(mark_direct(app), base_url="http://hotel.lan:8100")
    fresh.portal = ingress.portal
    login(fresh, "nuovo", temporary)
    answers.append(fresh.get("/api/persons").json()["detail"])
    answers.append(fresh.post("/api/password", json={"current": "x", "new": "y"}, headers=SAFE).json()["detail"])
    answers.append(
        fresh.post("/api/password", json={"current": temporary, "new": "corta"}, headers=SAFE).json()["detail"]
    )
    old = (datetime.now(tz=UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    app.state.db._execute("UPDATE app_user_passwords SET set_at = ? WHERE username = 'nuovo'", (old,))
    answers.append(login(stranger, "nuovo", temporary).json()["detail"])
    # Modifica senza l'intestazione della pagina, accesso diretto spento
    answers.append(site.post("/api/persons", json={"first_name": "X"}).json()["detail"])
    ingress.put("/api/direct-access", json={"enabled": False}, headers=ADMIN)
    answers.append(site.get("/api/me").json()["detail"])
    assert answers[0] == "Indirizzo non accettato: apri l'app con l'indirizzo IP del server"
    assert [a for a in answers if says(a)] == []


def test_settings_status_for_staff(plant):
    ingress, site, admin, app = plant
    status = site.get("/api/status?controller=false")
    assert status.status_code == 200
    # Solo le opzioni che la pagina mostra: niente Home Assistant, broker e database dell'add-on
    assert set(status.json()) == {"options", "now"}
    assert set(status.json()["options"]) == {"default_group", "event_retention_days"}
    full = ingress.get("/api/status?controller=false", headers=OPERATOR).json()
    assert {"home_assistant", "mqtt", "database"} <= set(full)
    assert {"home_assistant", "mqtt", "database"} <= set(admin.get("/api/status?controller=false").json())


def test_plain_text_and_source():
    assert plain_text("Ingresso · Home Assistant non risponde") == "Ingresso · telecamera non raggiungibile"
    assert plain_text("serve il broker", "") == ""
    assert plain_text("ha suonato") == "ha suonato"
    assert plain_text("HA spento") == NEUTRAL_ERROR
    # Parole intere: «Supervisore», «ingresso», «brokeraggio» non sono Home Assistant
    assert (
        plain_text("pulsante Supervisore, ingresso del brokeraggio") == "pulsante Supervisore, ingresso del brokeraggio"
    )
    assert plain_text("Supervisor di Home Assistant") == NEUTRAL_ERROR
    assert plain_text("core-mosquitto spento") == NEUTRAL_ERROR
    # Un nome scritto dagli utenti non conta, le parole dell'app fuori dal nome sì
    names = ["VAN NAM HA", "Broker Srl", "HA", "on", "MQTT"]
    assert (
        plain_text("registrata a nome VAN NAM HA (Broker Srl)", names=names)
        == "registrata a nome VAN NAM HA (Broker Srl)"
    )
    assert plain_text("VAN NAM HA: broker spento", names=names) == NEUTRAL_ERROR
    # Un nome fatto solo di una parola vietata non si distingue dalla parola tecnica; un nome corto non spezza add-on
    assert plain_text("Tipologia HA", names=names) == NEUTRAL_ERROR
    assert plain_text("MQTT spento", names=names) == NEUTRAL_ERROR
    assert plain_text("riavvia l'add-on", names=names) == NEUTRAL_ERROR
    # I nomi si leggono solo se c'è una parola vietata, una volta sola
    reads = []
    lazy = once(lambda: reads.append(1) or ["VAN NAM HA"])
    assert plain_text("tutto bene", names=lazy) == "tutto bene" and reads == []
    assert plain_text("VAN NAM HA", names=lazy) == "VAN NAM HA"
    assert plain_text("Ciao VAN NAM HA", names=lazy) == "Ciao VAN NAM HA"
    assert reads == [1]
    assert plain_source("Home Assistant") == "Apertura Remota"
    assert plain_source("Automazione (Home Assistant)") == "Automazione (apertura remota)"
    assert plain_source("Pulsante Portineria") == "Pulsante Portineria"


@needs_node
def test_page_helper_follows_the_server():
    """hideHA() nella pagina: decide il server (api/me); finché non lo si sa, il login (c'è solo nell'accesso diretto) o
    l'indirizzo (da Home Assistant la pagina sta sotto /api/hassio_ingress/)."""
    prelude = 'globalThis.document = {body: {dataset: {}}}; globalThis.location = {pathname: "/"};'

    def check(me: str, auth: str = "", path: str = "/") -> str:
        setup = f"document.body.dataset.auth = {auth!r}, location.pathname = {path!r}, store.me = {me}"
        return f"({setup}, [directPage(), hideHA(), appName()])"

    out = run_page(
        "Europe/Rome",
        {
            "staff": check("{direct: true, hide_home_assistant: true}"),
            "admin": check("{direct: true, hide_home_assistant: false}"),
            "ingress": check("{direct: false, hide_home_assistant: false}", path="/api/hassio_ingress/abc/"),
            "login": check("{perms: []}", auth="login"),
            "unknown_ingress": check("{perms: []}", path="/api/hassio_ingress/abc/"),
            "unknown_direct": check("{perms: []}"),
        },
        names=("directPage", "hideHA", "appName"),
        prelude=prelude,
    )
    assert out == {
        "staff": [True, True, "l'app"],
        "admin": [True, False, "l'add-on"],
        "ingress": [False, False, "l'add-on"],
        "login": [True, True, "l'app"],
        "unknown_ingress": [False, False, "l'add-on"],
        "unknown_direct": [True, True, "l'app"],
    }
