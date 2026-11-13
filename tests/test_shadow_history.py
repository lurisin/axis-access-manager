"""Confronto in ombra, collaudo 0.28.1: una riga INFO per ogni giro e lo storico dei giri nel database, per giorno
dell'impianto, che resta dopo un riavvio e un ripristino. Mai una scrittura sul controller."""

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from test_comparison import ADMIN, client, controller_id, page_html, writes  # noqa: F401
from test_database import DB_NAME, ha_backup, ha_restore, start
from test_direct import ADMIN as DIRECT_ADMIN
from test_direct import clients, enable, fast_hash, ready  # noqa: F401
from test_full_restore import bench_like
from test_page_time_zone import needs_node

from app import main
from app.compare import KIND_TITLES
from app.db import SCHEMA_VERSION, Database
from app.desired import PROGRAMMING
from app.main import create_app
from app.service import ServiceError
from app.shadow import CARDS_ROUND, HISTORY_DAYS, MANUAL_ROUND, PROGRAMMING_ROUND, Shadow, plant_day

CARDS = ("utente", "tessera")
# Un giro di utenti e tessere uguali, come lo dà compare.compare
CLEAN = {
    "rows": [
        {"kind": "utente", "key": "utente:u1", "action": "uguale", "surprises": [], "changes": []},
        {"kind": "tessera", "key": "tessera:c1", "action": "uguale", "surprises": [], "changes": []},
    ],
    "unread": [],
    "pending": [],
}
HIDDEN = "serve il permesso «Vedere l'anagrafica: persone, tessere e tipologie»"
H24_DIFFERENCE = "Profilo di Accesso H24: Riporterebbe com'è nell'app · Valida fino al 2040-12-31T00:00:00Z → —"


def rome(at: str) -> str:
    """Ora e minuti di at a Roma, il fuso dell'impianto nella pagina (vedi page_html)."""
    return datetime.fromisoformat(at).astimezone(ZoneInfo("Europe/Rome")).strftime("%H:%M")


def round_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.shadow" and r.levelno == logging.INFO]


