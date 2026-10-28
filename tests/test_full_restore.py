import pytest
from fastapi.testclient import TestClient
from test_backup import Plant
from test_rules_service import OFFICE

from app.full_restore import renamed_tokens
from app.main import create_app
from app.restore import Runtime, apply, plan
from app.vapix import VapixError

FULL = ["dispositivo", "controller", "tessere"]
OLD_UUID = "e6689c2d-4eca-4637-9a0e-b8a44fe6a074"
NEW_UUID = "11111111-2222-4333-8444-accc8e000001"
MQTT = {
    "server": {"protocol": "tcp", "host": "172.27.120.21", "port": 1883},
    "username": "axis_mqtt",
    "password": "segreta",
    "clientId": "via_consulta_controller",
    "deviceTopicPrefix": "axis/B8A44FE6A074",
}
EVENTS = {
    "topicPrefix": "custom",
    "customTopicPrefix": "accessi/x",
    "eventFilterList": [{"topicFilter": "onvif:AccessControl//.", "qos": 1, "retain": "property"}],
}


def bench_like(controller):
    """Come l'A1210 del banco: dispositivo nelle configurazioni, MQTT con utente dedicato, un ingresso dell'app."""
    controller.serial = "B8A44FE6A074"
    controller.device_uuid = OLD_UUID
    controller.door_configs["porta"]["DeviceUUID"] = OLD_UUID
    controller.door_configs["porta"]["DoorScheduleConfiguration"] = ""
    controller.idpoint_configs = {
        "lettore-1": {"DeviceUUID": OLD_UUID, "IdPoint.Reader.Type": "RS-485HD"},
        "Axis-b8a44fe6a074:1.1": {"DeviceUUID": OLD_UUID, "IdPoint.REX.Type": "ActiveLow"},
    }
    controller.idpoints["Axis-b8a44fe6a074:1.1"] = {"token": "Axis-b8a44fe6a074:1.1", "Name": "Relè Citofono"}
    controller.access_points[0]["DoorDeviceUUID"] = OLD_UUID
    controller.access_points.append(
        {
            "token": "Axis-b8a44fe6a074:1.2",
            "Name": "Relè Citofono",
            "Entity": "porta",
            "EntityType": "axtdc:Door",
            "Enabled": True,
            "DoorDeviceUUID": OLD_UUID,
            "IdPointDevice": [{"IdPoint": "Axis-b8a44fe6a074:1.1", "DeviceUUID": OLD_UUID}],
            "AuthenticationProfile": ["REXOnly"],
            "Attribute": [{"Name": "Direction", "Value": "out"}],
        }
    )
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"].append("Axis-b8a44fe6a074:1.2")
    controller.io_assignments = {
        "Ser0hd": {"IoMode": "rs485hd", "IoUser": [{"Type": "idpoint", "token": "lettore-1", "Usage": "RS-485HD"}]},
        "REX": {
            "IoMode": "in:pu/gnd",
            "IoUser": [{"Type": "idpoint", "token": "Axis-b8a44fe6a074:1.1", "Usage": "REX", "MultiIo": False}],
        },
    }
    controller.mqtt, controller.mqtt_events = dict(MQTT), dict(EVENTS)

    async def reader_configurations():
        return [
            {
                "token": token,
                "DeviceUUID": config["DeviceUUID"],
                "Configuration": [{"Name": k, "Value": v} for k, v in config.items() if k != "DeviceUUID"],
            }
            for token, config in controller.idpoint_configs.items()
        ]

    controller.list_idpoint_configurations = reader_configurations


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    plant = Plant(db, controller, settings, tmp_path / "backup")
    bench_like(controller)
    return plant


async def full_restore(plant, backup_id, keys=None, password="", known=None):
    _, saved = plant.backups.load(backup_id)
    current, _ = await plant.backups.snapshot(plant.clients)
    # Come l'app: con i token già cambiati dai ripristini precedenti
    items = plan(saved, current, FULL, plant.controller_id, set(known or {}), renamed_tokens(plant.db))
    chosen = [i["key"] for i in items if i["default"] and not i["blocked"]] if keys is None else keys
    runtime = Runtime(
        plant.db,
        plant.access,
        plant.rules,
        plant.programming,
        plant.controller_id,
        mqtt_password=password,
        mqtt_known=known or {},
    )
    return items, await apply(items, chosen, runtime, "admin")


