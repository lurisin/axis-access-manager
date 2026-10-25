import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.installation import InstallationService
from app.main import create_app
from app.programming import ProgrammingService, format_duration, parse_duration
from app.service import ServiceError

LEGACY = {
    "a1210_host": "https://172.27.0.1/",
    "a1210_username": "root",
    "a1210_password": "segreta",
    "door_token": "porta",
    "access_profile": "profilo_sempre",
    "mqtt_topic_prefix": "accessi/x",
    "snapshot_camera": "camera.ingresso",
    "intercom_name": "Citofono",
    "intercom_open_door": True,
}


def test_legacy_options_are_imported_once(db):
    installation = InstallationService(db)
    assert installation.migrate_from_options(parse_options(LEGACY)) is True
    assert installation.migrate_from_options(parse_options(LEGACY)) is False

    [controller] = installation.controllers()
    assert controller["host"] == "172.27.0.1" and controller["has_password"] is True
    assert "password" not in controller
    [door] = controller["doors"]
    assert door["door_token"] == "porta" and door["snapshot_camera"] == "camera.ingresso"
    assert door["intercom_name"] == "Citofono" and door["intercom_open_door"] is True
    assert db.get_setting("default_group") == "profilo_sempre"

    effective = installation.effective_settings(parse_options({"event_retention_days": 30}))
    assert effective.a1210_password == "segreta" and effective.door_token == "porta"
    assert effective.mqtt_topic_prefix == "accessi/x" and effective.event_retention_days == 30


def test_controller_and_door_validation(db):
    installation = InstallationService(db)
    data = {"name": "A1210", "host": "10.0.0.5", "username": "root", "password": "x", "mqtt_topic_prefix": "accessi/y"}
    controller_id = installation.save_controller(None, data, "admin")["id"]
    with pytest.raises(ServiceError, match="un solo controller"):
        installation.save_controller(None, data, "admin")
    # Password vuota = resta quella salvata
    installation.save_controller(controller_id, {**data, "password": "", "name": "Scala A"}, "admin")
    assert db.get_controller(controller_id)["password"] == "x"
    with pytest.raises(ServiceError, match="prefisso MQTT"):
        installation.save_controller(controller_id, {**data, "mqtt_topic_prefix": "a b"}, "admin")

    door = {"door_token": "porta", "name": "Ingresso", "snapshot_camera": "camera.x", "intercom_name": ""}
    door_id = installation.save_door(None, controller_id, door, "admin")["id"]
    with pytest.raises(ServiceError, match="già configurata come Ingresso"):
        installation.save_door(None, controller_id, {**door, "name": "Doppia"}, "admin")
    with pytest.raises(ServiceError, match="camera"):
        installation.save_door(door_id, controller_id, {**door, "snapshot_camera": "sensor.x"}, "admin")
    # Senza nome del citofono l'inoltro dell'apertura resta spento
    installation.save_door(door_id, controller_id, {**door, "intercom_open_door": True}, "admin")
    assert db.get_door(door_id)["intercom_open_door"] is False
    installation.delete_controller(controller_id, "admin")
    assert db.list_doors() == []


def test_durations():
    assert parse_duration("PT7S") == 7
    assert parse_duration("PT1M30S") == 90
    assert parse_duration("PT0.5S") == 0.5
    assert parse_duration("bad") is None
    assert format_duration(90) == "PT90S"


async def test_door_times_already_over_the_limit_do_not_block_saving(db, controller):
    """Un valore già sul controller oltre il massimo, se non cambia, non blocca le altre modifiche."""
    service = ProgrammingService(db, controller, 1, asyncio.Lock())
    controller.doors["porta"]["ExtendedAccessTime"] = "PT2M"
    await service.update_door("porta", {"name": "Portone", "access_time": 8, "extended_access_time": 120}, "admin")
    assert controller.doors["porta"]["AccessTime"] == "PT8S"
    with pytest.raises(ServiceError, match="Apertura Estesa: tra 1 e 60"):
        await service.update_door("porta", {"name": "Portone", "extended_access_time": 90}, "admin")


async def test_controller_inputs_get_name_role_and_door(db, controller):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())
    saved = await service.update_input(
        "IO1", {"name": "", "role": "intercom", "door": "porta", "supervised": True}, "admin"
    )
    assert saved["name"] == "Relè Citofono" and saved["supervised"] is True
    inputs = {i["io"]: i for i in (await service.read())["inputs"]}
    assert inputs["IO1"]["role"] == "intercom" and inputs["IO1"]["door"] == "porta"
    assert inputs["IO1"]["name"] == "Relè Citofono" and inputs["IO1"]["default_name"] == "Ingresso AUX 1"

    with pytest.raises(ServiceError, match="scegli la porta"):
        await service.update_input("REX", {"role": "rex"}, "admin")
    with pytest.raises(ServiceError, match="porta non trovata"):
        await service.update_input("REX", {"role": "rex", "door": "manca"}, "admin")
    with pytest.raises(ServiceError, match="non disponibile"):
        await service.update_input("ALARM", {"role": "rex", "door": "porta"}, "admin")
    with pytest.raises(ServiceError, match="non trovato"):
        await service.update_input("XX", {"role": ""}, "admin")
    # Un ingresso di allarme non ha porta
    saved = await service.update_input("ALARM", {"name": "Allagamento", "role": "alarm", "door": "porta"}, "admin")
    assert saved["door_token"] == "" and saved["name"] == "Allagamento"


