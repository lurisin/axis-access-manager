import json

import pytest

from app.db import SCHEMA_VERSION, Database
from app.health import (
    CLOCK_EVERY,
    CLOCK_TOLERANCE,
    HealthMonitor,
    clock_fault,
    clock_problem,
    ntp_summary,
    paused_reason,
)
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


async def test_clock_fault_says_which_clock_is_wrong(monitor, controller, controller_row):
    controller.clock_skew = CLOCK_TOLERANCE + 120
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert abs(state.clock_offset - (CLOCK_TOLERANCE + 120)) < 3
    # Controller sincronizzato via NTP (come al banco): a essere indietro è Home Assistant
    assert clock_fault(state) == "home_assistant" and not clock_problem(state)
    controller.ntp_synced = False
    state.answered = 0
    await monitor.check_all()
    assert clock_fault(state) == "controller" and clock_problem(state)
    controller.clock_skew = 5
    state.answered = 0
    await monitor.check_all()
    assert clock_fault(state) == "" and not clock_problem(state)


async def test_clock_read_every_few_checks(monitor, controller, controller_row):
    await monitor.check_all()
    controller.clock_skew = 300
    for _ in range(CLOCK_EVERY - 1):
        await monitor.check_all()
    state = monitor.states[controller_row]
    assert abs(state.clock_offset) < 3
    await monitor.check_all()
    assert state.clock_offset > 290
    # Tornato raggiungibile: ora e NTP si rileggono subito
    controller.clock_skew = 0
    controller.offline = True
    await monitor.check_all()
    controller.offline = False
    await monitor.check_all()
    assert abs(state.clock_offset) < 3


async def test_clock_offset_written_after_ntp(monitor, controller, controller_row):
    # Collaudo 0.19: appena avviato, chi leggeva lo stato durante la lettura dell'NTP vedeva "36 s avanti"
    controller.clock_skew = 96
    seen = []
    original = controller.ntp_info

    async def ntp_info():
        state = monitor.states[controller_row]
        seen.append((state.clock_offset, state.ntp))
        return await original()

    controller.ntp_info = ntp_info
    await monitor.check_all()
    assert seen == [(None, {})]
    assert clock_fault(monitor.states[controller_row]) == "home_assistant"


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
            "clock_fault": "controller",
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
    assert by_type["clock"]["title"] == "Orologio del Controller"
    assert by_type["clock"]["text"] == (
        "Quadro Teatro · 5 min indietro rispetto a Home Assistant, NTP non sincronizzato"
    )
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


def test_home_assistant_clock_notification(db):
    synced = {"unreachable": False, "clock_fault": "home_assistant", "clock_offset": 36.0 + 60, "checked_at": ""}
    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=[{**synced, "id": 1, "name": "Quadro"}, {**synced, "id": 2, "name": "Villa"}],
    )
    # Una sola notifica anche con più controller: l'orologio sbagliato è quello di Home Assistant
    assert [(i["title"], i["severity"]) for i in items] == [("Orologio di Home Assistant", "low")]
    assert items[0]["text"].startswith("Home Assistant è 96 s indietro rispetto a Quadro")


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


async def test_access_points_the_controller_does_not_manage_are_a_fault(monitor, controller, controller_row, db):
    # Come dopo un reset: punto di accesso ricreato, ma nessun controllo accessi decide per lui
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"] = []
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert state.unmanaged == ["Ingresso"]

    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=[{**state.as_dict(), "id": controller_row}],
    )
    assert [(i["title"], i["severity"]) for i in items] == [("Punti di Accesso Non Gestiti", "crit")]
    assert items[0]["text"].startswith("Quadro Teatro · Ingresso: il controller legge tessere e pulsanti")

    controller.access_controllers["Axis-x AccessController"]["AccessPoint"] = ["ap-in"]
    await monitor.recheck_managed(db.get_controller(controller_row))
    assert state.unmanaged == []


async def test_controller_back_online_wakes_the_card_sync(monitor, controller, controller_row, db, monkeypatch):
    """Dopo un reset o un riavvio le tessere si rileggono appena il controller risponde, non al giro dei 15 minuti."""
    import asyncio
    from types import SimpleNamespace

    from app import main

    class Stop(BaseException):
        pass

    async def one_round(_delay):
        raise Stop

    state = SimpleNamespace(
        health=monitor,
        installation=SimpleNamespace(primary=db.get_controller(controller_row)),
        controller=SimpleNamespace(paused=""),
        service=SimpleNamespace(notify_change=lambda: None),
        health_ready=asyncio.Event(),
        sync_wake=asyncio.Event(),
    )
    monkeypatch.setattr(main, "_apply_pause", lambda state: None)

    async def no_recovery(state):
        return None

    monkeypatch.setattr(main, "_recover_if_needed", no_recovery)
    monkeypatch.setattr(main.asyncio, "sleep", one_round)
    app = SimpleNamespace(state=state)

    async def round_with(offline: bool) -> bool:
        controller.offline = offline
        with pytest.raises(Stop):
            await main._health_loop(app)
        woke = state.sync_wake.is_set()
        state.sync_wake.clear()
        return woke

    assert await round_with(False) is False  # risponde da sempre: niente rilettura in più
    assert await round_with(True) is False  # non risponde
    assert await round_with(False) is True  # torna: rilettura subito
    assert await round_with(False) is False


async def test_rex_inputs_without_permission_are_a_fault(monitor, controller, controller_row, db):
    # Pulsante programmato fino alla 0.21.2: punto di accesso gestito, ma nessun profilo gli dà il permesso
    controller.access_points.append({"token": "ap-rex", "Entity": "porta", "Name": "Pulsante Portineria"})
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"].append("ap-rex")
    db.save_controller_input(
        controller_row, "REX", {"name": "Pulsante Portineria", "role": "rex", "access_point_token": "ap-rex"}
    )
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert state.rex_missing == ["Pulsante Portineria"] and state.unmanaged == []

    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=[{**state.as_dict(), "id": controller_row}],
    )
    assert [(i["title"], i["severity"]) for i in items] == [("Pulsanti Senza Permesso di Apertura", "crit")]
    assert items[0]["text"].startswith("Quadro Teatro · Pulsante Portineria: il controller nega l'apertura")

    # Con il profilo per le aperture senza tessera l'avviso sparisce al controllo successivo
    controller.profiles["rex"] = {
        "token": "rex",
        "AuthenticationProfile": ["REXOnly"],
        "AccessPolicy": [{"AccessPoint": "ap-rex", "Schedule": ["standard_always"]}],
    }
    await monitor.recheck_managed(db.get_controller(controller_row))
    assert state.rex_missing == []


def test_exit_button_openings_of_0_21_4_become_exits(tmp_path):
    path = tmp_path / "v5.db"
    db = Database(path)
    for detail, source in (
        ("Pulsante Uscita · Aux 1 · più pressioni", "Pulsante Portineria"),
        ("Relè Citofono · REX", "Via Consulta - Videocitofono"),
        ("tempo esteso", "Mario (HA)"),
    ):
        db.add_event(ts="2026-10-26T12:00:00+00:00", kind="apertura_remota", detail=detail, source=source)
    db._conn.execute("PRAGMA user_version = 5")
    db.close()

    db = Database(path)
    kinds = {e["source"]: e["kind"] for e in db.list_events()}
    assert kinds == {
        "Pulsante Portineria": "uscita",
        "Via Consulta - Videocitofono": "apertura_remota",
        "Mario (HA)": "apertura_remota",
    }
    db.close()