def written(controller, name):
    return [c for c in controller.calls if c[0] == name]


async def test_full_restore_after_factory_reset(plant, controller, db):
    anna = await plant.access.create_person("Anna", "04aa", "admin")
    await plant.access.create_person("Bruno", "04bb", "admin")
    await plant.access.set_card_enabled(anna["cards"][0]["id"], False, "admin")
    saved_doors = {t: dict(d) for t, d in controller.doors.items()}
    saved_points = {p["token"]: p for p in controller.access_points}
    backup = await plant.backup()
    # Nel backup la password del broker non c'è
    _, parts = plant.backups.load(backup["id"])
    assert "password" not in parts[f"controller:{plant.controller_id}"]["device"]["mqtt"]

    controller.factory_reset()
    controller.calls.clear()
    items, result = await full_restore(plant, backup["id"], password="segreta")

    keys = [i["key"] for i in items]
    assert keys[:3] == ["disp-fuso", "disp-mqtt", "disp-mqtt-eventi"]
    assert next(i for i in items if i["key"] == "disp-mqtt")["password"] is True
    assert "prog-fascia:standard_office_hours" in keys and "prog-fascia:standard_always" not in keys
    assert keys.index("prog-porta:porta") < keys.index("prog-lettore:lettore-1") < keys.index("prog-io:Ser0hd")
    assert keys.index("prog-io:REX") < keys.index("prog-punto:ap-in") < keys.index("prog-profilo:profilo_sempre")
    # Il reset svuota l'elenco dei punti di accesso gestiti: si rimette dopo averli ricreati
    managed = next(i for i in items if i["key"] == "prog-gestiti")
    assert managed["action"] == "ricrea" and managed["default"] is True
    assert managed["changes"] == [{"label": "Gestiti", "old": "nessuno", "new": "Ingresso, Relè Citofono"}]
    assert keys.index("prog-punto:Axis-b8a44fe6a074:1.2") < keys.index("prog-gestiti")
    assert keys[-1] == "tessere" and "2 tessere di 2 persone" in items[-1]["text"]
    # La fascia di fabbrica che nel backup non c'era si toglie, di serie perché il controller è appena resettato
    extra = next(i for i in items if i["key"] == "prog-fascia-togli:standard_after_hours")
    assert extra["action"] == "togli" and extra["default"] is True
    assert [i["key"] for i in items if i["section"] == "controller" and i["action"] not in ("ricrea", "togli")] == []
    assert not result["failed"], result["failed"]
    assert result["reload"] is True

    # Tutto com'era, con gli stessi token
    assert controller.doors == saved_doors
    assert {p["token"] for p in controller.access_points} == set(saved_points)
    points = {p["token"]: p for p in controller.access_points}
    assert points["Axis-b8a44fe6a074:1.2"]["IdPointDevice"][0]["IdPoint"] == "Axis-b8a44fe6a074:1.1"
    assert controller.profiles["profilo_sempre"]["AccessPolicy"][0]["AccessPoint"] == "ap-in"
    assert controller.io_assignments["REX"]["IoUser"][0]["token"] == "Axis-b8a44fe6a074:1.1"
    assert controller.idpoint_configs["Axis-b8a44fe6a074:1.1"]["IdPoint.REX.Type"] == "ActiveLow"
    # Configurazioni scritte senza dispositivo: il controller ci mette il suo e le collega a sé (con l'UUID esplicito
    # al banco il lettore restava scollegato). La programmazione oraria della porta non si scrive.
    assert ("set_door_configuration", "porta", "") in controller.calls
    assert set(written(controller, "set_idpoint_configuration")) == {
        ("set_idpoint_configuration", "lettore-1", ""),
        ("set_idpoint_configuration", "Axis-b8a44fe6a074:1.1", ""),
    }
    assert controller.door_configs["porta"]["DeviceUUID"] == OLD_UUID
    assert controller.idpoint_configs["lettore-1"]["DeviceUUID"] == OLD_UUID
    assert "standard_after_hours" not in controller.schedules and "standard_always" in controller.schedules
    assert "DoorScheduleConfiguration" not in controller.door_configs["porta"]
    assert controller.time_zone == "Europe/Rome" and controller.mqtt["password"] == "segreta"
    assert controller.mqtt_events == EVENTS
    # Ordine di scrittura: chi è usato prima di chi lo usa, le tessere per ultime
    order = [c[0] for c in controller.calls]
    assert order.index("set_schedule") < order.index("set_door") < order.index("set_door_configuration")
    assert order.index("create_idpoint") < order.index("set_io_assignment") < order.index("create_access_point")
    assert order.index("set_access_profile") < order.index("set_credential")
    assert controller.managed() == {"ap-in", "Axis-b8a44fe6a074:1.2"}
    assert max(i for i, c in enumerate(order) if c == "create_access_point") < order.index("set_access_controller")
    cards = {c["number"]: c for c in db.all_cards()}
    assert not db.missing_cards()
    credential = controller.credentials[cards["04aa"]["axis_credential_token"]]
    assert credential["Enabled"] is False
    assert credential["CredentialAccessProfile"][0]["AccessProfile"] == "profilo_sempre"
    assert result["applied"][-1] == {"label": "Tessere", "text": "2 tessere riscritte"}

    # Di nuovo uguale al backup: niente da fare
    items, _ = await full_restore(plant, backup["id"], password="segreta")
    assert items == []