async def test_rex_input_is_written_to_the_controller_and_can_be_undone(db, controller):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())

    await service.update_input("REX", {"name": "Pulsante Portineria", "role": "rex", "door": "porta"}, "admin")
    row = db.controller_inputs(controller_id)["REX"]
    idpoint, point = row["idpoint_token"], row["access_point_token"]
    assert controller.idpoints[idpoint]["Name"] == "Pulsante Portineria"
    assert controller.idpoint_configs[idpoint]["IdPoint.REX.Type"] == "ActiveLow"
    assert controller.idpoint_configs[idpoint]["IdPoint.Reader.Type"] == "None"
    assert controller.io_assignments["REX"]["IoMode"] == "in:pu/gnd"
    assert controller.io_assignments["REX"]["IoUser"][0]["Usage"] == "REX"
    ap = next(a for a in controller.access_points if a["token"] == point)
    assert ap["Entity"] == "porta" and ap["AuthenticationProfile"] == ["REXOnly"]
    assert {"Name": "Direction", "Value": "out"} in ap["Attribute"]
    assert ap["IdPointDevice"][0]["IdPoint"] == idpoint

    # Relè del citofono su AUX, linea supervisionata e contatto NC: tipo e uso supervisionati
    await service.update_input(
        "IO1", {"role": "intercom", "door": "porta", "supervised": True, "contact": "nc"}, "admin"
    )
    relay = db.controller_inputs(controller_id)["IO1"]
    assert controller.idpoint_configs[relay["idpoint_token"]]["IdPoint.REX.Type"] == "SupervisedActiveHigh"
    assert controller.io_assignments["IO1"] == {
        "IoMode": "supervised",
        "IoUser": [{"Type": "idpoint", "token": relay["idpoint_token"], "Usage": "REXSupervised", "MultiIo": False}],
    }

    # Lo schema dei lettori non vede pulsante e relè: salvarlo non li tocca
    data = await service.read()
    assert idpoint not in {r["token"] for r in data["readers"]}
    assert point not in {a["token"] for a in data["access_points"]}
    await service.set_links([{"reader": "lettore-1", "door": "porta", "direction": "in"}], "admin")
    ap = next(a for a in controller.access_points if a["token"] == point)
    assert ap["IdPointDevice"][0]["IdPoint"] == idpoint

    # Relè tolto da supervisionato: il firmware ignora il cambio di modo senza utenti, si passa dal REX normale
    await service.update_input("IO1", {"role": ""}, "admin")
    assert controller.io_assignments["IO1"] == {"IoMode": "inout:pu/gnd", "IoUser": []}
    assert relay["idpoint_token"] not in controller.idpoint_configs

    # Ruolo tolto: punto di accesso e punto di identificazione rimossi, ingresso liberato in un modo innocuo
    await service.update_input("REX", {"role": ""}, "admin")
    assert idpoint not in controller.idpoints and all(a["token"] != point for a in controller.access_points)
    assert controller.io_assignments["REX"] == {"IoMode": "in:pu/gnd", "IoUser": []}
    assert db.controller_inputs(controller_id)["REX"]["idpoint_token"] == ""

    # Annulla dallo Storico Modifiche: torna il pulsante
    change = next(c for c in db.list_controller_changes(controller_id) if c["kind"] == "input" and c["token"] == "REX")
    await service.undo(change["id"], "admin")
    row = db.controller_inputs(controller_id)["REX"]
    assert row["role"] == "rex" and row["idpoint_token"] in controller.idpoints

    # Il controller rifiuta l'assegnazione: errore chiaro, i token creati restano per il prossimo tentativo
    controller.fail_io_assignment = True
    with pytest.raises(ServiceError, match="rifiutato la configurazione"):
        await service.update_input("IO2", {"role": "rex", "door": "porta"}, "admin")
    assert db.controller_inputs(controller_id)["IO2"]["idpoint_token"].startswith("idpoint-")
    assert db.controller_inputs(controller_id)["IO2"]["role"] == ""


