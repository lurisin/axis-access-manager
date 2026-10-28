import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import parse_options
from app.installation import InstallationService
from app.main import create_app
from app.programming import ProgrammingService, format_duration, is_rex_profile, parse_duration
from app.rules_service import RulesService
from app.service import AccessService, ServiceError

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
    # Gestito dal controller (senza, il controller vede il pulsante ma non decide); configurazione senza dispositivo
    assert controller.managed() == {"ap-in", point}
    assert controller.idpoint_configs[idpoint]["DeviceUUID"] == "uuid-locale"

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
    assert point not in controller.managed()
    assert controller.io_assignments["REX"] == {"IoMode": "in:pu/gnd", "IoUser": []}
    assert db.controller_inputs(controller_id)["REX"]["idpoint_token"] == ""

    # Annulla dallo Storico Modifiche: torna il pulsante
    change = next(c for c in db.list_controller_changes(controller_id) if c["kind"] == "input" and c["token"] == "REX")
    await service.undo(change["id"], "admin")
    row = db.controller_inputs(controller_id)["REX"]
    assert row["role"] == "rex" and row["idpoint_token"] in controller.idpoints

    # Il controller rifiuta l'assegnazione: errore chiaro, i token creati restano per il prossimo tentativo
    controller.fail_io_assignment = True
    with pytest.raises(ServiceError, match="configurazione dell'ingresso non completata"):
        await service.update_input("IO2", {"role": "rex", "door": "porta"}, "admin")
    assert db.controller_inputs(controller_id)["IO2"]["idpoint_token"].startswith("idpoint-")
    assert db.controller_inputs(controller_id)["IO2"]["role"] == ""


def rex_profiles(controller) -> list[dict]:
    return [p for p in controller.profiles.values() if is_rex_profile(p)]


async def test_rex_inputs_get_the_permission_to_open(db, controller):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())

    # Come nell'esempio Axis con REX: profilo con il modo REXOnly, fascia sempre e una regola sul punto del pulsante
    await service.update_input("REX", {"name": "Pulsante Portineria", "role": "rex", "door": "porta"}, "admin")
    point = db.controller_inputs(controller_id)["REX"]["access_point_token"]
    [profile] = rex_profiles(controller)
    assert profile["Name"] == "Uscite e Citofono" and profile["Enabled"] is True
    assert profile["Schedule"] == ["standard_always"] and profile["AuthenticationProfile"] == ["REXOnly"]
    assert profile["AccessPolicy"] == [
        {"AccessPoint": point, "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_always"]}
    ]
    assert (await service.read())["rex_missing"] == []

    # Il relè del citofono entra nello stesso profilo con una regola sua
    await service.update_input("IO1", {"name": "Citofono", "role": "intercom", "door": "porta"}, "admin")
    relay = db.controller_inputs(controller_id)["IO1"]["access_point_token"]
    [profile] = rex_profiles(controller)
    assert {p["AccessPoint"] for p in profile["AccessPolicy"]} == {point, relay}

    # Cambiare il nome non tocca il profilo
    controller.calls.clear()
    await service.update_input("IO1", {"name": "Citofono Ingresso", "role": "intercom", "door": "porta"}, "admin")
    assert not [c for c in controller.calls if c[0] == "set_access_profile"]

    # Altra porta: prima si toglie la regola che nomina il vecchio punto di accesso, poi il punto
    controller.calls.clear()
    await service.update_input("IO1", {"name": "Citofono Ingresso", "role": "intercom", "door": "porta2"}, "admin")
    moved = db.controller_inputs(controller_id)["IO1"]["access_point_token"]
    names = [c[0] for c in controller.calls]
    assert names.index("set_access_profile") < names.index("remove_access_point")
    [profile] = rex_profiles(controller)
    assert {p["AccessPoint"] for p in profile["AccessPolicy"]} == {point, moved}

    # Il profilo non è un gruppo di persone: non si vede tra i gruppi
    rules = RulesService(AccessService(db, controller, parse_options(LEGACY)))
    assert profile["token"] not in {g["token"] for g in (await rules.list_groups())["groups"]}

    # Senza più ingressi che aprono, il profilo sparisce
    await service.update_input("IO1", {"role": ""}, "admin")
    [profile] = rex_profiles(controller)
    assert [p["AccessPoint"] for p in profile["AccessPolicy"]] == [point]
    await service.update_input("REX", {"role": ""}, "admin")
    assert rex_profiles(controller) == []
    assert not any(c[0] == "access_door" for c in controller.calls)


