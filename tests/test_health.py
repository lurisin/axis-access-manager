import json
from datetime import UTC, datetime, timedelta

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
    zone_differs,
)
from app.notifications import NotificationService
from app.vapix import ControllerGuard, VapixError


@pytest.fixture(autouse=True)
def home_assistant_time_zone(monkeypatch):
    """Home Assistant con lo stesso fuso del controller finto, qualunque sia quello di chi esegue i test."""
    monkeypatch.setenv("TZ", "Europe/Rome")


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


async def test_controller_time_zone_compared_with_home_assistant(monitor, controller, controller_row, db, monkeypatch):
    # Come dopo un reset: controller su UTC, Home Assistant su Europe/Rome, orologio giusto
    controller.time_zone = "UTC"
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert state.zone_differs and clock_fault(state) == ""
    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=[{**state.as_dict(), "id": controller_row}],
    )
    assert [(i["type"], i["title"]) for i in items] == [("clock", "Fuso Orario del Controller")]
    assert items[0]["text"] == "Quadro Teatro · UTC invece di Europe/Rome"

    controller.time_zone = "Europe/Berlin"
    await monitor.recheck_clock(db.get_controller(controller_row))
    assert not state.zone_differs
    # Senza il fuso di Home Assistant non si segnala niente
    monkeypatch.delenv("TZ")
    assert not zone_differs({"timeZone": "UTC"}, "")
    # Fuso POSIX senza nome: vale lo scarto dell'ora locale
    winter = datetime(2026, 1, 10, tzinfo=UTC)
    assert not zone_differs({"localDateTime": "2026-01-10T13:00:00+01:00"}, "Europe/Rome", winter)
    assert zone_differs({"localDateTime": "2026-01-10T12:00:00+00:00"}, "Europe/Rome", winter)
    # Stesse ore d'inverno ma non d'estate
    assert zone_differs({"timeZone": "UTC"}, "Europe/London", winter)


def test_seen_faults_come_back_only_when_they_get_worse(db):
    service = NotificationService(db, settle=timedelta(0))

    def collect(count, mass=False):
        return service.collect(
            mqtt_configured=True,
            mqtt_connected=True,
            controller_configured=True,
            controller_online=True,
            sync={"ok": True},
            door_names={},
            missing={"count": count, "persons": count, "since": "2026-10-25T09:00:00+00:00", "mass": mass},
        )

    items = collect(1)
    service.mark_seen([i["id"] for i in items], items)
    assert service.listing(collect(1))["open"] == 0
    # Reset del controller: spariscono tutte le tessere, la notifica torna da gestire
    assert service.listing(collect(40, mass=True))["open"] == 1
    service.mark_seen([i["id"] for i in items], collect(40, mass=True))
    # Riscritte in parte: migliora, resta vista
    assert service.listing(collect(5))["open"] == 0


def test_seen_kept_while_faults_come_back_after_a_restart(db):
    def collect(service, offline):
        return service.collect(
            mqtt_configured=True,
            mqtt_connected=True,
            controller_configured=True,
            controller_online=True,
            sync={"ok": True},
            door_names={},
            controllers=[{"id": 1, "name": "Quadro", "unreachable": offline, "online": not offline}],
        )

    service = NotificationService(db, settle=timedelta(0))
    service.mark_seen(["controller:1"], collect(service, True))
    # Riavvio: per i primi minuti il controller non risulta ancora irraggiungibile, la presa visione resta
    restarted = NotificationService(db)
    assert restarted.listing(collect(restarted, False))["open"] == 0
    assert restarted.listing(collect(restarted, True))["open"] == 0


def test_seen_group_stays_seen_when_first_events_leave_the_window(db):
    service = NotificationService(db, settle=timedelta(0))
    base = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    for minutes in (0, 2, 4):
        ts = (base + timedelta(minutes=minutes)).isoformat()
        db.add_event(ts=ts, kind="allarme", door="porta", detail="porta forzata")

    def collect(now):
        return service.collect(
            mqtt_configured=True,
            mqtt_connected=True,
            controller_configured=True,
            controller_online=True,
            sync={"ok": True},
            door_names={"porta": "Ingresso"},
            now=now,
        )

    items = collect(base + timedelta(minutes=10))
    service.mark_seen([i["id"] for i in items], items)
    # Il giorno dopo il primo allarme esce dalle 24 ore: il gruppo si accorcia ma resta visto
    listing = service.listing(collect(base + timedelta(hours=24, minutes=1)))
    assert [(i["title"], i["seen"]) for i in listing["items"]] == [("Allarme Porta ×2", True)]


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
    assert by_type["controller"]["text"] == "Villa 1 · il controller non risponde"
    assert by_type["controller_changed"]["title"] == "Controller Cambiato"
    assert by_type["clock"]["title"] == "Orologio del Controller"
    assert by_type["clock"]["text"] == (
        "Quadro Teatro · 5 min indietro rispetto a Home Assistant, NTP non sincronizzato"
    )
    assert by_type["missing_cards"]["severity"] == "crit"
    assert "3 tessere di 2 persone" in by_type["missing_cards"]["text"]