async def test_replaced_controller_gets_its_own_device(plant, controller):
    backup = await plant.backup()
    controller.serial = "ACCC8E000001"
    controller.factory_reset()
    controller.device_uuid = NEW_UUID

    items, result = await full_restore(plant, backup["id"], password="segreta")

    assert not result["failed"], result["failed"]
    # Configurazioni senza dispositivo: il controller nuovo ci mette il suo
    assert ("set_door_configuration", "porta", "") in controller.calls
    assert ("set_idpoint_configuration", "lettore-1", "") in controller.calls
    assert controller.idpoint_configs["lettore-1"]["DeviceUUID"] == NEW_UUID
    assert controller.door_configs["porta"]["DeviceUUID"] == NEW_UUID
    # I punti di accesso passano al dispositivo nuovo
    points = {p["token"]: p for p in controller.access_points}
    assert points["ap-in"]["DoorDeviceUUID"] == NEW_UUID
    assert points["Axis-b8a44fe6a074:1.2"]["IdPointDevice"][0]["DeviceUUID"] == NEW_UUID
    assert OLD_UUID not in str(controller.access_points)
    # Gestiti dal controllo accessi del controller nuovo, che ha un altro token
    assert list(controller.access_controllers) == ["Axis-accc8e000001 AccessController"]
    assert controller.managed() == {"ap-in", "Axis-b8a44fe6a074:1.2"}


async def test_backup_without_access_controllers_links_all_its_access_points(plant, controller):
    """Backup fatti fino alla 0.21.0: non sanno quali punti erano gestiti, si fanno gestire tutti."""
    backup = await plant.backup()
    _, saved = plant.backups.load(backup["id"])
    saved[f"controller:{plant.controller_id}"].pop("access_controllers")
    controller.factory_reset()
    current, _ = await plant.backups.snapshot(plant.clients)

    items = plan(saved, current, FULL, plant.controller_id, set())

    managed = next(i for i in items if i["key"] == "prog-gestiti")
    assert set(managed["_points"]) == {"ap-in", "Axis-b8a44fe6a074:1.2"}