async def test_inputs_from_before_get_the_permission_back(db, controller):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())
    await service.update_input("REX", {"name": "Pulsante Portineria", "role": "rex", "door": "porta"}, "admin")
    # Come un ingresso programmato fino alla 0.21.2: nessun profilo, il controller nega con InvalidAccessProfile
    controller.profiles.pop(rex_profiles(controller)[0]["token"])
    assert (await service.read())["rex_missing"] == [{"io": "REX", "name": "Pulsante Portineria"}]

    assert await service.repair_rex_profile() == 1
    assert (await service.read())["rex_missing"] == []
    assert db.list_audit()[0]["detail"] == "permesso di apertura senza tessera: Pulsante Portineria"
    # Già a posto: niente da scrivere
    controller.calls.clear()
    assert await service.repair_rex_profile() == 0
    assert not [c for c in controller.calls if c[0] == "set_access_profile"]

    # Un profilo disattivato dal sito del controller non dà il permesso: lo si riattiva
    [profile] = rex_profiles(controller)
    profile["Enabled"] = False
    assert (await service.read())["rex_missing"] == [{"io": "REX", "name": "Pulsante Portineria"}]
    assert await service.repair_rex_profile() == 1
    assert rex_profiles(controller)[0]["Enabled"] is True

    # Punto di accesso tolto dal controller: nessuna regola che lo nomini, nessun errore
    row = db.controller_inputs(controller_id)["REX"]
    controller.access_points = [a for a in controller.access_points if a["token"] != row["access_point_token"]]
    assert (await service.read())["rex_missing"] == []
    assert await service.repair_rex_profile() == 0
    assert rex_profiles(controller) == []

    # Senza controller configurato non c'è niente da riparare
    assert await ProgrammingService(db, controller, None, asyncio.Lock()).repair_rex_profile() == 0


async def test_door_contact_role_writes_the_door_monitor(db, controller):
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())
    with pytest.raises(ServiceError, match="linea supervisionata"):
        await service.update_input("DPS", {"role": "door_monitor", "door": "porta", "supervised": True}, "admin")

    # Come al banco: DoorMonitor Standard, contatto NA (porta chiusa = ingresso aperto), DPS ai due controlli
    await service.update_input("DPS", {"name": "", "role": "door_monitor", "door": "porta", "contact": "no"}, "admin")
    config = {c["Name"]: c["Value"] for c in controller.door_configs["porta"]["Configuration"]}
    assert config == {
        "Lock.Type": "FailSecure",
        "DoorMonitor.Type": "Standard",
        "DoorMonitor.ValueWhenOpen": "Input Ground",
        "DoorMonitor.ValueWhenClosed": "Input Open",
    }
    assert controller.io_assignments["DPS"] == {
        "IoMode": "in:pu/gnd",
        "IoUser": [
            {"Type": "doorcontrol", "token": "porta", "Usage": "DoorOpenMonitor", "MultiIo": False},
            {"Type": "doorcontrol", "token": "porta", "Usage": "DoorClosedMonitor", "MultiIo": False},
        ],
    }
    inputs = {i["io"]: i for i in (await service.read())["inputs"]}
    assert inputs["DPS"]["role"] == "door_monitor" and inputs["DPS"]["name"] == "Sensore Porta"
    assert "door_monitor" in inputs["DPS"]["roles"]

    # Contatto NC: valori invertiti
    await service.update_input("DPS", {"role": "door_monitor", "door": "porta", "contact": "nc"}, "admin")
    config = {c["Name"]: c["Value"] for c in controller.door_configs["porta"]["Configuration"]}
    assert config["DoorMonitor.ValueWhenClosed"] == "Input Ground"

    # Tolto: DoorMonitor spento e ingresso libero; serratura invariata
    await service.update_input("DPS", {"role": ""}, "admin")
    config = {c["Name"]: c["Value"] for c in controller.door_configs["porta"]["Configuration"]}
    assert config["DoorMonitor.Type"] == "None" and config["Lock.Type"] == "FailSecure"
    assert controller.io_assignments["DPS"]["IoUser"] == []