def history(client, **params) -> dict:  # noqa: F811
    response = client.get("/api/shadow", params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def today_of(data: dict) -> dict:
    assert data["days"][0]["day"] == data["today"]
    return data["days"][0]["giri"]


def hourly(client):  # noqa: F811
    state = client.app.state
    state.shadow.next_full = 0
    assert client.portal.call(main._shadow_programming, state) is True


def difference(**fields) -> dict:
    """Giro di utenti e tessere con la tessera di Anna da riportare."""
    row = {
        "kind": "tessera",
        "key": "tessera:c1",
        "label": "Tessera 04aa (Anna)",
        "action": "riporta",
        "text": "Riporta com'è nell'app",
        "surprises": [],
        "changes": [{"label": "Descrizione", "old": "Anna (a mano)", "new": "Anna"}],
        **fields,
    }
    return {"rows": [CLEAN["rows"][0], row], "unread": [], "pending": []}


def test_every_round_writes_one_info_line(client, controller, caplog):  # noqa: F811
    """Collaudo 0.28.1, punto 2: il giro orario scriveva nel log solo le differenze nuove, e alla fine della settimana
    non restava prova dei giri fatti. Ora ogni giro (sincronizzazione, ogni ora, Confronta Ora) ha la sua riga INFO,
    una sola, con l'esito e i conteggi di ogni tipo."""
    state = client.app.state
    bench_like(controller)
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    controller.attempts.clear()

    with caplog.at_level(logging.INFO, logger="app.shadow"):
        caplog.clear()
        assert client.post("/api/sync", headers=ADMIN).status_code == 200
        assert round_lines(caplog) == [
            "Confronto in ombra, giro di utenti e tessere dopo la sincronizzazione: senza differenze. "
            "Utenti: 1 uguale · Tessere: 1 uguale"
        ]

        caplog.clear()
        hourly(client)
        [line] = round_lines(caplog)
        head, kinds = line.split(". ", 1)
        # Il banco ha i pulsanti fatti a mano e il profilo Sempre senza descrizione: l'esito somma i tipi
        assert head == "Confronto in ombra, giro della programmazione di ogni ora: 5 differenze, 4 sorprese"
        counts = {k["kind"]: k for k in state.shadow.as_dict()["kinds"] if k["kind"] in PROGRAMMING}
        assert sum(k["differenze"] for k in counts.values()) == 5 and sum(k["sorprese"] for k in counts.values()) == 4
        # Ogni tipo della programmazione una volta, con i suoi conteggi in breve
        parts = dict(part.split(": ") for part in kinds.split(" · "))
        assert list(parts) == [KIND_TITLES[k] for k in KIND_TITLES if k in PROGRAMMING]
        assert parts["Fasce Orarie"] == "nessun oggetto" and parts["Punti di Accesso Gestiti"] == "1 uguale"
        assert parts["Lettori"] == "2 da prendere in carico, 1 differenza, 1 sorpresa"
        assert parts["Profili di Accesso"] == "1 differenza"

        caplog.clear()
        assert client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).status_code == 200
        [line] = round_lines(caplog)
        assert line.startswith("Confronto in ombra, giro della programmazione con Confronta Ora: ")

        # Una differenza: il WARNING dell'oggetto (una volta sola, come prima) e la riga del giro, a ogni giro
        controller.credentials[anna["cards"][0]["axis_credential_token"]]["Description"] = "Anna (a mano)"
        caplog.clear()
        client.post("/api/sync", headers=ADMIN)
        client.post("/api/sync", headers=ADMIN)
        assert (
            round_lines(caplog)
            == [
                "Confronto in ombra, giro di utenti e tessere dopo la sincronizzazione: 1 differenza. "
                "Utenti: 1 uguale · Tessere: 1 differenza"
            ]
            * 2
        )
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

        # Controller spento: il giro di Confronta Ora non riuscito, con il motivo
        controller.down = True
        caplog.clear()
        client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)
        assert round_lines(caplog) == [
            "Confronto in ombra, giro della programmazione con Confronta Ora: non riuscito, "
            "il controller non risponde (tempo scaduto)"
        ]
    assert writes(controller) == []


def test_round_line_with_lists_not_read():
    """Un elenco non letto: l'esito lo dice, con il motivo una volta sola, e il tipo è «non letto»."""
    shadow = Shadow()
    result = {**CLEAN, "unread": [{"kind": "utente", "blocked": "utenti: elenco vuoto"}]}
    with _capture() as records:
        shadow.record(1, CARDS, result, "2026-11-12T18:43:31+01:00", CARDS_ROUND)
    assert records == [
        "Confronto in ombra, giro di utenti e tessere dopo la sincronizzazione: 1 tipo non letto "
        "(utenti: elenco vuoto). Utenti: non letto · Tessere: 1 uguale"
    ]


class _capture:
    """Righe INFO del confronto in ombra, senza il fixture caplog."""

    def __enter__(self):
        self.records: list[str] = []
        self.handler = logging.Handler()
        self.handler.emit = lambda r: self.records.append(r.getMessage()) if r.levelno == logging.INFO else None
        logger = logging.getLogger("app.shadow")
        self.level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(self.handler)
        return self.records

    def __exit__(self, *exc):
        logger = logging.getLogger("app.shadow")
        logger.removeHandler(self.handler)
        logger.setLevel(self.level)