async def test_programming_changes_can_be_undone(db, controller):
    service = ProgrammingService(db, controller, 1, asyncio.Lock())
    data = await service.read()
    doors = {d["token"]: d for d in data["doors"]}
    assert doors["porta"]["access_time"] == 7 and doors["porta"]["extended_open_too_long_time"] == 60
    assert doors["porta"]["configuration"] == [{"name": "Lock.Type", "value": "FailSecure"}]
    [point] = data["access_points"]
    assert point["reader_names"] == ["Lettore esterno"] and point["direction"] == "in"
    assert data["errors"] == ["configurazione lettori: non supportato"]
    inputs = {i["io"]: i for i in data["inputs"]}
    assert list(inputs) == ["DPS", "REX", "IO1", "ALARM"]
    assert inputs["REX"]["pins"] == ["Door 1:3+4"] and inputs["REX"]["can_supervise"] is True
    assert inputs["REX"]["assigned"] == [] and inputs["IO1"]["port"] == "0"
    assert inputs["ALARM"]["name"] == "Ingresso Allarme" and inputs["ALARM"]["can_supervise"] is False
    assert inputs["REX"]["roles"] == ["", "rex", "intercom"] and inputs["REX"]["role"] == ""

    await service.update_door("porta", {"name": "Portone", "access_time": 10}, "admin")
    assert controller.doors["porta"]["AccessTime"] == "PT10S"
    assert controller.doors["porta"]["Name"] == "Portone"
    assert controller.doors["porta"]["OpenTooLongTime"] == "PT15S"
    with pytest.raises(ServiceError, match="Tempo di Apertura: tra 1 e 30"):
        await service.update_door("porta", {"name": "Portone", "access_time": 0}, "admin")
    with pytest.raises(ServiceError, match="Tempo di Apertura: tra 1 e 30"):
        await service.update_door("porta", {"name": "Portone", "access_time": 31}, "admin")

    await service.update_reader("lettore-2", {"name": "Lettore uscita"}, "admin")
    await service.update_access_point("ap-in", {"readers": ["lettore-2"], "direction": "out", "door": "porta"}, "admin")
    point = controller.access_points[0]
    assert point["IdPointDevice"] == [{"IdPoint": "lettore-2", "DeviceUUID": ""}]
    assert point["Attribute"] == [{"Name": "Direction", "Value": "out"}]
    with pytest.raises(ServiceError, match="lettore non trovato"):
        await service.update_access_point("ap-in", {"readers": ["manca"]}, "admin")

    changes = db.list_controller_changes(1)
    assert [c["kind"] for c in changes] == ["access_point", "reader", "door"]
    await service.undo(changes[0]["id"], "admin")
    assert controller.access_points[0]["IdPointDevice"] == [{"IdPoint": "lettore-1", "DeviceUUID": ""}]
    with pytest.raises(ServiceError, match="già annullata"):
        await service.undo(changes[0]["id"], "admin")
    # Nessun comando alla serratura
    assert not any(c[0] == "access_door" for c in controller.calls)


def test_installation_api_reloads_runtime(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(parse_options({}), allow_any_client=True, controller=controller, db_path=tmp_path / "i.db")
    with TestClient(app) as client:
        assert client.get("/api/summary").json()["configured"] is False
        data = {"name": "A1210", "host": "10.0.0.5", "username": "root", "password": "x", "mqtt_topic_prefix": "acc"}
        assert client.post("/api/controllers/test", json=data).json()["doors"][1] == {
            "token": "porta2",
            "name": "Cantina",
        }
        controller_id = client.post("/api/controllers", json=data).json()["id"]
        assert client.get("/api/summary").json()["configured"] is True
        assert client.get(f"/api/controllers/{controller_id}/doors-available").status_code == 200
        door = {"door_token": "porta2", "name": "Cantina", "intercom_name": "Citofono", "intercom_open_door": True}
        assert client.post(f"/api/controllers/{controller_id}/doors", json=door).status_code == 200
        status = client.get("/api/status").json()
        assert status["doors"][0]["name"] == "Cantina"
        assert status["intercoms"][0]["door"] == "Cantina"
        assert "Apri Porta" in status["home_assistant"]["entities"]
        installation = client.get("/api/installation").json()
        assert installation["controllers"][0]["has_password"] is True
        assert "password" not in installation["controllers"][0]

        response = client.put("/api/programming/doors/porta2", json={"name": "Cantina B", "access_time": 4})
        assert response.status_code == 200
        programming = client.get("/api/programming").json()
        assert programming["changes"][0]["summary"] == "porta Cantina B"
        assert client.post(f"/api/programming/changes/{programming['changes'][0]['id']}/undo").status_code == 200
        assert controller.doors["porta2"]["Name"] == "Cantina"

        # Senza il permesso impianti non si vede niente
        client.get("/api/me", headers={"X-Remote-User-Name": "admin"})
        assert client.get("/api/installation", headers={"X-Remote-User-Name": "altro"}).status_code == 403