async def test_tokens_refused_by_the_new_controller_are_renamed_everywhere(plant, controller, db):
    rule = {"target": "door", "target_id": plant.scala, "schedule": "standard_always"}
    category = await plant.rules.save_category(None, {"name": "Pulizie", "rules": [rule]}, "admin")
    old_profile = "Axis-b8a44fe6a074:9.9"
    controller.profiles[old_profile] = {**controller.profiles.pop(category["token"]), "token": old_profile}
    db.replace_token(category["token"], old_profile)
    anna = await plant.access.create_person("Anna", "04aa", "admin", category_id=category["id"])
    assert db.get_person(anna["id"])["cards"][0]["access_profile"] == old_profile
    db.save_controller_input(
        plant.controller_id,
        "REX",
        {
            "name": "Relè Citofono",
            "role": "intercom",
            "door_token": "porta",
            "idpoint_token": "Axis-b8a44fe6a074:1.1",
            "access_point_token": "Axis-b8a44fe6a074:1.2",
        },
    )
    backup = await plant.backup()
    controller.factory_reset()
    controller.refuse_axis_tokens = True

    items, result = await full_restore(plant, backup["id"], password="segreta")

    assert not result["failed"], result["failed"]
    assert result["renamed"] == 3
    new_profile = db.get_category(category["id"])["group_token"]
    assert new_profile != old_profile and new_profile in controller.profiles
    assert db.get_person(anna["id"])["cards"][0]["access_profile"] == new_profile
    credential = next(iter(controller.credentials.values()))
    assert credential["CredentialAccessProfile"][0]["AccessProfile"] == new_profile
    mine = db.controller_inputs(plant.controller_id)["REX"]
    assert mine["idpoint_token"] in controller.idpoints and mine["access_point_token"] != "Axis-b8a44fe6a074:1.2"
    point = next(p for p in controller.access_points if p["token"] == mine["access_point_token"])
    assert point["IdPointDevice"][0]["IdPoint"] == mine["idpoint_token"]
    assert controller.io_assignments["REX"]["IoUser"][0]["token"] == mine["idpoint_token"]


async def test_second_restore_after_renamed_tokens_makes_no_duplicates(plant, controller):
    backup = await plant.backup()
    controller.factory_reset()
    controller.refuse_axis_tokens = True
    _, result = await full_restore(plant, backup["id"], password="segreta")
    assert not result["failed"], result["failed"]
    points, readers = {p["token"] for p in controller.access_points}, set(controller.idpoints)

    # Di nuovo dallo stesso backup (per esempio dopo una riga non riuscita): gli oggetti ci sono già
    items, result = await full_restore(plant, backup["id"], password="segreta")

    assert [i["key"] for i in items if i["action"] == "ricrea"] == []
    assert {p["token"] for p in controller.access_points} == points and set(controller.idpoints) == readers


async def test_timeout_after_a_write_makes_no_duplicate(plant, controller, db):
    backup = await plant.backup()
    controller.factory_reset()
    original = controller.create_access_point

    async def slow(point):
        token = await original(point)
        if point.get("token", "").startswith("Axis-"):
            # Il controller scrive, ma la risposta non arriva
            raise VapixError("il controller non risponde (ReadTimeout)")
        return token

    controller.create_access_point = slow
    _, result = await full_restore(plant, backup["id"], password="segreta")

    assert not result["failed"], result["failed"]
    assert "renamed" not in result and not renamed_tokens(db)
    assert sorted(p["token"] for p in controller.access_points) == ["Axis-b8a44fe6a074:1.2", "ap-in"]


async def test_schedule_rules_come_back_with_the_schedule(plant, controller, db):
    """Fasce fatte con l'app: dopo un reset tornano anche le regole nell'app, con le festività di adesso."""
    await plant.access.create_person("Anna", "04aa", "admin")
    await plant.rules.add_holidays([{"day": "2026-12-25", "name": "Natale"}], "admin")
    token = (await plant.rules.create_schedule("Uffici", "", OFFICE, True, "admin"))["token"]
    rules = db.schedule_rules()[token]
    backup = await plant.backup()
    await plant.rules.add_holidays([{"day": "2027-01-01", "name": "Capodanno"}], "admin")
    controller.factory_reset()
    # Le versioni precedenti le cancellavano alla prima lettura delle fasce
    db.delete_schedule_rules(token)

    items, result = await full_restore(plant, backup["id"], password="segreta")

    assert next(i for i in items if i["key"] == f"prog-fascia:{token}")["label"] == "Fascia Oraria Uffici"
    assert not result["failed"], result["failed"]
    assert db.schedule_rules()[token] == rules
    assert "20270101" in controller.schedules[token]["ExceptionScheduleDefinition"]
    items, _ = await full_restore(plant, backup["id"], password="segreta")
    assert items == []

    # Fascia uguale sul controller, regole perse nell'app: si rimettono solo quelle
    db.delete_schedule_rules(token)
    controller.calls.clear()
    items, result = await full_restore(plant, backup["id"], keys=[f"prog-fascia:{token}"])
    assert items[0]["text"] == "Riporta le regole della fascia nell'app" and not result["failed"]
    assert db.schedule_rules()[token] == rules and not written(controller, "set_schedule")