def test_history_by_day_and_round_survives_a_restart(settings, controller, tmp_path, monkeypatch):
    """Lo storico sta nel database: dopo un riavvio i giri di oggi ci sono ancora (prima il riavvio azzerava tutto)."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    path = tmp_path / "storico.db"

    def app():
        return TestClient(create_app(settings, allow_any_client=True, controller=controller, db_path=path))

    with app() as client:  # noqa: F811
        client.get("/api/me", headers=ADMIN)
        bench_like(controller)
        anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
        controller.attempts.clear()
        before = today_of(history(client))["utenti_tessere"] or {"giri": 0, "senza_differenze": 0}
        client.post("/api/sync", headers=ADMIN)
        client.post("/api/sync", headers=ADMIN)
        controller.credentials[anna["cards"][0]["axis_credential_token"]]["Description"] = "Anna (a mano)"
        client.post("/api/sync", headers=ADMIN)
        hourly(client)
        hourly(client)
        client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)
        data = history(client)
        cards = today_of(data)["utenti_tessere"]
        assert cards["giri"] - before["giri"] == 3
        assert cards["senza_differenze"] - before["senza_differenze"] == 2
        assert cards["con_differenze"] == 1 and cards["non_riusciti"] == 0
        assert cards["ultima_differenza"] == (
            "Tessera 04aa (Anna): Riporterebbe com'è nell'app · Descrizione Anna (a mano) → Anna"
        )
        assert cards["ultima_differenza_nascosta"] == ""
        assert today_of(data)["programmazione"]["giri"] == 2
        assert today_of(data)["confronta_ora"]["giri"] == 1
        assert len(data["days"]) == HISTORY_DAYS and data["unsaved"] == 0
        # Gli altri giorni senza giri
        assert all(day["giri"] == dict.fromkeys(day["giri"]) for day in data["days"][1:])
        # I conteggi dell'ultimo giro, come nello stato del controller
        assert data["current"]["rounds"]["confronta_ora"]["at"]
        saved = today_of(data)

    with app() as client:  # noqa: F811
        client.get("/api/me", headers=ADMIN)
        after = today_of(history(client))
        # Dopo il riavvio: lo stesso storico (la sincronizzazione all'avvio può aggiungere un giro di tessere)
        assert after["programmazione"] == saved["programmazione"]
        assert after["confronta_ora"] == saved["confronta_ora"]
        assert after["utenti_tessere"]["giri"] >= saved["utenti_tessere"]["giri"]
        assert after["utenti_tessere"]["con_differenze"] >= 1
        # Il giro di oggi in memoria si è perso: lo storico no
        assert "programmazione" not in client.app.state.shadow.rounds
    assert writes(controller) == []


def test_history_survives_a_home_assistant_backup_and_restore(settings, controller, tmp_path):
    """Ripristino di Home Assistant: lo storico torna con il database."""
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:  # noqa: F811
        client.get("/api/me", headers=ADMIN)
        hourly(client)
        saved = today_of(history(client))["programmazione"]
        assert saved["giri"] == 1
        ha_backup(data, tmp_path / "backup.tar")
    restored = ha_restore(tmp_path / "backup.tar", tmp_path / "ripristino")
    with start(settings, controller, restored) as client:  # noqa: F811
        client.get("/api/me", headers=ADMIN)
        assert today_of(history(client))["programmazione"] == saved


def test_history_survives_a_restore_from_a_backup_of_the_app(client, controller):  # noqa: F811
    """Ripristino dai backup dell'app: lo storico non fa parte dei backup e il ripristino non lo tocca."""
    person = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    created = client.post("/api/backups", json={"name": "Prima dei lavori"}, headers=ADMIN).json()
    hourly(client)
    saved = history(client)["days"]
    client.delete(f"/api/persons/{person['id']}", headers=ADMIN)
    path = f"/api/backups/{created['id']}"
    preview = client.post(f"{path}/preview", json={"sections": ["persone"]}, headers=ADMIN).json()
    keys = [i["key"] for i in preview["items"] if i["default"]]
    result = client.post(f"{path}/restore", json={"sections": ["persone"], "keys": keys}, headers=ADMIN).json()
    assert result["applied"][0]["label"] == "Anna"
    assert history(client)["days"] == saved
    assert saved[0]["giri"]["programmazione"]["giri"] == 1


