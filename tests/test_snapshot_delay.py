"""Ritardo della foto della telecamera della porta (0.28.7). Il video della telecamera arriva 2-3 s dopo la realtà:
la foto presa subito mostra chi passa ancora lontano dal lettore. Con il ritardo la foto si chiede quei secondi dopo
l'evento; l'evento va subito a Home Assistant. Solo FakeController e telecamera finta."""

import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from test_main import FakeCamera
from test_page_time_zone import run_page

from app.config import parse_options
from app.db import Database
from app.installation import snapshot_delay
from app.main import create_app
from app.service import ServiceError


class TimedCamera(FakeCamera):
    """Telecamera finta che segna quando le è stata chiesta ogni foto."""

    def __init__(self, snapshot_dir):
        super().__init__(snapshot_dir)
        self.at = []

    async def save_snapshot(self, camera, name):
        self.at.append(time.monotonic())
        return await super().save_snapshot(camera, name)


def make_app(controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    settings = parse_options(
        {"a1210_host": "a", "door_token": "porta", "access_profile": "profilo_sempre", "snapshot_camera": "camera.x"}
    )
    ha = TimedCamera(tmp_path / "shots")
    app = create_app(
        settings, allow_any_client=True, controller=controller, db_path=tmp_path / "s.db", home_assistant=ha
    )
    return app, ha


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("2,5", 2.5), ("2.5", 2.5), (" 3 ", 3.0), (2.54, 2.5), (10, 10.0), ("0,1", 0.1), (0, 0.0), ("", 0.0), (None, 0.0)],
)
def test_the_delay_is_read_with_comma_or_point(value, seconds):
    assert snapshot_delay(value) == seconds


@pytest.mark.parametrize("value", [-1, "10,1", 11, "abc", "1,2,3", "2,5 s", True, float("nan")])
def test_a_wrong_delay_is_refused(value):
    with pytest.raises(ServiceError, match="da 0,1 a 10 secondi"):
        snapshot_delay(value)


def test_the_delay_is_saved_with_the_camera_of_the_door(controller, tmp_path, monkeypatch):
    app, _ = make_app(controller, tmp_path, monkeypatch)
    with TestClient(app) as client:
        door = app.state.db.list_doors()[0]
        assert door["snapshot_delay"] == 0.0
        assert client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_delay": "2,5"}).status_code == 200
        door = app.state.db.list_doors()[0]
        assert door["snapshot_delay"] == 2.5
        # Un altro salvataggio della porta (Impostazioni App) che manda tutti i campi lo tiene
        assert client.put(f"/api/doors/{door['id']}", json={**door, "name": "Ingresso"}).status_code == 200
        assert app.state.db.list_doors()[0]["snapshot_delay"] == 2.5
        assert client.get("/api/summary").json()["doors"][0]["snapshot_delay"] == 2.5
        # Fuori dai limiti: rifiutato, resta quello di prima
        response = client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_delay": 12})
        assert response.status_code == 400 and "da 0,1 a 10 secondi" in response.json()["detail"]
        assert app.state.db.list_doors()[0]["snapshot_delay"] == 2.5
        # Senza telecamera non c'è ritardo
        assert client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_camera": ""}).status_code == 200
        assert app.state.db.list_doors()[0]["snapshot_delay"] == 0.0


def test_the_photo_is_taken_after_the_delay_and_the_event_goes_out_at_once(controller, tmp_path, monkeypatch):
    import app.main as main_module

    waits = []
    real_wait = main_module._wait_for_photo

    async def wait(state, delay):
        waits.append(delay)
        return await real_wait(state, delay)

    monkeypatch.setattr(main_module, "_wait_for_photo", wait)
    app, ha = make_app(controller, tmp_path, monkeypatch)
    with TestClient(app) as client:
        door = app.state.db.list_doors()[0]
        client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_delay": 0.4})
        published = []
        original = app.state.bridge.publish_event

        async def publish(row):
            published.append(time.monotonic())
            await original(row)

        app.state.bridge.publish_event = publish

        def event(kind, door="porta"):
            row = {"ts": "2026-11-17T14:38:28Z", "kind": kind, "card": "04aa", "door": door}
            row["id"] = app.state.db.add_event(**row)
            client.portal.call(app.state.service.after_event, row)
            return row["id"]

        denied = event("negato")
        assert waits == [0.4] and len(ha.shots) == 1 and ha.at[0] - published[0] >= 0.35
        # La foto è dell'evento, come senza ritardo
        row = next(e for e in client.get("/api/events?limit=5").json() if e["id"] == denied)
        assert row["snapshot"] and row["snapshot_error"] == ""
        # Il coperchio del controller ha la sua telecamera: niente ritardo della porta
        ctrl = app.state.db.list_controllers()[0]
        assert client.put(f"/api/controllers/{ctrl['id']}/camera", json={"camera": "camera.quadro"}).status_code == 200
        event("manomissione", door="")
        assert ha.shots[-1][0] == "camera.quadro" and waits == [0.4]


