"""Pulsanti Bloccati (0.28.10): un Pulsante Uscita o Relè Citofono su un AUX premuto da più di un minuto è una
notifica, finché non si libera. Il 18/11 il Pulsante Portineria (AUX 1) è rimasto incastrato per ore e la porta non si
apriva più con lui. Il tempo si misura con l'ora dell'app, non con quella del controller."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.events import Event, EventMonitor, StateSnapshot
from app.main import create_app
from app.notifications import FAULT_TYPES, STUCK_AFTER, TYPES, VISIBILITY, NotificationService, _duration

PREFIX = "accessi/prova"
AUX1 = f"{PREFIX}/eventi/Device/IO/Port/$source/port/0"
# Ora dell'app nei test, al secondo
T0 = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)


def payload(state: str, millis: int) -> bytes:
    return json.dumps({"timestamp": millis, "message": {"data": {"state": state}}}).encode()


class Clock:
    def __init__(self, at: datetime = T0):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


# Monitor


def test_press_time_is_the_app_time_of_the_last_change():
    clock = Clock()
    monitor = EventMonitor(prefix=PREFIX, door_token="porta", clock=clock)
    # Orologio del controller indietro di due anni: non conta
    controller_ms = iter(range(1729800000000, 1729800100000, 100))
    monitor.handle(AUX1, payload("1", next(controller_ms)))
    assert monitor.io_since == {"0": "2026-11-18T09:00:00+00:00"}
    clock.at = T0 + timedelta(seconds=5)
    monitor.handle(AUX1, payload("0", next(controller_ms)))
    assert monitor.input_active("0", pushbutton=True) is True
    assert monitor.io_since["0"] == "2026-11-18T09:00:05+00:00"
    # Lo stesso valore di nuovo (un doppione, lo stato iniziale del WebSocket) non sposta l'ora della pressione
    clock.at = T0 + timedelta(minutes=10)
    monitor.handle(AUX1, payload("0", next(controller_ms)))
    monitor.apply_event(
        Event("Device", "Device/IO/Port/$source/port/0/", T0 - timedelta(days=700), {"port": "0", "state": "0"}),
        retained=True,
    )
    assert monitor.io_since["0"] == "2026-11-18T09:00:05+00:00"
    # Rilasciato: nuova ora
    monitor.handle(AUX1, payload("1", next(controller_ms)))
    assert monitor.io_since["0"] == "2026-11-18T09:10:00+00:00"
    # Controller riavviato (offline sul broker): gli ingressi tornano sconosciuti, l'ora con loro
    monitor.handle(f"{PREFIX}/stato", b"offline")
    assert monitor.io_since == {}


def test_press_time_from_the_controller_read_after_an_add_on_restart():
    # Il caso del 18/11 dopo la 0.28.9: ultimo evento 0 (premuto), mai cambiato dall'avvio dell'add-on. Premuto lo
    # dice solo la lettura del controller, con lo stesso valore: l'ora parte da lì
    clock = Clock()
    monitor = EventMonitor(prefix=PREFIX, door_token="porta", clock=clock)
    monitor.handle(AUX1, payload("0", 1729800000000))
    assert monitor.input_active("0", pushbutton=True) is False
    clock.at = T0 + timedelta(seconds=40)
    assert monitor.apply_snapshot(StateSnapshot(at=T0.isoformat(), io={"0": False}), monitor.seq) is True
    assert monitor.input_active("0", pushbutton=True) is True
    assert monitor.io_since["0"] == "2026-11-18T09:00:40+00:00"
    # Le letture dopo, uguali, non la spostano
    clock.at = T0 + timedelta(minutes=5)
    assert monitor.apply_snapshot(StateSnapshot(at=T0.isoformat(), io={"0": False}), monitor.seq) is False
    assert monitor.io_since["0"] == "2026-11-18T09:00:40+00:00"
    # Liberato tra due letture, senza eventi: nuova ora
    assert monitor.apply_snapshot(StateSnapshot(at=T0.isoformat(), io={"0": True}), monitor.seq) is True
    assert monitor.io_since["0"] == "2026-11-18T09:05:00+00:00"
    # Controller che non risponde: tutto sconosciuto
    monitor.forget_state()
    assert monitor.io_since == {}


# Notifica


def pressed(since: datetime = T0, role: str = "rex") -> list[dict]:
    return [
        {
            "controller_id": 1,
            "io": "IO1",
            "name": "Pulsante Prova",
            "role": role,
            "door": "Porta Prova",
            "door_token": "porta",
            "since": since.isoformat(timespec="seconds"),
        }
    ]


def collect(service: NotificationService, buttons: list[dict], now: datetime) -> list[dict]:
    items = service.collect(
        mqtt_configured=True,
        mqtt_connected=True,
        controller_configured=True,
        controller_online=True,
        sync={},
        door_names={"porta": "Porta Prova"},
        pressed=buttons,
        now=now,
    )
    return [i for i in items if i["type"] == "stuck_input"]


def test_stuck_after_a_minute_with_the_input_and_door_names(db):
    service = NotificationService(db)
    assert collect(service, pressed(), T0 + timedelta(seconds=59)) == []
    assert collect(service, pressed(), T0 + STUCK_AFTER) == []
    [item] = collect(service, pressed(), T0 + timedelta(seconds=61))
    assert (item["title"], item["text"]) == ("Pulsante Bloccato", "Pulsante Prova · Porta Prova · premuto da 1 min")
    assert (item["severity"], item["fault"], item["ts"]) == ("warn", True, "2026-11-18T09:00:00+00:00")
    assert item["link"] == "#impianti/c1" and item["door_token"] == "porta" and item["door"] == ""
    # La durata cresce, la notifica resta la stessa; vista, torna da gestire ogni 4 ore finché dura
    [later] = collect(service, pressed(), T0 + timedelta(hours=1, minutes=5))
    assert later["text"].endswith("premuto da 1 h 5 min") and later["id"] == item["id"]
    assert (item["weight"], later["weight"]) == (0, 0)
    [hours] = collect(service, pressed(), T0 + timedelta(hours=4, minutes=1))
    assert hours["weight"] == 1 and hours["id"] == item["id"]
    [relay] = collect(service, pressed(role="intercom"), T0 + timedelta(minutes=2))
    assert relay["title"] == "Relè del Citofono Bloccato" and relay["text"].endswith("premuto da 2 min")
    # Rilasciato e premuto di nuovo: un'altra notifica, da gestire anche se la precedente era stata vista
    [again] = collect(service, pressed(T0 + timedelta(hours=2)), T0 + timedelta(hours=3))
    assert again["id"] != item["id"]
    service.set_levels({"stuck_input": "off"}, "prova")
    assert collect(service, pressed(), T0 + timedelta(minutes=2)) == []


def test_duration_text():
    assert _duration(timedelta(seconds=61)) == "1 min"
    assert _duration(timedelta(minutes=59, seconds=59)) == "59 min"
    assert _duration(timedelta(hours=1)) == "1 h"
    assert _duration(timedelta(hours=26, minutes=3)) == "26 h 3 min"


def test_stuck_button_type_and_who_sees_it(db):
    assert TYPES["stuck_input"] == (
        "Pulsanti Bloccati",
        "Pulsante Uscita o Relè Citofono premuto da più di un minuto.",
        "urgent",
    )
    assert "stuck_input" in FAULT_TYPES and "stuck_input" not in VISIBILITY
    service = NotificationService(db)
    items = collect(service, pressed(), T0 + timedelta(minutes=3))
    # Chi vede solo lo stato delle porte, anche dall'accesso diretto senza permessi tecnici: la vede com'è
    listing = service.listing(items, {"stato"}, plain=True)
    [shown] = listing["items"]
    assert (shown["title"], shown["text"]) == (items[0]["title"], items[0]["text"])
    assert {"key": "stuck_input", "level": "urgent"}.items() <= next(
        t for t in listing["types"] if t["key"] == "stuck_input"
    ).items()


# Dall'app, con il controller finto


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "pulsante.db")
    with TestClient(app) as test_client:
        yield test_client


def read(client, controller, pressed_now: bool, responding: bool | None = True) -> None:
    """Stato degli ingressi letto dal controller finto. AUX 1 come Pulsante Uscita: a riposo closed (normalState
    open), premuto open."""
    state = client.app.state
    controller.ports[0]["state"] = "open" if pressed_now else "closed"
    client.portal.call(state.state_keeper.tick, state.controller, ["porta"], responding)


def stuck(client, now: datetime) -> list[dict]:
    return [i for i in main._current_notifications(client.app.state, now) if i["type"] == "stuck_input"]


def test_stuck_exit_button_through_the_app(client, controller):
    state = client.app.state
    client.portal.call(state.health.check_all)
    primary = state.installation.primary["id"]
    door = next(d["name"] for d in state.doors if d["door_token"] == "porta")
    values = {"name": "Pulsante Prova", "role": "rex", "door_token": "porta"}
    state.db.save_controller_input(primary, "IO1", values)
    clock = Clock(datetime.now(tz=UTC).replace(microsecond=0))
    state.monitor.clock = clock
    read(client, controller, pressed_now=False)
    assert stuck(client, clock.at + timedelta(hours=1)) == []
    read(client, controller, pressed_now=True)
    assert stuck(client, clock.at + timedelta(seconds=59)) == []
    [item] = stuck(client, clock.at + timedelta(seconds=61))
    assert (item["title"], item["text"]) == ("Pulsante Bloccato", f"Pulsante Prova · {door} · premuto da 1 min")
    assert item["link"] == f"#impianti/c{primary}"

    # Dalle API, con l'ora vera: nella pagina, tra i guasti della barra in alto e nelle impostazioni
    clock.at -= timedelta(minutes=2)
    state.monitor.io_since["0"] = clock.at.isoformat()
    listing = client.get("/api/notifications").json()
    [shown] = [i for i in listing["items"] if i["type"] == "stuck_input"]
    assert (shown["title"], shown["text"]) == ("Pulsante Bloccato", f"Pulsante Prova · {door} · premuto da 2 min")
    assert "Pulsante Bloccato" in [i["title"] for i in client.get("/api/summary").json()["faults"]]
    kind = next(t for t in listing["types"] if t["key"] == "stuck_input")
    assert (kind["name"], kind["level"]) == ("Pulsanti Bloccati", "urgent")

    # Una porta modificata ricrea il monitor: l'ora della pressione resta, e con lei la notifica
    row = state.db.list_doors()[0]
    client.put(f"/api/doors/{row['id']}", json={**row, "snapshot_camera": "camera.x"})
    state = client.app.state
    assert [i["id"] for i in stuck(client, datetime.now(tz=UTC))] == [shown["id"]]

    # Controller che non risponde, o non ancora letto: lo stato non si sa, nessuna notifica
    state.monitor.reachable = None
    assert stuck(client, datetime.now(tz=UTC) + timedelta(hours=1)) == []
    state.monitor.reachable = True
    read(client, controller, pressed_now=True, responding=False)
    assert stuck(client, datetime.now(tz=UTC) + timedelta(hours=1)) == []

    # Liberato: niente
    read(client, controller, pressed_now=False)
    assert state.monitor.reachable is True
    assert stuck(client, datetime.now(tz=UTC) + timedelta(hours=1)) == []
    assert all(f["type"] != "stuck_input" for f in client.get("/api/summary").json()["faults"])


def test_rex_terminal_and_other_roles_are_never_stuck(client, controller):
    state = client.app.state
    client.portal.call(state.health.check_all)
    primary = state.installation.primary["id"]
    # L'ingresso REX non manda Device/IO/Port: non si sa se è premuto. Un AUX come Ingresso Generico non è un pulsante
    state.db.save_controller_input(primary, "REX", {"name": "Pulsante Prova", "role": "rex", "door_token": "porta"})
    state.db.save_controller_input(primary, "IO1", {"name": "Sensore Prova", "role": "alarm"})
    state.db.save_controller_input(primary, "IO2", {"name": "Pulsante Altro", "role": ""})
    for port in controller.ports:
        port["state"] = "open"
    read(client, controller, pressed_now=True)
    assert all(state.monitor.input_active(str(n), pushbutton=True) for n in range(2))
    assert stuck(client, datetime.now(tz=UTC) + timedelta(hours=1)) == []