def test_history_days_are_the_days_of_the_plant(tmp_path, monkeypatch):
    """Il giorno è quello dell'impianto (il fuso di Home Assistant), non quello di UTC né del sistema."""
    db = Database(tmp_path / DB_NAME)
    shadow = Shadow(db)
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    # 23:30 UTC del 12 sono le 08:30 del 13 a Tokyo; le 14:30 UTC sono le 23:30 del 12
    shadow.record(1, CARDS, CLEAN, "2026-11-12T23:30:00+00:00", CARDS_ROUND)
    shadow.record(1, CARDS, CLEAN, "2026-11-12T14:30:00+00:00", CARDS_ROUND)
    shadow.record(1, CARDS, CLEAN, "2026-11-13T08:00:00+09:00", CARDS_ROUND)
    assert {(r["day"], r["rounds"]) for r in db.shadow_days("2000-01-01")} == {("2026-11-13", 2), ("2026-11-12", 1)}
    monkeypatch.setenv("TZ", "Europe/Rome")
    # Cambio dell'ora: la mezzanotte di Roma del 25 ottobre è alle 22:00 UTC del 24, quella del 26 alle 23:00 del 25
    assert plant_day("2026-10-24T21:59:59+00:00") == "2026-10-24"
    assert plant_day("2026-10-24T22:00:00+00:00") == "2026-10-25"
    assert plant_day("2026-10-25T22:59:59+00:00") == "2026-10-25"
    assert plant_day("2026-10-25T23:00:00Z") == "2026-10-26"
    db.close()


def test_history_keeps_30_days_with_one_write_per_round(tmp_path, monkeypatch):
    """Gli ultimi 30 giorni, e per ogni giro una scrittura sola: un commit, anche con la pulizia dei giorni vecchi."""
    monkeypatch.setenv("TZ", "Europe/Rome")
    db = Database(tmp_path / DB_NAME)
    shadow = Shadow(db)
    statements: list[str] = []
    db._conn.set_trace_callback(statements.append)
    first = datetime(2026, 9, 1, 10, tzinfo=UTC)
    for n in range(40):
        for hour in (0, 1):
            statements.clear()
            at = (first + timedelta(days=n, hours=hour)).isoformat()
            shadow.record(1, CARDS, CLEAN, at, CARDS_ROUND)
            assert [s for s in statements if s == "COMMIT"] == ["COMMIT"]
            assert all("shadow_days" in s for s in statements if s not in ("BEGIN ", "COMMIT"))
    db._conn.set_trace_callback(None)
    days = [r["day"] for r in db.shadow_days("2000-01-01")]
    assert len(days) == HISTORY_DAYS == 30
    assert (days[0], days[-1]) == ("2026-10-10", "2026-09-11")
    assert {r["rounds"] for r in db.shadow_days("2000-01-01")} == {2}
    db.close()


class FullOn:
    """Connessione che risponde «disco pieno» alle istruzioni che cominciano con prefix."""

    def __init__(self, conn, prefix: str) -> None:
        self._real, self._prefix = conn, prefix

    def execute(self, sql, *args):
        if sql.startswith(self._prefix):
            err = sqlite3.OperationalError("database or disk is full")
            err.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise err
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_history_with_the_disk_full(tmp_path, monkeypatch):
    """Disco pieno: il confronto va avanti, nel log un avviso solo, niente resta a metà nel database e i giri restano
    in memoria finché una scrittura non riesce. Poi si salvano tutti, con un commit."""
    monkeypatch.setenv("TZ", "Europe/Rome")
    db = Database(tmp_path / DB_NAME)
    shadow = Shadow(db)
    real = db._conn
    # La riga del giro si scrive, la pulizia no: tutta la scrittura torna indietro
    monkeypatch.setattr(db, "_conn", FullOn(real, "DELETE FROM shadow_days"))
    now = datetime.now(tz=UTC).astimezone().isoformat(timespec="seconds")
    with _capture() as lines, _capture_warnings() as warnings:
        shadow.record(1, CARDS, CLEAN, now, CARDS_ROUND)
        assert not real.in_transaction
        monkeypatch.setattr(db, "_conn", FullOn(real, "INSERT INTO shadow_days"))
        shadow.record(1, CARDS, difference(), now, CARDS_ROUND)
        shadow.fail(1, PROGRAMMING_ROUND, now, sqlite3.OperationalError("disk I/O error"))
        assert len(lines) == 3
        assert [w for w in warnings if w.startswith("Storico")] == [
            "Storico del confronto in ombra non salvato, si riprova al giro dopo: database or disk is full"
        ]
        assert db.shadow_days("2000-01-01") == [] and not real.in_transaction
        # I giri non salvati si vedono lo stesso, e si sa che non sono salvati
        data = shadow.history(7)
        cards = data["days"][0]["giri"]["utenti_tessere"]
        assert (cards["giri"], cards["senza_differenze"], cards["con_differenze"]) == (2, 1, 1)
        assert data["days"][0]["giri"]["programmazione"]["non_riusciti"] == 1
        assert data["unsaved"] == 3
        # Spazio di nuovo libero
        monkeypatch.setattr(db, "_conn", real)
        statements: list[str] = []
        real.set_trace_callback(statements.append)
        shadow.record(1, CARDS, CLEAN, now, CARDS_ROUND)
        real.set_trace_callback(None)
        assert statements.count("COMMIT") == 1
        assert lines[-1] == "Storico del confronto in ombra di nuovo salvato"
    rows = {r["round"]: r for r in db.shadow_days("2000-01-01")}
    assert (rows[CARDS_ROUND]["rounds"], rows[CARDS_ROUND]["clean"], rows[CARDS_ROUND]["differences"]) == (3, 2, 1)
    assert rows[CARDS_ROUND]["difference"].startswith("Tessera 04aa (Anna): Riporterebbe")
    assert (rows[PROGRAMMING_ROUND]["errors"], rows[PROGRAMMING_ROUND]["last_error"]) == (1, "disk I/O error")
    assert shadow.history(7)["unsaved"] == 0
    db.close()