def test_controller_mqtt_offline_but_reachable(db):
    def titles(**health):
        items = NotificationService(db).collect(
            mqtt_configured=True,
            mqtt_connected=True,
            controller_configured=True,
            controller_online=False,
            sync={"ok": True},
            door_names={},
            controllers=[{"id": 1, "name": "Quadro", **health}],
        )
        return [i["title"] for i in items]

    assert titles(online=True, unreachable=False) == ["Eventi del Controller Non Arrivano"]
    # Primo controllo fallito: non si dice che il controller risponde
    assert titles(online=False, unreachable=False) == []


def test_controller_off_the_broker_with_another_controller_down(db):
    # Collaudo 29/10: il principale risponde ma è scollegato dal broker da ore; un altro controller non risponde
    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=False,
        sync={"ok": True},
        door_names={},
        primary_id=1,
        controller_offline_since="2026-10-29T11:33:00+00:00",
        controllers=[
            {"id": 1, "name": "Quadro Hall", "online": True, "unreachable": False},
            {"id": 2, "name": "Villa 3", "online": False, "unreachable": True, "error": "non risponde"},
        ],
    )
    fault = next(i for i in items if i["id"] == "events@2026-10-29T11:33:00+00:00")
    assert fault["severity"] == "crit" and fault["ts"] == "2026-10-29T11:33:00+00:00"
    assert fault["text"] == "Quadro Hall · MQTT: controller non collegato al broker"


def _broker_fault(service, now, since="2026-10-29T11:33:00+00:00"):
    return service.collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=False,
        sync={"ok": True},
        door_names={},
        primary_id=1,
        controller_offline_since=since,
        controllers=[{"id": 1, "name": "Rack Portineria", "online": True, "unreachable": False}],
        now=now,
    )


def test_acknowledged_broker_fault_comes_back_while_it_lasts(db):
    from datetime import UTC, datetime

    service = NotificationService(db)
    # Collaudo 29/10: preso visione alle 18:20 e rimasto nascosto per ore; ora torna ogni 4 ore finché dura
    seen_at = _broker_fault(service, datetime(2026, 10, 29, 18, 20, 22, tzinfo=UTC))
    assert seen_at[0]["ts"] == "2026-10-29T11:33:00+00:00"
    service.mark_seen([seen_at[0]["id"]], seen_at)
    assert service.listing(_broker_fault(service, datetime(2026, 10, 29, 19, 0, tzinfo=UTC)))["items"][0]["seen"]
    later = _broker_fault(service, datetime(2026, 10, 29, 19, 34, tzinfo=UTC))
    assert service.listing(later)["items"][0]["seen"] is False


def test_broker_fault_that_starts_again_is_not_seen(db):
    from datetime import UTC, datetime

    service = NotificationService(db)
    seen_at = _broker_fault(service, datetime(2026, 10, 29, 18, 20, 22, tzinfo=UTC))
    service.mark_seen([seen_at[0]["id"]], seen_at)
    # Collaudo 29/10: con l'aggiornamento dell'add-on il guasto riparte alle 21:46:04 ma restava visto dalle 18:20
    restarted = NotificationService(db)
    items = _broker_fault(restarted, datetime(2026, 10, 29, 21, 47, tzinfo=UTC), "2026-10-29T21:46:04+00:00")
    assert items[0]["ts"] == "2026-10-29T21:46:04+00:00"
    listing = restarted.listing(items)
    assert listing["items"][0]["seen"] is False and listing["open"] == 1
    # Il guasto nuovo si prende visione come gli altri
    restarted.mark_seen([items[0]["id"]], items)
    assert restarted.listing(items)["open"] == 0


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
    assert items[0]["text"] == "96 s indietro rispetto a Quadro"


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
    assert [(i["title"], i["type"], i["severity"]) for i in items] == [
        ("Punto di Accesso Non Gestito", "programming", "crit")
    ]
    assert items[0]["text"] == "Quadro Teatro · Ingresso"

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
    row = {
        "name": "Pulsante Portineria",
        "role": "rex",
        "door_token": "porta",
        "access_point_token": "ap-rex",
        "idpoint_token": "id-rex",
    }
    db.save_controller_input(controller_row, "REX", row)
    await monitor.check_all()
    state = monitor.states[controller_row]
    assert state.rex_missing == ["Pulsante Portineria"] and state.unmanaged == [] and state.input_problems == []

    items = NotificationService(db).collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        controllers=[{**state.as_dict(), "id": controller_row}],
    )
    assert [(i["title"], i["severity"]) for i in items] == [("Pulsante Senza Permesso di Apertura", "crit")]
    assert items[0]["text"] == "Quadro Teatro · Pulsante Portineria"

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