def test_a_photo_waiting_for_its_delay_is_not_taken_at_shutdown(controller, tmp_path, monkeypatch):
    app, ha = make_app(controller, tmp_path, monkeypatch)
    with TestClient(app) as client:
        door = app.state.db.list_doors()[0]
        client.put(f"/api/doors/{door['id']}", json={**door, "snapshot_delay": 5})
        row = {"ts": "2026-11-17T14:38:28Z", "kind": "negato", "card": "04aa", "door": "porta"}
        row["id"] = app.state.db.add_event(**row)
        client.portal.start_task_soon(app.state.service.after_event, row)
        client.portal.call(asyncio.sleep, 0.1)
        assert len(app.state.photo_waits) == 1
    assert ha.shots == []


DOOR = {
    "door_token": "porta",
    "name": "Porta",
    "snapshot_camera": "camera.x",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": "",
    "intercom_open_door": 0,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}


def test_a_database_of_the_0_28_6_gets_the_delay_column_and_saves_doors(tmp_path):
    """Collaudo della 0.28.7 sull'impianto: un database esistente (già allo schema 10) non riceveva la colonna, che
    stava solo nelle colonne della migrazione 1, e ogni salvataggio di una porta dava «no such column: snapshot_delay»
    (errore 500). Il database della 0.28.6 si rifà togliendo la colonna: la versione dello schema resta quella."""
    path = tmp_path / "vecchio.db"
    db = Database(path)
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    door_id = db.save_door(None, controller_id, DOOR)
    version = db._conn.execute("PRAGMA user_version").fetchone()[0]
    db.close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE doors DROP COLUMN snapshot_delay")
        assert "snapshot_delay" not in {row[1] for row in conn.execute("PRAGMA table_info(doors)")}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == version

    db = Database(path)
    [door] = db.list_doors()
    assert door["snapshot_delay"] == 0.0 and door["snapshot_camera"] == "camera.x"
    # Salvataggio di una porta esistente (anche solo il videocitofono) e di una porta nuova
    db.save_door(door_id, controller_id, {**DOOR, "intercom_name": "Citofono", "snapshot_delay": 2.5})
    db.save_door(None, controller_id, {**DOOR, "door_token": "cantina", "name": "Cantina"})
    db.close()
    # Dopo un riavvio il ritardo resta
    db = Database(path)
    saved = {d["door_token"]: d for d in db.list_doors()}
    assert saved["porta"]["snapshot_delay"] == 2.5 and saved["porta"]["intercom_name"] == "Citofono"
    assert saved["cantina"]["snapshot_delay"] == 0.0
    db.close()


def test_the_page_reads_the_delay_with_the_comma():
    names = ["delayText", "parseDelay", "doorBody"]
    out = run_page(
        "Europe/Rome",
        {
            "parsed": '["2,5", "2.5", " 3 ", "0,1", "10", "0", "10,1", "", "abc", "2,5 s", "-1"].map(parseDelay)',
            "text": "[2.5, 3, 0.1, 10].map(delayText)",
            "body": 'doorBody({name: "Porta", door_token: "porta", snapshot_camera: "camera.x", snapshot_delay: 2.5})',
        },
        names=names,
    )
    assert out["parsed"] == [2.5, 2.5, 3, 0.1, 10, None, None, None, None, None, None]
    assert out["text"] == ["2,5 s", "3 s", "0,1 s", "10 s"]
    assert out["body"]["snapshot_delay"] == 2.5