class _capture_warnings(_capture):
    def __enter__(self):
        records = super().__enter__()
        self.handler.emit = lambda r: records.append(r.getMessage()) if r.levelno == logging.WARNING else None
        return records


def test_a_round_that_fails_is_in_the_history_and_in_the_log(client, controller, monkeypatch, caplog):  # noqa: F811
    """Un giro che si ferma per un errore (database, un profilo che l'app non accetta) conta fra i non riusciti, con
    il motivo, e nel log ha la sua riga; la sincronizzazione riesce lo stesso."""
    real = main.load_conf

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(main, "load_conf", broken)
    with caplog.at_level(logging.INFO, logger="app.shadow"):
        caplog.clear()
        assert client.post("/api/sync", headers=ADMIN).status_code == 200
        assert round_lines(caplog) == [
            "Confronto in ombra, giro di utenti e tessere dopo la sincronizzazione: non riuscito, disk I/O error"
        ]
    monkeypatch.setattr(main, "load_conf", real)

    def refused(*args):
        raise ServiceError("descrizione del profilo troppo lunga")

    monkeypatch.setattr(main, "confronto", refused)
    response = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN)
    assert response.status_code == 400
    today = today_of(history(client))
    assert (today["utenti_tessere"]["non_riusciti"], today["utenti_tessere"]["ultimo_errore"]) == (1, "disk I/O error")
    manual = today["confronta_ora"]
    assert (manual["giri"], manual["non_riusciti"], manual["senza_differenze"]) == (1, 1, 0)
    assert manual["ultimo_errore"] == "descrizione del profilo troppo lunga"
    rounds = client.app.state.shadow.as_dict()["rounds"]
    assert rounds[MANUAL_ROUND]["error"] == "descrizione del profilo troppo lunga"
    assert writes(controller) == []


def test_history_needs_the_plant_permission_and_hides_people(client, controller):  # noqa: F811
    """Permesso Impianti, come la scheda; senza Persone l'ultima differenza di utenti e tessere (nome, tessera) no."""
    anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN).json()
    controller.credentials[anna["cards"][0]["axis_credential_token"]]["Description"] = "Anna (a mano)"
    client.post("/api/sync", headers=ADMIN)
    for username, perms in (("mario", ["persone", "registro", "backup"]), ("tecnico", ["impianti"])):
        assert client.post("/api/users", json={"username": username, "perms": perms}, headers=ADMIN).status_code == 200

    assert client.get("/api/shadow", headers={"X-Remote-User-Name": "mario"}).status_code == 403
    response = client.get("/api/shadow", headers={"X-Remote-User-Name": "tecnico"})
    assert response.status_code == 200
    cards = today_of(response.json())["utenti_tessere"]
    assert cards["con_differenze"] >= 1 and cards["ultima_differenza_at"]
    assert (cards["ultima_differenza"], cards["ultima_differenza_nascosta"]) == ("", HIDDEN)
    assert "Anna" not in response.text and "04aa" not in response.text
    assert "Anna" in today_of(history(client))["utenti_tessere"]["ultima_differenza"]
    # Quanti giorni: da 1 a 30
    assert len(history(client, days=7)["days"]) == 7
    for days in (0, 31):
        assert client.get("/api/shadow", params={"days": days}, headers=ADMIN).status_code == 422