def _casing_fault(service, now, casing_open=True, since="2026-10-29T21:55:17+00:00", unreachable=False):
    return service.collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={"ok": True},
        door_names={},
        primary_id=1,
        controllers=[
            {
                "id": 1,
                "name": "Rack Portineria",
                "online": not unreachable,
                "unreachable": unreachable,
                "casing_open": casing_open,
                "casing_since": since,
            }
        ],
        now=now,
    )


def test_open_casing_is_a_fault_while_it_stays_open(db):
    from datetime import UTC, datetime

    service = NotificationService(db, settle=timedelta(0))
    # Collaudo 30/10: coperchio aperto dall'avvio del 29/10, in Home Assistant acceso ma nell'app da nessuna parte
    now = datetime(2026, 10, 30, 22, 30, tzinfo=UTC)
    items = _casing_fault(service, now)
    assert [(i["type"], i["title"], i["severity"]) for i in items] == [
        ("casing", "Coperchio del Controller Aperto", "crit")
    ]
    fault = items[0]
    assert fault["id"] == "casing:1@2026-10-29T21:55:17+00:00" and fault["ts"] == "2026-10-29T21:55:17+00:00"
    assert fault["fault"] is True and fault["door"] == "Rack Portineria" and fault["link"] == "#impianti/c1/stato"
    # Oltre le 24 ore degli allarmi resta, e già visto torna da gestire ogni 4 ore come i guasti che durano
    service.mark_seen([fault["id"]], items)
    assert service.listing(_casing_fault(service, now + timedelta(hours=1)))["open"] == 0
    assert service.listing(_casing_fault(service, now + timedelta(hours=4)))["open"] == 1
    # Chiuso, sconosciuto o controller che non risponde: niente guasto
    assert _casing_fault(service, now, casing_open=False) == []
    assert _casing_fault(service, now, casing_open=None) == []
    assert [i["type"] for i in _casing_fault(service, now, unreachable=True)] == ["controller"]


def test_casing_opened_again_is_a_new_fault(db):
    from datetime import UTC, datetime

    service = NotificationService(db, settle=timedelta(0))
    now = datetime(2026, 10, 30, 22, 30, tzinfo=UTC)
    items = _casing_fault(service, now)
    service.mark_seen([items[0]["id"]], items)
    # Chiuso e riaperto: un'altra apertura, da gestire anche se la precedente era stata vista
    service.listing(_casing_fault(service, now, casing_open=False))
    again = _casing_fault(service, now + timedelta(minutes=5), since="2026-10-30T22:34:00+00:00")
    assert service.listing(again)["open"] == 1
    # Il tipo si può spegnere da solo, senza spegnere gli allarmi delle porte
    service.set_levels({"casing": "off"}, "admin")
    assert _casing_fault(service, now) == []


def test_open_casing_with_its_alarm_is_one_notification(db):
    # Collaudo 31/10: "Coperchio del Controller Aperto" e "Manomissione · Coperchio del controller aperto", stessa ora
    service = NotificationService(db, settle=timedelta(0))
    now = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)
    since = "2026-10-31T10:08:12+00:00"
    db.add_event(ts=since, kind="manomissione", detail="coperchio del controller aperto", source="Rack Portineria")
    items = _casing_fault(service, now, since=since)
    assert [(i["type"], i["title"], i["text"], i["door"]) for i in items] == [
        ("door_alarm", "Coperchio del Controller Aperto", "", "Rack Portineria")
    ]
    [open_item] = items
    # Finché resta aperto è un guasto: dopo la presa visione resta nella barra e torna da gestire ogni 4 ore
    assert open_item["fault"] is True and open_item["severity"] == "crit"
    service.mark_seen([open_item["id"]], items)
    assert service.listing(_casing_fault(service, now + timedelta(hours=1), since=since))["open"] == 0
    assert service.listing(_casing_fault(service, now + timedelta(hours=4), since=since))["open"] == 1
    # Richiuso: la stessa voce, già vista, resta nelle 24 ore degli allarmi
    closed = _casing_fault(service, now + timedelta(hours=5), casing_open=False, since="2026-10-31T15:00:00+00:00")
    [closed_item] = closed
    assert (closed_item["id"], closed_item["text"], closed_item["fault"]) == (open_item["id"], "Richiuso", False)
    assert service.listing(closed)["open"] == 0
    # Oltre le 24 ore dell'allarme, ancora aperto: torna il guasto dello stato
    later = _casing_fault(service, now + timedelta(hours=25), since=since)
    assert [(i["type"], i["id"]) for i in later] == [("casing", f"casing:1@{since}")]
    # Allarmi delle porte spenti: resta il guasto dello stato
    service.set_levels({"door_alarm": "off"}, "admin")
    assert [i["type"] for i in _casing_fault(service, now, since=since)] == ["casing"]


