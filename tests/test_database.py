"""Database dell'add-on sul disco: backup e ripristino di Home Assistant, database rovinato, disco pieno, pulizia del
registro, un anno di eventi."""

import asyncio
import errno
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app import backup_copy, main
from app import db as db_module
from app import ha as ha_module
from app.backup_copy import consistent_copy, ha_copy_path, recover, update_copy
from app.db import SCHEMA_VERSION, Database, events_of_controller
from app.events import Event, EventMonitor
from app.ha import HomeAssistant
from app.main import create_app
from app.service import AccessService

ADDON = Path(__file__).parent.parent / "axis_access_manager"
CONFIG = yaml.safe_load((ADDON / "config.yaml").read_text())
DB_NAME = "axis_access_manager.db"


def start(settings, controller, data: Path, media: Path | None = None) -> TestClient:
    ha = HomeAssistant(token="", snapshot_dir=media or data.parent / "media")
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=data / DB_NAME, home_assistant=ha)
    return TestClient(app)


def backup_pre(db_path: Path) -> subprocess.CompletedProcess:
    """Come il backup_pre del config.yaml: lo script da solo, in un altro processo."""
    command = CONFIG["backup_pre"].split()
    assert command[-1].endswith("app/backup_copy.py")
    return subprocess.run(
        [sys.executable, str(ADDON / "app" / "backup_copy.py")],
        env=dict(os.environ, AAM_DB_PATH=str(db_path)),
        capture_output=True,
        text=True,
        check=True,
    )


def _excluded(path: Path) -> bool:
    return any(PurePath(path).match(pattern) for pattern in CONFIG["backup_exclude"])


def _add(tar: tarfile.TarFile, origin: Path, arcname: str) -> None:
    # Come securetar.atomic_contents_add del Supervisor: la cartella, poi ogni voce che backup_exclude non toglie
    tar.add(origin, arcname=arcname, recursive=False)
    for item in origin.iterdir():
        if _excluded(item):
            continue
        if item.is_dir() and not item.is_symlink():
            _add(tar, item, f"{arcname}/{item.name}")
        else:
            tar.add(item, arcname=f"{arcname}/{item.name}", recursive=False)


def ha_backup(data: Path, archive: Path) -> list[str]:
    """Backup dell'add-on fatto da Home Assistant con l'add-on acceso: backup_pre, poi /data senza backup_exclude."""
    backup_pre(data / DB_NAME)
    with tarfile.open(archive, "w") as tar:
        _add(tar, data, "data")
    with tarfile.open(archive) as tar:
        return tar.getnames()


def ha_restore(archive: Path, target: Path) -> Path:
    """Ripristino di Home Assistant: cartella dei dati svuotata e riempita dal backup."""
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="fully_trusted")
    return target / "data"