def test_history_from_direct_access_with_the_plant_permission(clients, controller):  # noqa: F811
    """Personale dell'accesso diretto: con il permesso Impianti legge lo storico (senza Persone, senza nomi), senza
    no."""
    ingress, site, app = clients
    enable(ingress)
    anna = ingress.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=DIRECT_ADMIN).json()
    controller.attempts.clear()
    controller.credentials[anna["cards"][0]["axis_credential_token"]]["Description"] = "Anna (a mano)"
    ingress.post("/api/sync", headers=DIRECT_ADMIN)
    ready(ingress, site, "tecnico", ("impianti",))
    response = site.get("/api/shadow?days=7")
    assert response.status_code == 200
    assert today_of(response.json())["utenti_tessere"]["ultima_differenza_nascosta"] == HIDDEN
    assert "Anna" not in response.text
    assert site.post("/api/logout", headers={"X-AAM-Request": "1"}).status_code == 200
    ready(ingress, site, "portiere", ("persone", "registro"))
    assert site.get("/api/shadow").status_code == 403
    assert writes(controller) == []


def test_database_of_0_28_1_gets_the_history(tmp_path):
    """Un database della 0.28.1 (senza la tabella): all'avvio la tabella si aggiunge, senza una migrazione e senza
    toccare il resto."""
    path = tmp_path / DB_NAME
    old = Database(path, "0.28.1")
    old._execute("DROP TABLE shadow_days")
    person = old.add_person("Anna", "u1")
    old.close()
    db = Database(path, "0.28.2")
    assert db.schema_version == SCHEMA_VERSION
    assert db.get_person(person)["name"] == "Anna"
    Shadow(db).record(1, CARDS, CLEAN, datetime.now(tz=UTC).isoformat(), CARDS_ROUND)
    assert [r["rounds"] for r in db.shadow_days("2000-01-01")] == [1]
    db.close()


# La scheda Confronto, fatta girare con node sulle risposte vere dell'app


@needs_node
def test_shadow_box_says_which_round_the_counts_come_from(client, controller):  # noqa: F811
    """Collaudo 0.28.1, punto 1: i conteggi della programmazione potevano essere di Confronta Ora o del giro di ogni
    ora, senza dirlo. Ora il riquadro dice da quale giro vengono, con l'ora."""
    bench_like(controller)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=ADMIN)
    client.post("/api/sync", headers=ADMIN)
    hourly(client)

    html = page_html("shadowHtml(DATA)", history(client)["current"])
    assert '<p class="cmp-source">Programmazione · conteggi del giro di ogni ora, oggi ' in html
    assert '<p class="cmp-source">Utenti e tessere · conteggi della sincronizzazione, oggi ' in html
    assert "Confronta Ora, oggi" not in html

    data = client.get(f"/api/controllers/{controller_id(client)}/comparison", headers=ADMIN).json()
    html = page_html("shadowHtml(DATA)", data["shadow"])
    assert '<p class="cmp-source">Programmazione · conteggi di Confronta Ora, oggi ' in html
    assert "conteggi del giro di ogni ora" not in html
    # Ogni tipo sotto il suo giro
    programming, cards = html.split('<p class="cmp-source">')[1:]
    assert "<dt>Porte</dt>" in programming and "<dt>Tessere</dt>" not in programming
    assert "<dt>Tessere</dt>" in cards


