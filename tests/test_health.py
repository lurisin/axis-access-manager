import json

import pytest

from app.db import SCHEMA_VERSION, Database
from app.health import CLOCK_TOLERANCE, HealthMonitor, clock_problem, ntp_summary, paused_reason
from app.notifications import NotificationService
from app.vapix import ControllerGuard, VapixError


@pytest.fixture
def controller_row(db):
    controller_id = db.save_controller(
        None,
        {
            "name": "Quadro Teatro",
            "host": "a1210.local",
            "username": "root",
            "password": "x",
            "verify_ssl": False,
            "mqtt_topic_prefix": "accessi/x",
        },
    )
    return controller_id


@pytest.fixture
def monitor(db, controller):
    return HealthMonitor(db, lambda row: controller)


async def test_first_check_remembers_identity(monitor, db, controller_row):
    await monitor.check_all()

    row = db.get_controller(controller_row)
    assert (row["serial"], row["model"], row["firmware"], row["cert_fingerprint"]) == (
        "X",
        "A1210",
        "10.12.288.1",
        "AA:BB",
    )
    state = monitor.states[controller_row]
    assert state.online is True and state.latency_ms is not None
    assert state.ntp == {
        "enabled": True,
        "source": "static",
        "servers": ["it.pool.ntp.org"],
        "active": True,
        "offset": 0.367,
    }
    assert db.list_audit()[0]["action"] == "controller riconosciuto"


async def test_changed_controller_pauses_writes_until_accepted(monitor, db, controller, controller_row):
    await monitor.check_all()
    controller.serial = "Y"

    await monitor.check_all()

    row = db.get_controller(controller_row)
    reason = paused_reason(row)
    assert "numero di serie X → Y" in reason
    guard = ControllerGuard(controller)
    guard.paused = reason
    with pytest.raises(VapixError, match="scritture sospese"):
        await guard.set_user({"token": "u", "Name": "x"})
    # Letture e apertura della porta passano
    assert await guard.list_credentials() == []
    await guard.access_door("porta")

    # Un secondo giro uguale non ripete l'avviso
    await monitor.check_all()
    assert [a["action"] for a in db.list_audit()].count("controller cambiato") == 1

    monitor.accept_identity(controller_row, "admin")
    row = db.get_controller(controller_row)
    assert row["serial"] == "Y" and paused_reason(row) == ""


async def test_original_controller_back_clears_change(monitor, db, controller, controller_row):
    await monitor.check_all()
    controller.fingerprint = "CC:DD"
    await monitor.check_all()
    assert "certificato" in paused_reason(db.get_controller(controller_row))

    controller.fingerprint = "AA:BB"
    await monitor.check_all()

    assert paused_reason(db.get_controller(controller_row)) == ""


async def test_firmware_update_is_not_a_change(monitor, db, controller, controller_row):
    await monitor.check_all()
    controller.firmware = "12.0.1"

    await monitor.check_all()

    row = db.get_controller(controller_row)
    assert row["firmware"] == "12.0.1" and paused_reason(row) == ""
    assert db.list_audit()[0]["action"] == "firmware del controller aggiornato"


async def test_unreachable_after_two_failures(monitor, controller, controller_row):
    controller.offline = True
    await monitor.check_all()
    assert not monitor.states[controller_row].unreachable
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert state.unreachable and "non risponde" in state.error


async def test_clock_offset(monitor, controller, controller_row):
    controller.clock_skew = CLOCK_TOLERANCE + 120
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert abs(state.clock_offset - (CLOCK_TOLERANCE + 120)) < 3
    assert clock_problem(state)
    controller.clock_skew = 5
    await monitor.check_all()
    assert not clock_problem(monitor.states[controller_row])


def test_ntp_summary_variants():
    assert ntp_summary({}) == {"enabled": None, "source": "", "servers": [], "active": None, "offset": None}
    dhcp = {
        "client": {"serversSource": "DHCP", "staticServers": ["a"], "advertisedServers": ["10.0.0.1"], "synced": False}
    }
    assert ntp_summary(dhcp)["servers"] == ["10.0.0.1"] and ntp_summary(dhcp)["active"] is False