async def test_controller_gone_at_restore_time_is_not_unchanged(plant, controller):
    backup = await plant.backup()
    controller.factory_reset()
    _, saved = plant.backups.load(backup["id"])
    current, _ = await plant.backups.snapshot(plant.clients)
    keys = [i["key"] for i in plan(saved, current, FULL, plant.controller_id, set()) if i["default"]]
    # Tra l'anteprima e la conferma il controller smette di rispondere
    controller.offline = True
    current, _ = await plant.backups.snapshot(plant.clients)
    items = plan(saved, current, FULL, plant.controller_id, set())
    runtime = Runtime(plant.db, plant.access, plant.rules, plant.programming, plant.controller_id)

    result = await apply(items, keys, runtime, "admin")

    assert result["applied"] == [] and result["unchanged"] == 0
    assert [f["label"] for f in result["failed"]] == ["Ripristino Completo"]
    assert "non risponde" in result["failed"][0]["error"]


async def test_unreadable_cards_do_not_look_like_a_reset(plant, controller):
    await plant.access.create_person("Anna", "04aa", "admin")
    backup = await plant.backup()
    controller.doors["Door9"] = {"token": "Door9", "Name": "Porta Retro"}

    async def broken():
        raise VapixError("il controller non risponde (ReadTimeout)")

    controller.list_credentials = broken
    items, _ = await full_restore(plant, backup["id"], keys=[])

    extra = next(i for i in items if i["key"] == "prog-porta-togli:Door9")
    assert extra["default"] is False
    assert (extra["label"], extra["text"]) == ("Porta Retro", "Togli: non c'era nel backup")


async def test_door_times_are_capped_and_extra_objects_removed_after_reset(plant, controller):
    controller.doors["porta"]["AccessTime"] = "PT90S"
    await plant.access.create_person("Anna", "04aa", "admin")
    backup = await plant.backup()
    controller.factory_reset()
    controller.doors["Door1"] = {"token": "Door1", "Name": "Door 1"}

    items, result = await full_restore(plant, backup["id"], password="segreta")

    door = next(i for i in items if i["key"] == "prog-porta:porta")
    assert "fuori dai limiti della serratura" in door["warnings"][0]
    extra = next(i for i in items if i["key"] == "prog-porta-togli:Door1")
    # Dopo un reset (nessuna tessera sul controller) gli oggetti di fabbrica si tolgono di serie
    assert extra["action"] == "togli" and extra["default"] is True
    assert not result["failed"], result["failed"]
    assert controller.doors["porta"]["AccessTime"] == "PT30S" and "Door1" not in controller.doors


async def test_extra_access_point_is_unlinked_before_removal(plant, controller):
    backup = await plant.backup()
    controller.access_points.append({"token": "ap-extra", "Name": "In più", "Entity": "porta"})
    controller.access_controllers["Axis-x AccessController"]["AccessPoint"].append("ap-extra")

    items, result = await full_restore(plant, backup["id"], keys=["prog-punto-togli:ap-extra"])

    assert not result["failed"], result["failed"]
    assert "ap-extra" not in controller.managed() and "ap-extra" not in {p["token"] for p in controller.access_points}
    order = [c[0] for c in controller.calls]
    assert order.index("set_access_controller") < order.index("remove_access_point")
    # Il resto resta gestito
    assert controller.managed() == {"ap-in", "Axis-b8a44fe6a074:1.2"}