@needs_node
def test_last_7_days_in_the_comparison_tab(client, controller):  # noqa: F811
    """Ultimi 7 giorni: per giorno i giri di ogni ora e di utenti e tessere, quanti senza differenze, e in evidenza i
    giorni con differenze o con giri non riusciti."""
    state = client.app.state
    today = date.fromisoformat(plant_day())
    day = [(today - timedelta(days=n)).isoformat() for n in range(7)]

    def values(rounds, clean, differences=0, errors=0, difference="", error="", at=""):
        return {
            "rounds": rounds,
            "clean": clean,
            "differences": differences,
            "surprises": 0,
            "unread": 0,
            "errors": errors,
            "last_at": at or f"{day[0]}T12:00:00+01:00",
            "last_error": error,
            "difference": difference,
            "difference_kind": "tipologia" if difference else "",
            "difference_at": at if difference else "",
        }

    state.db.add_shadow_rounds(
        [
            (day[1], PROGRAMMING_ROUND, values(24, 24)),
            (day[1], CARDS_ROUND, values(96, 96)),
            (
                day[2],
                PROGRAMMING_ROUND,
                values(
                    24,
                    23,
                    differences=1,
                    difference=H24_DIFFERENCE,
                    at=f"{day[2]}T18:43:31+01:00",
                ),
            ),
            (day[2], CARDS_ROUND, values(96, 96)),
            (
                day[3],
                PROGRAMMING_ROUND,
                values(
                    20, 19, errors=1, error="il controller non risponde (tempo scaduto)", at=f"{day[3]}T06:50:02+01:00"
                ),
            ),
        ]
    )
    data = history(client, days=7)

    html = page_html("shadowHistoryHtml(DATA)", data)

    assert html.startswith('<h3 class="section-title" style="margin:16px 0 4px">Ultimi 7 Giorni</h3>')
    entries = html.split("<dt")[1:]
    assert len(entries) == 7
    assert "oggi, " in entries[0]
    assert '<dt class=""><span class="dot ok"></span>' in html
    assert "Ogni ora: 24 giri, 24 senza differenze<br>Utenti e tessere: 96 giri, 96 senza differenze</dd>" in entries[1]
    assert entries[2].startswith(' class="warn"><span class="dot warn"></span>')
    assert "Ogni ora: 24 giri, 23 senza differenze · 1 con differenze" in entries[2]
    assert (
        f"Ultima differenza alle {rome(f'{day[2]}T18:43:31+01:00')}: Profilo di Accesso H24: Riporterebbe" in entries[2]
    )
    assert entries[3].startswith(' class="err"><span class="dot err"></span>')
    assert "Ogni ora: 20 giri, 19 senza differenze · 1 non riuscito<br>Utenti e tessere: nessun giro" in entries[3]
    assert f"Ultimo giro non riuscito alle {rome(f'{day[3]}T06:50:02+01:00')}: il controller non risponde" in entries[3]
    assert "Ogni ora: nessun giro<br>Utenti e tessere: nessun giro</dd>" in entries[6]
    assert "Confronta Ora" not in html

    # Senza il permesso Persone: la differenza di utenti e tessere non si vede, si dice perché
    state.db.add_shadow_rounds(
        [
            (
                day[0],
                CARDS_ROUND,
                {
                    **values(
                        1,
                        0,
                        differences=1,
                        difference="Tessera 04aa (Anna): Riporterebbe",
                        at=f"{day[0]}T09:15:00+01:00",
                    ),
                    "difference_kind": "tessera",
                },
            )
        ]
    )
    assert (
        client.post("/api/users", json={"username": "tecnico", "perms": ["impianti"]}, headers=ADMIN).status_code == 200
    )
    hidden = client.get("/api/shadow?days=7", headers={"X-Remote-User-Name": "tecnico"}).json()
    html = page_html("shadowHistoryHtml(DATA)", hidden)
    # Come esc() della pagina: l'apostrofo di «l'anagrafica» diventa &#39;
    shown = escape(HIDDEN[0].upper() + HIDDEN[1:]).replace("&#x27;", "&#39;")
    assert f"Ultima differenza alle {rome(f'{day[0]}T09:15:00+01:00')}: {shown}" in html
    assert "Anna" not in html