def test_casing_opened_before_and_again_now(db):
    service = NotificationService(db, settle=timedelta(0))
    now = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)
    db.add_event(
        ts="2026-10-31T06:00:00+00:00",
        kind="manomissione",
        detail="coperchio del controller aperto",
        source="Rack Portineria",
    )
    db.add_event(
        ts="2026-10-31T10:08:12+00:00",
        kind="manomissione",
        detail="coperchio del controller aperto",
        source="Rack Portineria",
    )
    items = _casing_fault(service, now, since="2026-10-31T10:08:12+00:00")
    # Quella di adesso è il guasto. L'apertura delle 6 non dice "Richiuso": con il coperchio ancora aperto può essere
    # la stessa, ripetuta dal controller al riavvio (il registro la scrive a ogni avvio)
    assert [(i["text"], i["fault"]) for i in items] == [("", True), ("", False)]
    # Chiuso adesso: tutte e due richiuse
    closed = _casing_fault(service, now, casing_open=False, since="2026-10-31T10:20:00+00:00")
    assert [(i["text"], i["fault"]) for i in closed] == [("Richiuso", False), ("Richiuso", False)]
    # Controller che non risponde: non si sa se è ancora aperto, niente guasto e niente "Richiuso"
    unknown = _casing_fault(service, now, unreachable=True, since="2026-10-31T10:08:12+00:00")
    assert [(i["type"], i["text"]) for i in unknown if i["type"] == "door_alarm"] == [
        ("door_alarm", ""),
        ("door_alarm", ""),
    ]


def test_controller_and_door_tamper_titles(db):
    service = NotificationService(db, settle=timedelta(0))
    now = datetime(2026, 10, 31, 10, 30, tzinfo=UTC)
    db.add_event(
        ts="2026-10-31T10:00:00+00:00", kind="manomissione", detail="controller inclinato", source="Rack Portineria"
    )
    db.add_event(
        ts="2026-10-31T10:01:00+00:00", kind="manomissione", detail="manomissione porta", source="porta", door="porta"
    )
    db.add_event(ts="2026-10-31T10:02:00+00:00", kind="allarme", detail="porta forzata", source="porta", door="porta")
    items = _casing_fault(service, now, casing_open=False)
    assert sorted((i["title"], i["text"]) for i in items) == [
        ("Allarme Porta", "Porta forzata"),
        ("Controller Inclinato", ""),
        ("Manomissione", ""),
    ]


async def test_cover_opened_and_closed_again_is_still_one_notification(db, controller, settings):
    # Revisione 0.24.1: aperto, chiuso e riaperto entro 5 minuti. La riga del registro diventa
    # "coperchio del controller aperto · ripetuta" e la notifica tornava doppia, con il titolo "Manomissione"
    from app.events import Event, EventMonitor
    from app.service import AccessService

    db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "Rack Portineria", "verify_ssl": 0})
    monitor = EventMonitor(prefix="x", door_token="porta")
    access = AccessService(db, controller, settings, monitor)
    start = datetime(2026, 10, 31, 10, 8, 12, tzinfo=UTC)
    for seconds, value in ((0, "1"), (60, "0"), (120, "1")):
        event = Event(
            "Device", "Device/Casing/Open/$source/Name/Casing", start + timedelta(seconds=seconds), {"Open": value}
        )
        monitor.apply_event(event)
        access.record_event(event)
    assert [e["detail"] for e in db.list_events()] == ["coperchio del controller aperto · ripetuta"]
    service = NotificationService(db, settle=timedelta(0))
    items = _casing_fault(service, start + timedelta(minutes=10), since=monitor.casing_since)
    assert [(i["type"], i["title"], i["fault"]) for i in items] == [
        ("door_alarm", "Coperchio del Controller Aperto", True)
    ]
    # Anche il controller inclinato più volte di seguito ha il suo titolo
    for seconds in (0, 40):
        access.record_event(Event("Device", "Device/Tampering/TiltDetected", start + timedelta(seconds=seconds), {}))
    titles = [i["title"] for i in _casing_fault(service, start + timedelta(minutes=10), since=monitor.casing_since)]
    assert sorted(titles) == ["Controller Inclinato", "Coperchio del Controller Aperto"]