async def test_partial_differences_on_a_working_controller(plant, controller):
    await plant.access.create_person("Anna", "04aa", "admin")
    backup = await plant.backup()
    controller.doors["Door9"] = {"token": "Door9", "Name": "Aggiunta"}
    controller.door_configs["porta"]["Configuration"] = [{"Name": "Lock.Type", "Value": "FailSafe"}]
    controller.idpoints["lettore-2"]["Name"] = "Cambiato"
    controller.schedules["fascia_nuova"] = {"token": "fascia_nuova", "Name": "Nuova", "ScheduleDefinition": ""}

    items, result = await full_restore(plant, backup["id"], keys=[])

    by_key = {i["key"]: i for i in items}
    assert set(by_key) == {"prog-porta:porta", "prog-lettore:lettore-2", "prog-porta-togli:Door9"}
    assert by_key["prog-porta:porta"]["changes"] == [{"label": "Lock.Type", "old": "FailSafe", "new": "FailSecure"}]
    assert by_key["prog-lettore:lettore-2"]["changes"][0]["new"] == "Lettore interno"
    # Con le tessere sul controller un oggetto in più è lavoro fatto dopo il backup: non spuntato. Le fasce in più
    # non si propongono affatto: possono servire a tipologie e ingressi creati dopo
    assert by_key["prog-porta-togli:Door9"]["default"] is False
    assert result["applied"] == []


async def test_mqtt_password_known_to_the_add_on_is_not_asked(plant, controller):
    backup = await plant.backup()
    controller.factory_reset()

    items, result = await full_restore(plant, backup["id"], keys=["disp-mqtt"])
    assert result["failed"] == [
        {"label": "Collegamento MQTT", "error": "scrivi la password del broker MQTT per l'utente axis_mqtt"}
    ]
    items, result = await full_restore(plant, backup["id"], keys=["disp-mqtt"], known={"axis_mqtt": "dal-supervisor"})
    assert next(i for i in items if i["key"] == "disp-mqtt")["password"] is False
    assert not result["failed"] and controller.mqtt["password"] == "dal-supervisor"


async def test_offline_controller_blocks_the_full_restore(plant, controller):
    backup = await plant.backup()
    controller.offline = True

    items, _ = await full_restore(plant, backup["id"], keys=[])

    assert [i["key"] for i in items] == ["completo"] and "non risponde" in items[0]["blocked"]


@pytest.fixture
def client(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    bench_like(controller)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "api.db")
    with TestClient(app) as test_client:
        yield test_client


def test_full_restore_api(client, controller):
    admin = {"X-Remote-User-Name": "rising", "X-Remote-User-Display-Name": "Rising"}
    client.get("/api/me", headers=admin)
    client.post("/api/persons", json={"name": "Anna", "card": "04aa"}, headers=admin)
    created = client.post("/api/backups", json={}, headers=admin).json()
    part = next(k for k in created["parts"] if k.startswith("controller:"))
    controller.factory_reset()

    # Solo dalla scheda del controller
    response = client.post(f"/api/backups/{created['id']}/preview", json={"sections": FULL}, headers=admin)
    assert response.status_code == 400
    preview = client.post(
        f"/api/backups/{created['id']}/preview", json={"sections": FULL, "part": part}, headers=admin
    ).json()
    assert list(preview["sections"])[:2] == ["dispositivo", "controller"]
    assert all("_saved" not in i for i in preview["items"])
    keys = [i["key"] for i in preview["items"] if i["default"]]
    result = client.post(
        f"/api/backups/{created['id']}/restore",
        json={"sections": FULL, "part": part, "keys": keys, "mqtt_password": "segreta"},
        headers=admin,
    ).json()
    assert not result["failed"], result["failed"]
    assert controller.mqtt["password"] == "segreta" and "porta" in controller.doors
    assert next(iter(controller.credentials.values()))["IdData"][0]["Value"] == "04aa"
    actions = [a["action"] for a in client.get("/api/audit", headers=admin).json()]
    assert "ripristino completo del controller da backup" in actions
    # Il ripristino li ha già fatti gestire: dalla programmazione non resta niente da fare
    assert client.get("/api/programming", headers=admin).json()["unmanaged"] == []
    assert client.post("/api/programming/manage", headers=admin).json() == {"managed": 0, "permitted": 0}