def files(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def integrity(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        conn.close()


# Backup e ripristino di Home Assistant


@pytest.mark.parametrize("history", ["installato", "aggiornato", "prima della migrazione"])
def test_home_assistant_backup_and_restore_with_the_add_on_writing(settings, controller, tmp_path, history):
    data = tmp_path / "data"
    data.mkdir()
    media = tmp_path / "media"
    if history == "aggiornato":
        # Versione precedente: al primo avvio di questa si fa la copia in copie_database
        Database(data / DB_NAME, "0.25.0").close()
    if history == "prima della migrazione":
        old = Database(data / DB_NAME, "0.25.0")
        old._execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        old.close()
    with start(settings, controller, data, media) as client:
        for i in range(5):
            assert client.post("/api/persons", json={"name": f"Persona {i}", "card": f"04a{i}"}).status_code == 200
        assert client.post("/api/backups", json={"name": "Prima dei lavori"}).status_code == 200
        db = client.app.state.db
        install = db.install_id
        # Un'istantanea di un evento: sta nella cartella Media, fuori dal backup dell'add-on
        event_id = db.add_event(ts=datetime.now(tz=UTC).isoformat(timespec="seconds"), kind="negato", door="porta")
        media.mkdir(exist_ok=True)
        (media / f"{event_id}_foto.jpg").write_bytes(b"\xff\xd8foto")
        db.set_event_snapshot(event_id, f"{event_id}_foto.jpg")
        if history == "prima della migrazione":
            # Backup fatto dalla versione di prima: database con lo schema di prima
            db._execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
            db._execute("UPDATE app_settings SET value = '0.25.0' WHERE key = 'app_version'")
        stop = threading.Event()
        written = []

        def writer():
            # L'add-on scrive per tutto il backup: eventi e WAL che crescono
            while not stop.is_set():
                written.append(db.add_event(ts="2026-11-04T10:00:00+00:00", kind="concesso", raw={"p": "x" * 500}))
                time.sleep(0.001)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            time.sleep(0.2)
            names = ha_backup(data, tmp_path / "backup.tar")
        finally:
            stop.set()
            thread.join()
        at_backup = len(written)
    assert "data/copia_backup/axis_access_manager.db" in names
    # Le copie prima degli aggiornamenti e i database danneggiati restano fuori
    assert not [n for n in names if n.startswith("data/copie_database/") or "_danneggiato_" in n]
    if history == "aggiornato":
        assert list((data / "copie_database").iterdir())

    restored = ha_restore(tmp_path / "backup.tar", tmp_path / "ripristino")
    with start(settings, controller, restored, media) as client:
        state = client.app.state
        assert state.db.schema_version == SCHEMA_VERSION
        # Lo stesso database: la stessa installazione, anche con lo schema aggiornato all'avvio
        assert state.db.install_id == install
        assert len(client.get("/api/persons").json()) == 5
        assert [b["name"] for b in client.get("/api/backups").json()["backups"]] == ["Prima dei lavori"]
        # Dati del backup: almeno gli eventi scritti fino alla copia per il backup
        events = state.db._query("SELECT COUNT(*) AS n FROM events WHERE kind = 'concesso'")[0]["n"]
        assert 0 < events <= at_backup
        shot = state.db.get_event(event_id)
        assert shot["snapshot"] and (media / shot["snapshot"]).exists()
        recovered = state.db_recovery
        copies = [c["name"] for c in client.get("/api/status").json()["database"]["copies"]]
        if history == "prima della migrazione":
            # Schema aggiornato all'avvio, con la copia del database com'era nel backup
            assert len(copies) == 1 and copies[0].endswith("_v0.25.0.db")
        else:
            assert copies == []
    # Niente di incoerente: né WAL né copie a metà, né accanto al database né accanto alla copia
    left = files(restored)
    assert not [n for n in left if n.endswith(("-wal", "-shm", "-journal", ".parziale", ".ripristino"))], left
    assert integrity(restored / DB_NAME) == "ok"
    if recovered:
        # Database preso a metà scrittura: si riparte dalla copia coerente
        assert backup_copy.RESTORED in recovered["text"]


def _restored_with_index_damage(tmp_path: Path, wal: str) -> Path:
    """Cartella rimessa da un backup di Home Assistant con database e WAL copiati in momenti diversi: un indice che
    non torna con la tabella (quick_check non lo vede), il WAL accanto e la copia del backup sana."""
    path = tmp_path / DB_NAME
    db = Database(path, "0.26.0")
    db.add_audit("admin", "prima")
    db.close()
    consistent_copy(path, ha_copy_path(path))
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE prova (n INTEGER, m INTEGER)")
    conn.execute("CREATE INDEX prova_n ON prova(n)")
    conn.executemany("INSERT INTO prova VALUES (?, ?)", [(n, n + 1000) for n in range(1, 50)])
    conn.commit()
    # Indice che non torna con la tabella, come dopo una copia presa a metà: quick_check non lo vede. Le voci sono di n,
    # la definizione dice m (con la condizione di un indice parziale cambiata, dalla 3.53 SQLite non lo vede più)
    conn.execute("PRAGMA writable_schema = ON")
    conn.execute("UPDATE sqlite_master SET sql = replace(sql, 'prova(n)', 'prova(m)') WHERE name = 'prova_n'")
    conn.commit()
    conn.close()
    assert backup_copy.damage(path) == ""
    # Come nel backup: una scrittura ancora nel WAL
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("INSERT INTO prova VALUES (100, 1100)")
    writer.commit()
    restored = tmp_path / "ripristino"
    restored.mkdir()
    (restored / DB_NAME).write_bytes(path.read_bytes())
    (restored / f"{DB_NAME}-wal").write_bytes(Path(f"{path}-wal").read_bytes() if wal == "con scritture" else b"")
    writer.close()
    (restored / "copia_backup").mkdir()
    (restored / "copia_backup" / DB_NAME).write_bytes(ha_copy_path(path).read_bytes())
    return restored / DB_NAME


@pytest.mark.parametrize("wal", ["con scritture", "vuoto"])
def test_database_and_wal_copied_at_different_moments_use_the_backup_copy(tmp_path, wal):
    """Il Supervisor copia database e WAL in momenti diversi: gli indici possono non tornare con le tabelle, e
    quick_check non lo vede. Con il WAL rimasto il controllo all'avvio è completo e si riparte dalla copia. Anche con
    il WAL vuoto: copiato subito dopo lo svuotamento del backup_pre, mentre il database cambiava ancora."""
    restored = _restored_with_index_damage(tmp_path, wal)

    message = recover(restored)

    assert message.startswith("database danneggiato spostato in") and backup_copy.RESTORED in message
    again = Database(restored)
    assert [a["action"] for a in again.list_audit()] == ["prima"]
    again.close()


class _Killed(BaseException):
    """Spegnimento improvviso."""


@pytest.mark.parametrize("stop", ["spegnimento", "disco pieno"])
def test_restore_from_the_copy_interrupted_at_startup_is_not_forgotten(tmp_path, monkeypatch, stop):
    """Il controllo completo vede il danno agli indici solo con il WAL accanto, e chiudendo il database lo toglie. Se il
    primo avvio non finiva la copia (spegnimento, disco pieno), all'avvio dopo il danno non si vedeva più: l'app
    partiva con il database rovinato, e il backup dopo ne faceva la copia al posto di quella sana."""
    restored = _restored_with_index_damage(tmp_path, "vuoto")
    original = backup_copy.consistent_copy

    def interrupted(source, target, check=False):
        if stop == "spegnimento":
            raise _Killed
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_copy, "consistent_copy", interrupted)
    if stop == "spegnimento":
        with pytest.raises(_Killed):
            recover(restored)
        monkeypatch.setattr(backup_copy, "consistent_copy", original)
        message = recover(restored)
    else:
        message = recover(restored)
    assert backup_copy.RESTORED in message
    assert integrity(restored) == "ok"
    again = Database(restored)
    assert [a["action"] for a in again.list_audit()] == ["prima"]
    again.close()
    assert len(list(restored.parent.glob("axis_access_manager_danneggiato_*.db"))) == 1


def test_backup_copy_is_one_file_and_reading_it_leaves_nothing(tmp_path):
    path = tmp_path / DB_NAME
    Database(path).close()
    copy = ha_copy_path(path)
    # -wal e -shm lasciati accanto alla copia dalle versioni precedenti
    copy.parent.mkdir()
    for suffix in ("-wal", "-shm"):
        Path(f"{copy}{suffix}").write_bytes(b"vecchio")
    consistent_copy(path, copy)
    assert files(copy.parent) == [DB_NAME]
    # Letta in sola lettura (ripristino all'avvio): non resta niente accanto, che poi finiva nel backup
    reader = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    reader.execute("SELECT count(*) FROM sqlite_master").fetchone()
    reader.close()
    assert files(copy.parent) == [DB_NAME]


def test_backup_copy_error_written_with_the_disk_full_is_not_a_good_copy(settings, controller, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        backup_pre(data / DB_NAME)
        # Disco pieno al backup_pre: errore.txt si crea, ma il motivo non si scrive
        (data / backup_copy.HA_COPY_DIR / backup_copy.HA_COPY_ERROR).write_bytes(b"")
        status = client.get("/api/status").json()["database"]
        items = client.get("/api/notifications").json()["items"]
    # Prima la copia risultava «Preparata», in verde, e nessuna notifica
    assert status["ha_copy_error"] == "copia non aggiornata (spazio su disco esaurito)"
    assert "Copia per il Backup Non Aggiornata" in [i["title"] for i in items]


def test_backup_pre_empties_the_wal_while_the_add_on_writes(tmp_path):
    path = tmp_path / DB_NAME
    db = Database(path)
    for i in range(200):
        db.add_audit("admin", f"riga {i}", "x" * 500)
    assert Path(f"{path}-wal").stat().st_size > 0
    backup_pre(path)
    # Il file del database resta fermo finché il WAL non torna grande: copiato dal Supervisor è coerente
    assert Path(f"{path}-wal").stat().st_size == 0
    db.add_audit("admin", "dopo")
    assert db.list_audit(1)[0]["action"] == "dopo"
    db.close()


# Database che SQLite non apre


def _person_and_backup(settings, controller, data: Path) -> None:
    with start(settings, controller, data) as client:
        assert client.post("/api/persons", json={"name": "Anna Rossi", "card": "04aa"}).status_code == 200
        assert client.post("/api/backups", json={"name": "Prima del danno"}).status_code == 200


def _header(path: Path) -> None:
    content = bytearray(path.read_bytes())
    content[0:16] = b"x" * 16
    path.write_bytes(bytes(content))


def test_unreadable_database_without_copy_starts_empty_and_says_so(settings, controller, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _person_and_backup(settings, controller, data)
    _header(data / DB_NAME)

    # Prima l'add-on non partiva più, e i backup dell'app non si raggiungevano
    with start(settings, controller, data) as client:
        # Database nuovo: le Attività di prima non ci sono
        assert [a["action"] for a in client.app.state.db.list_audit()].count("database controllato all'avvio") == 1
        assert "persona creata" not in [a["action"] for a in client.app.state.db.list_audit()]
        assert [b["name"] for b in client.get("/api/backups").json()["backups"]] == ["Prima del danno"]
        items = client.get("/api/notifications").json()["items"]
        [notice] = [i for i in items if i["type"] == "database"]
        recovered = client.get("/api/status").json()["database"]["recovered"]
    assert notice["title"] == recovered["title"] == "Database Ripartito Vuoto"
    # Anche nel riquadro Database dell'Add-on: cosa fare, e subito
    assert recovered["hint"].startswith("Ripristina l'add-on da un backup di Home Assistant")
    assert notice["severity"] == "crit" and notice["fault"] is True
    assert "database vuoto" in notice["text"] and "Impianti → Backup" in notice["text"]
    assert "i backup di Home Assistant fatti da adesso contengono il database vuoto" in notice["text"]
    # Il danneggiato è da parte, fuori dai backup di Home Assistant
    [broken] = data.glob("axis_access_manager_danneggiato_*.db")
    assert _excluded(broken) and broken.read_bytes()[:16] == b"x" * 16


def test_started_empty_is_no_longer_a_fault_after_restoring_an_app_backup(settings, controller, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        client.post("/api/persons", json={"name": "Anna Rossi", "card": "04aa", "company": "ACME"})
        backup = client.post("/api/backups", json={"name": "Prima del danno"}).json()
    _header(data / DB_NAME)
    with start(settings, controller, data) as client:
        # La persona riletta dal controller, senza ditta, si toglie: il backup la rimette com'era
        client.portal.call(main._sync, client.app.state)
        for person in client.get("/api/persons").json():
            client.delete(f"/api/persons/{person['id']}")
        preview = client.post(f"/api/backups/{backup['id']}/preview", json={"sections": ["persone"]}).json()
        keys = [i["key"] for i in preview["items"] if i["default"]]
        body = {"sections": ["persone"], "keys": keys}
        assert client.post(f"/api/backups/{backup['id']}/restore", json=body).json()["applied"]
        [notice] = [i for i in client.get("/api/notifications").json()["items"] if i["type"] == "database"]
        recovered = client.get("/api/status").json()["database"]["recovered"]
        faults = [f["title"] for f in client.get("/api/summary").json()["faults"]]
    # Prima restava un guasto critico, con l'invito a ripristinare un backup di Home Assistant di prima del danno
    assert notice["title"] == "Database Ripartito Vuoto" and notice["fault"] is False
    assert "Database Ripartito Vuoto" not in faults
    assert (
        recovered["hint"]
        == notice["text"].split(". ")[-1]
        == ("Dati ripristinati da un backup dell'app: i backup di Home Assistant fatti da adesso li contengono.")
    )


def test_unreadable_database_and_copy_without_space_to_duplicate_it(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    _person_and_backup(settings, controller, data)
    backup_pre(data / DB_NAME)
    _header(data / DB_NAME)

    def full(source, target, check=False):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_copy, "consistent_copy", full)
    with start(settings, controller, data) as client:
        persons = client.get("/api/persons").json()
        recovered = client.app.state.db_recovery
    # Mai un database vuoto con una copia sana: il backup di Home Assistant successivo l'avrebbe sostituita
    assert [p["name"] for p in persons] == ["Anna Rossi"]
    assert recovered["title"] == "Database Ripristinato dalla Copia"
    assert "spostata al posto del database" in recovered["text"]
    assert not ha_copy_path(data / DB_NAME).exists()
    assert list(data.glob("axis_access_manager_danneggiato_*.db"))


def test_missing_database_with_damaged_copy_says_it_starts_empty(tmp_path):
    path = tmp_path / DB_NAME
    ha_copy_path(path).parent.mkdir()
    ha_copy_path(path).write_bytes(b"rovinata" * 100)
    assert recover(path).endswith(backup_copy.STARTED_EMPTY)


def test_unreadable_database_then_disk_full_still_says_the_copy_was_restored(
    settings, controller, tmp_path, monkeypatch
):
    data = tmp_path / "data"
    data.mkdir()
    _person_and_backup(settings, controller, data)
    db = Database(data / DB_NAME)
    db.add_session("sessione-vecchia", "reception", "2026-01-01T00:00:00+00:00", "10.0.0.9", "browser")
    db.close()
    backup_pre(data / DB_NAME)
    _header(data / DB_NAME)

    def full(source, target, check=False):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_copy, "consistent_copy", full)
    monkeypatch.setattr(main, "low_space", lambda folder: True)
    connect = Database._connect
    calls = []

    def connect_without_space(self, path, existed):
        calls.append(path)
        if len(calls) == 1:
            # Copia spostata al posto del database, poi SQLite non riesce a creare il WAL: disco pieno
            raise sqlite3.OperationalError("database or disk is full")
        connect(self, path, existed)

    monkeypatch.setattr(Database, "_connect", connect_without_space)
    with start(settings, controller, data) as client:
        persons = client.get("/api/persons").json()
        recovered = client.app.state.db_recovery
        sessions = client.app.state.db.list_sessions()
    assert len(calls) == 2 and [p["name"] for p in persons] == ["Anna Rossi"]
    # Al nuovo tentativo il database è sano: prima niente notifica, e le sessioni di prima restavano buone
    assert recovered["title"] == "Database Ripristinato dalla Copia"
    assert "spostata al posto del database" in recovered["text"]
    assert sessions == [] and "sessioni dell'Accesso Diretto sono state chiuse" in recovered["text"]


def test_missing_database_and_copy_without_space_to_duplicate_it(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    db = Database(path)
    db.add_person("Anna Rossi", "utente_anna")
    db.close()
    backup_pre(path)
    path.unlink()

    def full(source, target, check=False):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(backup_copy, "consistent_copy", full)
    db = Database(path)
    # Prima ripartiva vuota, all'avvio dopo non si riprovava e il backup successivo sostituiva la copia sana
    assert [p["name"] for p in db.list_persons()] == ["Anna Rossi"]
    assert backup_copy.RESTORED in db.recovered and "spostata al posto del database" in db.recovered
    assert not ha_copy_path(path).exists()
    db.close()


def test_damaged_page_of_the_log_does_not_set_the_database_aside(tmp_path):
    """Database che si apre, con una pagina del registro rovinata e nessuna copia: al primo avvio della versione
    nuova la creazione dell'indice delle persone legge tutto il registro. L'app parte con i dati, come prima."""
    path = tmp_path / DB_NAME
    db = Database(path)
    db.add_person("Anna Rossi", "utente_anna")
    db.close()
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO events (ts, kind, detail) VALUES ('2026-10-01', 'concesso', ?)", [("x" * 50,)] * 20000
    )
    # Database di una versione precedente, senza l'indice
    conn.execute("DROP INDEX events_person")
    conn.commit()
    leaf = [row[0] for row in conn.execute("SELECT pageno FROM dbstat WHERE name = 'events' AND pagetype = 'leaf'")][10]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    conn.close()
    content = bytearray(path.read_bytes())
    content[size * (leaf - 1) : size * leaf] = b"\xab" * size
    path.write_bytes(bytes(content))

    db = Database(path)
    # Prima tutto il database finiva da parte e l'app ripartiva vuota, senza persone e tessere ancora leggibili
    assert [p["name"] for p in db.list_persons()] == ["Anna Rossi"]
    assert db.recovered == "database danneggiato e nessuna copia sana del backup da cui ripartire"
    assert not list(tmp_path.glob("*_danneggiato_*"))
    db.close()


# Copia prima dell'aggiornamento


def test_update_copy_failure_does_not_stop_the_add_on(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    Database(data / DB_NAME, "0.25.0").close()

    def full(source, target, check=False):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backup_copy, "consistent_copy", full)
    with start(settings, controller, data) as client:
        state = client.app.state
        items = client.get("/api/notifications").json()["items"]
        status = client.get("/api/status").json()["database"]
        audit = [a for a in state.db.list_audit() if a["action"] == "copia del database prima dell'aggiornamento"]
        version = state.db.get_setting("app_version")
    [notice] = [i for i in items if i["type"] == "database"]
    assert notice["title"] == "Copia Prima dell'Aggiornamento Non Fatta"
    assert notice["text"].startswith("Copia del database prima dell'aggiornamento non fatta (")
    assert status["copy_error"]["text"] == audit[0]["detail"]
    # L'aggiornamento è fatto: al prossimo avvio non si riprova (e non si fallisce) di nuovo
    assert version != "0.25.0"


def test_update_copy_makes_room_before_writing(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    Database(path).close()
    for n in range(3):
        update_copy(path, f"v0.{n}")
        time.sleep(0.01)
    copies = tmp_path / "copie_database"
    seen = []
    original = backup_copy.consistent_copy

    def spy(source, target, check=False):
        # Con il disco quasi pieno la nuova copia trova il posto della più vecchia
        seen.append(len(list(copies.iterdir())))
        original(source, target, check)

    monkeypatch.setattr(backup_copy, "consistent_copy", spy)
    update_copy(path, "v0.3")
    assert seen == [2]
    assert sorted(p.name.rsplit("_", 1)[-1] for p in copies.iterdir()) == ["v0.1.db", "v0.2.db", "v0.3.db"]


def test_update_copy_removes_what_an_interrupted_copy_left(tmp_path):
    path = tmp_path / DB_NAME
    Database(path).close()
    copies = tmp_path / "copie_database"
    copies.mkdir()
    # Copia interrotta a un avvio precedente (spegnimento, arresto oltre il tempo del Supervisor)
    left = [copies / f"{DB_NAME[:-3]}_20261104_101010_v0.26.0.db.parziale", copies / f"{DB_NAME[:-3]}_x.db.parziale"]
    for leftover in left:
        leftover.write_bytes(b"x" * 4096)
        Path(f"{leftover}-journal").write_bytes(b"x")
    update_copy(path, "v0.26.0")
    # Prima restavano per sempre, grandi fino al database, e non si vedevano nell'app
    assert [p.name.rsplit("_", 1)[-1] for p in copies.iterdir()] == ["v0.26.0.db"]


# Sessioni dell'accesso diretto dopo il ripristino dalla copia


def test_database_restored_from_copy_closes_direct_access_sessions(settings, controller, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = Database(data / DB_NAME)
    db.save_app_user("reception", "Reception", ["persone"])
    db.set_password("reception", "hash-vecchio", False, "admin")
    set_at = db.get_password("reception")["set_at"]
    db.add_session("sessione-rubata", "reception", set_at, "10.0.0.9", "browser")
    db.close()
    backup_pre(data / DB_NAME)
    _header(data / DB_NAME)

    with start(settings, controller, data) as client:
        state = client.app.state
        sessions = state.db.list_sessions()
        text = state.db_recovery["text"]
    # Sessione e password della copia tornerebbero buone: le sessioni si chiudono e la notifica lo dice
    assert sessions == []
    assert backup_copy.RESTORED in text and "sessioni dell'Accesso Diretto sono state chiuse" in text


# Disco pieno e pulizia del registro


def _full(*args, **kwargs):
    err = sqlite3.OperationalError("database or disk is full")
    err.sqlite_errorcode = sqlite3.SQLITE_FULL
    raise err


def _old_events(db: Database, media: Path, count: int, days: int = 400) -> None:
    """Eventi oltre la conservazione, uno su dieci con l'istantanea nella cartella Media."""
    old = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    media.mkdir(exist_ok=True)
    rows = [(old, f"{n}_foto.jpg" if n % 10 == 0 else "", "x" * 300) for n in range(count)]
    with db._lock:
        db._conn.executemany("INSERT INTO events (ts, kind, snapshot, raw) VALUES (?, 'negato', ?, ?)", rows)
        db._conn.commit()
    for _, snapshot, _ in rows:
        if snapshot:
            (media / snapshot).write_bytes(b"\xff\xd8foto")


def _prune_state(db: Database, media: Path, days: int = 30):
    return SimpleNamespace(
        db=db,
        ha=HomeAssistant(token="", snapshot_dir=media),
        settings=SimpleNamespace(event_retention_days=days),
    )


async def test_prune_goes_in_blocks_and_the_app_keeps_running(tmp_path):
    db = Database(tmp_path / DB_NAME)
    media = tmp_path / "media"
    _old_events(db, media, 5000)
    db.add_event(ts=datetime.now(tz=UTC).isoformat(timespec="seconds"), kind="concesso")
    ticks = 0

    async def app_running():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    other = asyncio.create_task(app_running())
    await main._prune(_prune_state(db, media))
    other.cancel()
    # Un blocco alla volta, e tra un blocco e l'altro l'app va avanti: prima tutto in un colpo, a ciclo fermo
    assert ticks >= 3
    assert [e["kind"] for e in db.list_events()] == ["concesso"]
    assert list(media.iterdir()) == []
    assert Path(f"{tmp_path / DB_NAME}-wal").stat().st_size == 0
    db.close()


async def test_prune_deletes_old_snapshots_left_without_their_event(tmp_path):
    db = Database(tmp_path / DB_NAME)
    media = tmp_path / "media"
    media.mkdir()
    now = datetime.now(tz=UTC)
    kept = db.add_event(ts=(now - timedelta(days=10)).isoformat(timespec="seconds"), kind="negato")
    # Foto di eventi persi con il ripristino di un backup di Home Assistant: le righe non ci sono più
    old, recent, own = media / "4_vecchia.jpg", media / "5_recente.jpg", media / f"{kept}_sua.jpg"
    for path, days in ((old, 35), (recent, 3), (own, 10)):
        path.write_bytes(b"\xff\xd8foto")
        moment = (now - timedelta(days=days)).timestamp()
        os.utime(path, (moment, moment))
    await main._prune(_prune_state(db, media))
    # Prima restavano per sempre: la pulizia cancellava solo le foto delle righe scadute
    assert sorted(p.name for p in media.iterdir()) == sorted([recent.name, own.name])
    db.close()


async def test_prune_with_the_disk_full_deletes_snapshots_first(tmp_path, monkeypatch):
    db = Database(tmp_path / DB_NAME)
    media = tmp_path / "media"
    _old_events(db, media, 3000)
    # Disco pieno: il DELETE non può scrivere il WAL
    monkeypatch.setattr(db, "delete_events", _full)
    with pytest.raises(sqlite3.OperationalError):
        await main._prune(_prune_state(db, media))
    # Le istantanee del primo blocco sono già cancellate: lo spazio liberato serve al giro dopo. Prima non se ne
    # cancellava nessuna, né quel giorno né dopo
    assert len(list(media.iterdir())) == 300 - db_module.PRUNE_BLOCK // 10
    db.close()


async def test_snapshot_not_taken_when_the_disk_is_nearly_full(tmp_path, monkeypatch):
    usage = SimpleNamespace(total=32 * 1024**3, used=0, free=600 * 1024**2)
    monkeypatch.setattr(ha_module.shutil, "disk_usage", lambda path: usage)
    hass = HomeAssistant(token="token", snapshot_dir=tmp_path / "media")

    async def camera(*args, **kwargs):
        raise AssertionError("la telecamera non va chiesta")

    monkeypatch.setattr(hass._client, "get", camera)
    assert await hass.save_snapshot("camera.porta", "1_foto") == (
        "",
        "spazio su disco quasi esaurito: istantanea non salvata",
    )
    # Con più di 1 GB libero sì (il 5% del disco, 1,6 GB su 32 GB, vale solo se è meno)
    usage.free = 1100 * 1024**2
    assert not ha_module.low_space(tmp_path / "media")
    await hass.aclose()


async def test_snapshot_skipped_for_the_disk_does_not_blame_the_camera(tmp_path, monkeypatch):
    usage = SimpleNamespace(total=32 * 1024**3, used=0, free=500 * 1024**2)
    monkeypatch.setattr(ha_module.shutil, "disk_usage", lambda path: usage)
    db = Database(tmp_path / DB_NAME)
    event = db.add_event(ts="2026-11-04T10:00:00+00:00", kind="negato", door="cantina")
    state = SimpleNamespace(db=db, ha=HomeAssistant(token="token", snapshot_dir=tmp_path / "media"), snapshot_errors={})
    await main._save_snapshot(state, "camera.cantina", {"id": event, "ts": "2026-11-04T10:00:00+00:00"})
    # Il motivo resta sull'evento; la telecamera non è guasta. Prima «Telecamera Non Disponibile» per ogni telecamera,
    # anche dopo aver liberato spazio, fino al prossimo evento della porta
    [row] = db.list_events()
    assert row["snapshot_error"] == "spazio su disco quasi esaurito: istantanea non salvata"
    assert state.snapshot_errors == {}
    await state.ha.aclose()
    db.close()


def test_nearly_full_disk_is_a_fault(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    real = ha_module.shutil.disk_usage
    with start(settings, controller, data) as client:
        assert not [i for i in client.get("/api/notifications").json()["items"] if i["type"] == "database"]
        nearly_full = SimpleNamespace(total=32 * 1024**3, used=0, free=300 * 1024**2)
        monkeypatch.setattr(ha_module.shutil, "disk_usage", lambda path: nearly_full)
        items = client.get("/api/notifications").json()["items"]
        monkeypatch.setattr(ha_module.shutil, "disk_usage", real)
    [notice] = [i for i in items if i["type"] == "database"]
    assert notice["title"] == "Spazio su Disco Quasi Esaurito" and notice["fault"] is True
    assert notice["text"].startswith("Liberi 300 MB su 32,0 GB: le istantanee non si salvano")


def test_nearly_full_disk_seen_returns_when_space_keeps_going_down(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    usage = SimpleNamespace(total=32 * 1024**3, used=0, free=900 * 1024**2)
    with start(settings, controller, data) as client:
        monkeypatch.setattr(ha_module.shutil, "disk_usage", lambda path: usage)

        def notice():
            [item] = [i for i in client.get("/api/notifications").json()["items"] if i["id"] == "database:spazio"]
            return item

        client.post("/api/notifications/seen", json={"ids": ["database:spazio"]})
        assert notice()["seen"] is True
        # Prima restava vista fino al disco pieno
        usage.free = 100 * 1024**2
        assert notice()["seen"] is False
        client.post("/api/notifications/seen", json={"ids": ["database:spazio"]})
        usage.free = 120 * 1024**2
        assert notice()["seen"] is True
        usage.free = 10 * 1024**2
        assert notice()["seen"] is False


def test_full_disk_at_startup_deletes_expired_snapshots_and_starts(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    old, new = media / "1_vecchia.jpg", media / "2_nuova.jpg"
    for path in (old, new):
        path.write_bytes(b"\xff\xd8foto")
    year_ago = time.time() - 400 * 86400
    os.utime(old, (year_ago, year_ago))
    real = main.Database
    attempts = []

    def database(path, version):
        attempts.append(path)
        if len(attempts) == 1:
            # Come su ext4 pieno: SQLite non apre il file, o non crea il -shm
            raise sqlite3.OperationalError("unable to open database file")
        return real(path, version)

    monkeypatch.setattr(main, "Database", database)
    monkeypatch.setattr(main, "low_space", lambda folder: True)
    with start(settings, controller, data, media) as client:
        assert client.get("/api/status").status_code == 200
    # Prima l'add-on non partiva più: la pulizia che avrebbe liberato lo spazio non arrivava mai
    assert len(attempts) == 2
    assert not old.exists() and new.exists()


def test_checkpoint_after_the_prune_does_not_wait_for_readers(tmp_path):
    db = Database(tmp_path / DB_NAME)
    for i in range(200):
        db.add_audit("admin", f"riga {i}", "x" * 500)
    # Lettura lunga in corso su un'altra connessione (ricerca, CSV, copia per il backup)
    reader = sqlite3.connect(tmp_path / DB_NAME)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM audit").fetchone()
    db.add_audit("admin", "dopo")
    started = time.monotonic()
    db.checkpoint()
    waited = time.monotonic() - started
    reader.rollback()
    reader.close()
    # Prima il ciclo dell'app restava fermo 5 s
    assert waited < 1
    assert db._query("PRAGMA busy_timeout")[0]["timeout"] == 5000
    db.close()


def test_space_freed_by_the_prune_goes_back_to_the_disk(tmp_path):
    path = tmp_path / DB_NAME
    db = Database(path)
    assert db._query("PRAGMA auto_vacuum")[0]["auto_vacuum"] == 2
    assert db._query("PRAGMA journal_size_limit")[0]["journal_size_limit"] == db_module.WAL_SIZE_LIMIT
    _old_events(db, tmp_path / "media", 20000)
    db.checkpoint()
    before = path.stat().st_size
    while rows := db.expired_events(30):
        db.delete_events([row["id"] for row in rows])
    db.checkpoint()
    # Prima il file restava grande per sempre (e le sue copie con lui), e il WAL quasi quanto il database
    assert path.stat().st_size < before / 4
    assert Path(f"{path}-wal").stat().st_size == 0
    db.close()


def test_old_database_with_many_free_pages_is_compacted_at_startup(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    # Database creato senza auto_vacuum (le versioni precedenti), con un registro grande appena pulito
    conn = sqlite3.connect(path)
    conn.executescript(db_module.SCHEMA)
    conn.executemany("INSERT INTO events (ts, kind, raw) VALUES ('2025-01-01', 'negato', ?)", [("x" * 300,)] * 20000)
    conn.commit()
    conn.execute("DELETE FROM events")
    conn.commit()
    conn.close()
    before = path.stat().st_size
    monkeypatch.setattr(db_module, "COMPACT_FREE_PAGES", 100)
    db = Database(path)
    assert db._query("PRAGMA auto_vacuum")[0]["auto_vacuum"] == 2
    assert path.stat().st_size < before / 4
    db.close()


# Registro non scrivibile


async def test_event_not_saved_reaches_home_assistant_and_is_recovered_later(db, controller, settings, monkeypatch):
    service = AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))
    published = []

    async def after_event(row):
        published.append(row)

    service.after_event = after_event
    moment = datetime(2026, 10, 25, 10, 0, 5, tzinfo=UTC)
    granted = {"AccessPointToken": "ap-in", "Card": "04aa"}
    saved = db.add_event
    monkeypatch.setattr(db, "add_event", _full)
    service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Anonymous/", moment, dict(granted)))
    # Lo stesso passaggio su un altro topic: Home Assistant lo riceve una volta sola
    service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Card/", moment, dict(granted)))
    await asyncio.sleep(0)
    # Prima l'evento si perdeva in silenzio: niente registro, niente Home Assistant
    assert [(r["kind"], r["id"]) for r in published] == [("concesso", None)]
    assert service.write_error["error"] == "spazio su disco esaurito" and service.write_failures == 1

    # Spazio di nuovo libero: il recupero dal registro del controller lo rimette, anche se l'esito era già passato
    monkeypatch.setattr(db, "add_event", saved)
    keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(("AccessControl", "AccessGranted", "Anonymous"))]
    values = [{"Key": k, "Value": v} for k, v in granted.items()]
    controller.log_events = [{"rowid": 1, "UtcTime": "2026-10-25T10:00:05Z", "KeyValues": keys + values}]
    assert await service.recover_events(moment - timedelta(minutes=1)) == 1
    [row] = db.list_events()
    assert (row["kind"], row["recovered"]) == ("concesso", 1)
    assert service.write_error is None


class _CommitFails:
    """Connessione che al commit risponde come SQLite con il disco pieno: errore e transazione annullata."""

    def __init__(self, conn):
        self._real = conn

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        if self._real.in_transaction:
            self._real.rollback()
            _full()


@pytest.mark.parametrize("failure", ["add_event", "commit"])
async def test_recovery_that_fails_does_not_lose_the_passages(db, controller, settings, monkeypatch, failure):
    service = AccessService(db, controller, settings, EventMonitor(prefix="accessi/x", door_token="porta"))
    moment = datetime(2026, 10, 25, 10, 0, 5, tzinfo=UTC)
    granted = {"AccessPointToken": "ap-in", "Card": "04aa"}
    keys = [{"Key": f"topic{i}", "Value": t} for i, t in enumerate(("AccessControl", "AccessGranted", "Anonymous"))]
    values = [{"Key": k, "Value": v} for k, v in granted.items()]
    controller.log_events = [{"rowid": 1, "UtcTime": "2026-10-25T10:00:05Z", "KeyValues": keys + values}]
    saved = db.add_event
    monkeypatch.setattr(db, "add_event", _full)
    service.record_event(Event("AccessControl", "AccessControl/AccessGranted/Anonymous/", moment, dict(granted)))
    since = service.write_error["since"]
    # Disco ancora pieno al primo recupero: la pagina non si salva, nell'aggiunta della riga o al commit
    if failure == "commit":
        monkeypatch.setattr(db, "add_event", saved)
        monkeypatch.setattr(db, "_conn", _CommitFails(db._conn))
    with pytest.raises(sqlite3.OperationalError):
        await service.recover_events(moment - timedelta(minutes=1))
    assert db.list_events() == []
    # Il guasto resta quello di prima: la notifica non cambia
    assert service.write_error["since"] == since

    # Spazio di nuovo libero: il passaggio torna nel registro. Prima risultava già registrato e si perdeva per sempre
    monkeypatch.setattr(db, "add_event", saved)
    monkeypatch.setattr(db, "_conn", getattr(db._conn, "_real", db._conn))
    assert await service.recover_events(moment - timedelta(minutes=1)) == 1
    [row] = db.list_events()
    assert (row["kind"], row["recovered"]) == ("concesso", 1)
    assert service.write_error is None


def test_unsaved_events_hold_the_recovery_point_until_recovered(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        client.portal.call(state.health.check_all)
        state.monitor.ws_live = True
        for _ in range(2):
            client.portal.call(main._recover_if_needed, state)
        assert state.recover_pending is False
        calls = []
        original = state.service.recover_events

        async def recover(since):
            calls.append(since)
            if len(calls) == 1:
                _full()
            return await original(since)

        state.service.recover_events = recover
        mark = state.recover_from
        # Un evento in diretta non salvato (disco pieno)
        state.service.write_failures += 1
        state.service.write_error = {"since": "2026-11-04T10:00:00+00:00", "error": "spazio su disco esaurito"}
        items = client.get("/api/notifications").json()["items"]
        client.portal.call(main._recover_if_needed, state)
        # Registro ancora non scrivibile: il punto di recupero resta fermo, e si riprova
        assert len(calls) == 1 and state.recover_from == mark
        client.portal.call(main._recover_if_needed, state)
        client.portal.call(main._recover_if_needed, state)
    assert len(calls) == 2 and calls[1] == calls[0]
    [notice] = [i for i in items if i["type"] == "database"]
    assert notice["title"] == "Registro Non Scrivibile" and notice["severity"] == "crit"
    assert "Arrivano lo stesso a Home Assistant" in notice["text"]


async def test_door_opened_from_home_assistant_with_the_disk_full(db, controller, settings, monkeypatch):
    monitor = EventMonitor(prefix="accessi/x", door_token="porta")
    monitor.door_modes["porta"] = "Locked"
    service = AccessService(db, controller, settings, monitor)
    published = []

    async def after_event(row):
        published.append(row["kind"])

    service.after_event = after_event
    monkeypatch.setattr(db, "add_event", _full)
    monkeypatch.setattr(db, "add_audit", _full)
    # La porta si apre: prima risultava un errore e il log diceva "non eseguita"
    assert await service.open_door(False, "Luca (Home Assistant)") is True
    await asyncio.sleep(0)
    assert any(c[0] == "access_door" for c in controller.calls)
    assert published == ["apertura_remota"]


def test_extended_opening_not_saved_keeps_its_time_after_a_restart(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        monkeypatch.setattr(state.db, "add_event", _full)
        # Apri a Lungo da Home Assistant con il registro non scrivibile
        assert client.portal.call(state.service.open_door, True, "Luca (Home Assistant)") is True
    monkeypatch.undo()
    # Riavvio dell'add-on entro il tempo esteso, con il disco ancora quasi pieno
    monkeypatch.setattr(main, "low_space", lambda folder: True)
    with start(settings, controller, data) as client:
        state = client.app.state
        assert state.db.list_events(kind="apertura_remota") == []
        # Prima la rete di sicurezza la trattava come un'apertura normale: blocco dopo il tempo normale e un falso
        # Riblocco Forzato
        assert "porta" in state.lock_guard.extended_at
    monkeypatch.undo()
    # Con lo spazio una porta trovata Aperta a Tempo senza la riga resta un'apertura normale
    with start(settings, controller, data) as client:
        assert client.app.state.lock_guard.extended_at == {}


def test_notifications_and_backup_errors_with_the_database_not_writable(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        state.notifications.settled_at = 0
        state.db.set_setting("notifications_seen", '{"vecchia": 0}')
        monkeypatch.setattr(state.db, "set_setting", _full)
        # Prima la barra in alto rispondeva 500 quando doveva dimenticare una notifica vista
        assert client.get("/api/summary").status_code == 200
        state.backups._note_error("backup non salvato: spazio su disco esaurito")
        assert state.backups.last_error()["error"] == "backup non salvato: spazio su disco esaurito"


def test_app_rows_not_saved_return_to_the_log_when_space_returns(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        client.portal.call(state.health_ready.wait)
        saved = state.db.add_event
        monkeypatch.setattr(state.db, "add_event", _full)
        # Apertura da Home Assistant con il disco pieno: una riga che il registro del controller non ha
        assert client.portal.call(state.service.open_door, False, "Luca (Home Assistant)") is True
        assert "Registro Non Scrivibile" in [i["title"] for i in client.get("/api/summary").json()["faults"]]
        monkeypatch.setattr(state.db, "add_event", saved)
        # Spazio di nuovo libero e nessun passaggio: il recupero non trova niente da scrivere
        for _ in range(2):
            client.portal.call(main._recover_if_needed, state)
            client.portal.call(main._check_writable, state)
        faults = client.get("/api/summary").json()["faults"]
        rows = state.db.list_events(kind="apertura_remota")
    # Prima il guasto critico restava finché non passava qualcuno, e l'apertura non tornava più nel registro
    assert [i for i in faults if i["title"] == "Registro Non Scrivibile"] == []
    assert [(r["source"], r["recovered"]) for r in rows] == [("Luca (Home Assistant)", 0)]


def test_not_writable_fault_stays_while_the_test_write_fails(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        client.portal.call(state.health_ready.wait)
        state.service.write_error = {"since": "2026-11-04T10:00:00+00:00", "error": "spazio su disco esaurito"}
        probe = state.db.set_setting
        monkeypatch.setattr(state.db, "set_setting", _full)
        client.portal.call(main._check_writable, state)
        # Ancora pieno: il guasto resta, e il recupero non riparte a ogni giro
        assert state.service.write_error is not None and state.service.write_failures == 0
        monkeypatch.setattr(state.db, "set_setting", probe)
        client.portal.call(main._check_writable, state)
        assert state.service.write_error is None


def test_new_person_with_the_disk_full_leaves_nothing_on_the_controller(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        db = client.app.state.db
        anna = client.post("/api/persons", json={"name": "Anna", "card": "04aa"}).json()
        users, credentials = set(controller.users), set(controller.credentials)
        monkeypatch.setattr(db, "add_card", _full)
        response = client.post("/api/persons", json={"name": "Mario Rossi", "card": "04bb"})
        # Prima: errore 500 senza motivo, tessera attiva sul controller e assente nell'app; riprovando se ne
        # aggiungeva un'altra con lo stesso numero
        assert response.status_code == 400
        assert response.json()["detail"] == "spazio su disco esaurito: la persona non è stata registrata"
        assert (set(controller.users), set(controller.credentials)) == (users, credentials)
        assert [p["name"] for p in client.get("/api/persons").json()] == ["Anna"]
        # Anche una tessera aggiunta a una persona
        response = client.post(f"/api/persons/{anna['id']}/cards", json={"card": "04cc"})
        assert response.json()["detail"] == "spazio su disco esaurito: la tessera non è stata aggiunta"
        assert set(controller.credentials) == credentials
        monkeypatch.undo()
        # Riattivata con il disco pieno: torna sospesa sul controller, come la mostra l'app
        [card] = anna["cards"]
        token = card["axis_credential_token"]
        assert client.patch(f"/api/cards/{card['id']}", json={"enabled": False}).status_code == 200
        monkeypatch.setattr(db, "set_card_enabled", _full)
        response = client.patch(f"/api/cards/{card['id']}", json={"enabled": True})
        assert response.json()["detail"] == "spazio su disco esaurito: la tessera non è stata riattivata"
        assert controller.credentials[token]["Enabled"] is False
        # Sospesa con il disco pieno: resta sospesa sul controller, e l'errore lo dice
        monkeypatch.undo()
        assert client.patch(f"/api/cards/{card['id']}", json={"enabled": True}).status_code == 200
        monkeypatch.setattr(db, "set_card_enabled", _full)
        response = client.patch(f"/api/cards/{card['id']}", json={"enabled": False})
        assert response.json()["detail"].startswith("spazio su disco esaurito: la tessera è sospesa sul controller")
        assert controller.credentials[token]["Enabled"] is False


def test_seen_with_the_disk_full_says_why(settings, controller, tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    with start(settings, controller, data) as client:
        state = client.app.state
        # Dopo il primo giro di controllo, che con il registro scrivibile toglierebbe il guasto
        client.portal.call(state.health_ready.wait)
        state.service.write_error = {"since": "2026-11-04T10:00:00+00:00", "error": "spazio su disco esaurito"}
        [notice] = [i for i in client.get("/api/notifications").json()["items"] if i["type"] == "database"]
        set_setting = state.db.set_setting
        monkeypatch.setattr(state.db, "set_setting", _full)
        response = client.post("/api/notifications/seen", json={"ids": [notice["id"]]})
        # Prima «Richiesta non riuscita (errore 500)», senza motivo
        assert response.status_code == 503
        assert response.json()["detail"] == (
            "spazio su disco esaurito: la modifica non è stata salvata, libera spazio e riprova"
        )
        # Presa visione salvata e attività no: non è un errore
        monkeypatch.setattr(state.db, "set_setting", set_setting)
        monkeypatch.setattr(state.db, "add_audit", _full)
        assert client.post("/api/notifications/seen", json={"ids": [notice["id"]]}).json() == {"open": 1}


# Un anno di registro


def test_last_accesses_and_events_of_a_person_use_the_index(tmp_path):
    db = Database(tmp_path / DB_NAME)
    person = db.add_person("Anna", None)
    for day in (1, 3, 2):
        db.add_event(ts=f"2026-10-0{day}T08:00:00+00:00", kind="concesso", person_id=person)
    db.add_event(ts="2026-10-05T08:00:00+00:00", kind="negato", person_id=person)
    assert db.last_access(person) == "2026-10-03T08:00:00+00:00"
    assert db.last_accesses() == {person: "2026-10-03T08:00:00+00:00"}
    assert db.last_access(person + 1) == ""

    def plan(sql: str, params: tuple = ()) -> str:
        return " ".join(row["detail"] for row in db._query(f"EXPLAIN QUERY PLAN {sql}", params))

    # Senza leggere tutto il registro (1,5 milioni di righe in un anno)
    assert "events_person" in plan(
        "SELECT person_id, MAX(ts) AS ts FROM events WHERE person_id IS NOT NULL AND kind = 'concesso' "
        "GROUP BY person_id"
    )
    assert "events_person" in plan("SELECT id FROM events WHERE person_id = ? ORDER BY ts DESC, id DESC LIMIT 50", (1,))
    db.close()


def _has_person_index(path: Path) -> bool:
    conn = sqlite3.connect(path)
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events_person'").fetchone()
    conn.close()
    return bool(found)


def test_person_index_waits_for_space_and_the_add_on_starts(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    db = Database(path, "0.26.0")
    for day in range(1, 4):
        db.add_event(ts=f"2026-10-0{day}T08:00:00+00:00", kind="concesso", person_id=1)
    # Come un database della 0.26.0: senza l'indice
    db._execute("DROP INDEX events_person")
    db._conn.execute("PRAGMA incremental_vacuum").fetchall()
    db._conn.commit()
    db.close()
    # Disco quasi pieno al primo avvio dopo l'aggiornamento: l'indice riempiva il disco e l'add-on non partiva più
    monkeypatch.setattr(db_module.shutil, "disk_usage", lambda folder: SimpleNamespace(total=1 << 34, free=1 << 20))
    Database(path, "0.26.1").close()
    assert not _has_person_index(path)
    monkeypatch.undo()
    # Spazio stimato bene ma finito durante la creazione: si parte lo stesso
    connect = Database._connect

    def tight(self, name, existed):
        connect(self, name, existed)
        pages = self._conn.execute("PRAGMA page_count").fetchone()[0]
        self._conn.execute(f"PRAGMA max_page_count = {pages}")

    monkeypatch.setattr(Database, "_connect", tight)
    db = Database(path, "0.26.1")
    assert db.last_access(1) == "2026-10-03T08:00:00+00:00"
    db.close()
    assert not _has_person_index(path)
    # Con lo spazio, a un avvio dopo
    monkeypatch.undo()
    Database(path, "0.26.1").close()
    assert _has_person_index(path)


def test_short_reads_from_the_app_loop_do_not_wait_for_searches(tmp_path):
    db = Database(tmp_path / DB_NAME)
    db.add_event(ts="2026-10-01T08:00:00+00:00", kind="concesso")
    # Ricerche lunghe in corso nei thread: tutti i posti delle letture lunghe occupati
    for _ in range(db_module.READ_SLOTS):
        db._read_slots.acquire()

    async def summary():
        return db.list_events(limit=8)

    result = []
    thread = threading.Thread(target=lambda: result.append(asyncio.run(summary())))
    thread.start()
    thread.join(2)
    answered = bool(result)
    for _ in range(db_module.READ_SLOTS):
        db._read_slots.release()
    thread.join()
    # Prima il riepilogo, le notifiche e Home Assistant aspettavano la fine delle ricerche, e con loro tutta l'app
    assert answered and len(result[0]) == 1
    db.close()


def test_long_reads_do_not_hold_up_the_app(tmp_path):
    db = Database(tmp_path / DB_NAME)
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 4000000) SELECT count(*) FROM n"
    reading = threading.Event()

    def search():
        reading.set()
        db._read(slow)

    thread = threading.Thread(target=search)
    thread.start()
    reading.wait()
    time.sleep(0.05)
    started = time.monotonic()
    # Scritture e letture dell'app mentre la ricerca va avanti: prima aspettavano il lock della connessione unica
    db.add_audit("admin", "durante la ricerca")
    db.list_persons()
    waited = time.monotonic() - started
    still_reading = thread.is_alive()
    thread.join()
    assert still_reading and waited < 0.2
    db.close()


# Migrazione 9 (0.27.0): controller degli eventi e identità dell'installazione

V0261_SCHEMA = 8


def database_0_26_1(path: Path, events: int = 0) -> None:
    """Database come lo lascia la 0.26.1: schema 8, senza events.controller_id né install_id, con events passaggi da
    circa 1 KB nel registro."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(db_module, "SCHEMA_STEPS", [s for s in db_module.SCHEMA_STEPS if s[0] <= V0261_SCHEMA])
        patch.setattr(db_module, "SCHEMA_VERSION", V0261_SCHEMA)
        old = Database(path, "0.26.1")
    person = old.add_person("Anna Rossi", "utente_anna_rossi_261101120000")
    with old._lock:
        old._conn.executemany(
            "INSERT INTO events (ts, kind, person_id, door, raw) VALUES (?, 'concesso', ?, 'porta', ?)",
            [(f"2026-10-01T08:{n // 60 % 60:02d}:{n % 60:02d}+00:00", person, "x" * 1000) for n in range(events)],
        )
        old._conn.commit()
    old.close()
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == V0261_SCHEMA
        assert "controller_id" not in {row[1] for row in conn.execute("PRAGMA table_info(events)")}
        assert not conn.execute("SELECT 1 FROM app_settings WHERE key = 'install_id'").fetchall()
    finally:
        conn.close()


def test_database_of_0_26_1_gets_the_controller_of_events_without_rewriting_the_log(tmp_path, monkeypatch):
    path = tmp_path / DB_NAME
    database_0_26_1(path, events=5000)
    size = path.stat().st_size
    db = Database(path, "0.27.0")
    # Solo lo schema cambia: un UPDATE di tutto il registro scriveva nel WAL quanto il registro, e con un anno di
    # passaggi e il disco quasi pieno non finiva
    assert size > 5_000_000 and Path(f"{path}-wal").stat().st_size < 100_000
    assert db.schema_version == SCHEMA_VERSION
    counts = db._query("SELECT COUNT(*) AS n, COUNT(controller_id) AS with_controller FROM events")[0]
    assert counts == {"n": 5000, "with_controller": 0}
    install = db.install_id
    assert uuid.UUID(install).version == 4
    new = db.add_event(ts="2026-11-07T10:00:00+00:00", kind="concesso", door="porta", controller_id=1)
    assert db.get_event(new)["controller_id"] == 1
    db.close()
    # La copia prima della migrazione è il database com'era
    copy = sqlite3.connect(tmp_path / "copie_database" / db.copied)
    try:
        assert db.copied.endswith("_v0.26.1.db")
        assert copy.execute("PRAGMA user_version").fetchone()[0] == V0261_SCHEMA
        assert "controller_id" not in {row[1] for row in copy.execute("PRAGMA table_info(events)")}
    finally:
        copy.close()
    # Già migrato: niente migrazione, niente copia, stessa identità
    calls = []
    monkeypatch.setattr(Database, "_events_controller", lambda self: calls.append("controller"))
    monkeypatch.setattr(Database, "_new_install_id", lambda self: calls.append("install_id"))
    db = Database(path, "0.27.0")
    assert (db.copied, calls, db.install_id, db.schema_version) == ("", [], install, SCHEMA_VERSION)
    db.close()
    assert len(list((tmp_path / "copie_database").iterdir())) == 1


def test_install_id_stays_with_the_database_and_is_new_for_an_empty_one(tmp_path):
    path = tmp_path / DB_NAME
    db = Database(path, "0.27.0")
    install = db.install_id
    db.close()
    assert uuid.UUID(install).version == 4
    db = Database(path, "0.27.0")
    assert db.install_id == install
    db.close()
    # Database rovinato e copia del backup di Home Assistant: è sempre la stessa installazione
    backup_pre(path)
    _header(path)
    db = Database(path, "0.27.0")
    assert backup_copy.RESTORED in db.recovered and db.install_id == install
    db.close()
    # Senza una copia sana il database riparte vuoto: per il controller è un'altra installazione
    _header(path)
    ha_copy_path(path).unlink()
    db = Database(path, "0.27.0")
    assert backup_copy.STARTED_EMPTY in db.recovered
    assert db.install_id != install and uuid.UUID(db.install_id).version == 4
    db.close()


def test_events_without_controller_are_of_the_primary(tmp_path):
    db = Database(tmp_path / DB_NAME)
    before = db.add_event(ts="2026-11-01T08:00:00+00:00", kind="concesso")
    primary = db.add_event(ts="2026-11-02T08:00:00+00:00", kind="concesso", controller_id=1)
    other = db.add_event(ts="2026-11-03T08:00:00+00:00", kind="concesso", controller_id=2)

    def of(controller_id: int, is_primary: bool) -> set[int]:
        clause, params = events_of_controller(controller_id, is_primary)
        return {row["id"] for row in db._query(f"SELECT id FROM events WHERE {clause}", params)}

    assert of(1, True) == {before, primary}
    assert of(2, False) == {other}
    # Le letture di oggi non filtrano per controller
    assert {e["id"] for e in db.list_events()} == {before, primary, other}
    db.close()