async def test_door_contact_set_by_hand_is_recognised(db, controller):
    # Al banco il contatto porta è stato impostato a mano: l'app lo mostra e lo può togliere
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    service = ProgrammingService(db, controller, controller_id, asyncio.Lock())
    controller.door_configs["porta"]["Configuration"] += [
        {"Name": "DoorMonitor.Type", "Value": "Standard"},
        {"Name": "DoorMonitor.ValueWhenOpen", "Value": "Input Open"},
        {"Name": "DoorMonitor.ValueWhenClosed", "Value": "Input Ground"},
    ]
    users = [
        {"Type": "doorcontrol", "token": "porta", "Usage": u, "MultiIo": False}
        for u in ("DoorOpenMonitor", "DoorClosedMonitor")
    ]
    await controller.set_io_assignment("DPS", "in:pu/gnd", users)
    dps = {i["io"]: i for i in (await service.read())["inputs"]}["DPS"]
    assert (dps["role"], dps["door"], dps["contact"]) == ("door_monitor", "porta", "nc")

    await service.update_input("DPS", {"role": ""}, "admin")
    config = {c["Name"]: c["Value"] for c in controller.door_configs["porta"]["Configuration"]}
    assert config["DoorMonitor.Type"] == "None" and controller.io_assignments["DPS"]["IoUser"] == []
    # Annullando la modifica torna com'era
    change = db.list_controller_changes(controller_id)[0]
    assert db.get_controller_change(change["id"])["before"]["role"] == "door_monitor"
    await service.undo(change["id"], "admin")
    config = {c["Name"]: c["Value"] for c in controller.door_configs["porta"]["Configuration"]}
    assert config["DoorMonitor.Type"] == "Standard" and config["DoorMonitor.ValueWhenClosed"] == "Input Ground"
    assert len(controller.io_assignments["DPS"]["IoUser"]) == 2


async def test_unmanaged_access_points_are_shown_and_can_be_managed(db, controller):
    service = ProgrammingService(db, controller, 1, asyncio.Lock())
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"] = []

    assert (await service.read())["unmanaged"] == [{"token": "ap-in", "name": "Ingresso"}]
    assert await service.manage_access_points("admin") == 1
    assert controller.managed() == {"ap-in"}
    assert (await service.read())["unmanaged"] == []
    assert db.list_audit()[0]["detail"] == "punti di accesso gestiti dal controller: Ingresso"
    # Già tutti gestiti: niente da scrivere
    controller.calls.clear()
    assert await service.manage_access_points("admin") == 0
    assert not [c for c in controller.calls if c[0] == "set_access_controller"]


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
        summary = programming["changes"][0]["summary"]
        assert summary == "porta Cantina B: rinominata da Cantina, Tempo di Apertura 5 → 4 s"
        assert client.post(f"/api/programming/changes/{programming['changes'][0]['id']}/undo").status_code == 200
        assert controller.doors["porta2"]["Name"] == "Cantina"

        # Senza il permesso impianti non si vede niente
        client.get("/api/me", headers={"X-Remote-User-Name": "admin"})
        assert client.get("/api/installation", headers={"X-Remote-User-Name": "altro"}).status_code == 403