def test_fault_notifications(db):
    notifications = NotificationService(db)
    controllers = [
        {
            "id": 1,
            "name": "Quadro Teatro",
            "unreachable": False,
            "serial": "Y",
            "identity_change": "il controller Quadro Teatro è cambiato (numero di serie X → Y): conferma",
            "clock_problem": True,
            "clock_offset": -300.0,
            "checked_at": "2026-10-25T10:00:00+00:00",
        },
        {"id": 2, "name": "Villa 1", "unreachable": True, "error": "il controller non risponde", "checked_at": ""},
    ]
    items = notifications.collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=controllers,
        missing={"count": 3, "persons": 2, "since": "2026-10-25T09:00:00+00:00", "mass": True},
    )
    by_type = {i["type"]: i for i in items}
    assert by_type["controller"]["text"] == "Villa 1 · Il controller non risponde"
    assert by_type["controller_changed"]["title"] == "Controller Cambiato"
    assert by_type["clock"]["text"] == "Quadro Teatro · 5 min indietro rispetto a Home Assistant"
    assert by_type["missing_cards"]["severity"] == "crit"
    assert "3 tessere di 2 persone" in by_type["missing_cards"]["text"]


def test_controller_mqtt_offline_but_reachable(db):
    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=False,
        sync={"ok": True},
        door_names={},
        controllers=[{"id": 1, "name": "Quadro", "unreachable": False}],
    )
    assert [i["title"] for i in items] == ["Eventi del Controller Non Arrivano"]


def test_numbered_migrations_copy_old_database(tmp_path):
    path = tmp_path / "old.db"
    # Come un database della 0.17: tabelle di base, nessuna versione dello schema
    import sqlite3

    from app.db import SCHEMA

    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO persons (name, created_at, updated_at) VALUES ('Mario', '', '')")
    conn.commit()
    conn.close()

    db = Database(path, "0.18.0")
    assert db.schema_version == SCHEMA_VERSION
    assert db.all_persons()[0]["company"] == ""
    assert db.copied.startswith("old_") and (tmp_path / "copie_database" / db.copied).exists()
    db.close()

    # Stesso schema e stessa versione: nessuna copia
    again = Database(path, "0.18.0")
    assert again.copied == ""
    again.close()

    # Aggiornamento dell'add-on: copia, e se ne tengono 3
    for version in ("0.18.1", "0.18.2", "0.18.3"):
        Database(path, version).close()
    copies = sorted((tmp_path / "copie_database").iterdir())
    assert len(copies) == 3
    assert any("v0.18.2" in c.name for c in copies)


def test_damaged_database_restored_from_backup_copy(tmp_path):
    from app.backup_copy import consistent_copy, ha_copy_path

    path = tmp_path / "live.db"
    db = Database(path)
    db.add_audit("admin", "prova")
    db.close()
    consistent_copy(path, ha_copy_path(path))
    path.write_bytes(b"non un database" * 100)

    db = Database(path)

    assert "ripristinata la copia del backup" in db.recovered
    assert db.list_audit()[0]["action"] == "prova"
    db.close()
    assert list(tmp_path.glob("live_danneggiato_*.db"))


def test_backup_pre_script(tmp_path, monkeypatch):
    from app import backup_copy

    path = tmp_path / "x.db"
    Database(path).close()
    monkeypatch.setenv("AAM_DB_PATH", str(path))
    assert backup_copy.main() == 0
    assert backup_copy.is_healthy(backup_copy.ha_copy_path(path))


def test_identity_change_json_kept(db, controller_row):
    db.set_controller_identity_change(controller_row, json.dumps({"serial": "Z"}))
    # Senza identità memorizzata da confrontare il motivo resta generico
    assert "apparecchio diverso" in paused_reason(db.get_controller(controller_row))
