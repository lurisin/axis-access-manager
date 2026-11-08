"""Stato voluto calcolato dal database (desired.py) e catalogo dell'A1210 (hardware.py).

Golden test: per gli stessi dati, il voluto è quello che il codice di oggi scrive sul controller finto per tessere,
utenti, profili delle tipologie, profilo dei pulsanti, ingressi e fasce."""

import pytest
from test_backup import Plant
from test_rules_service import OFFICE

from app import hardware, programming
from app.compare import project, read_strict
from app.desired import (
    build,
    credential_payload,
    door_schedules,
    input_payloads,
    load_conf,
    places,
    profile_payload,
    profile_policies,
    rex_payload,
    schedule_payload,
    signature,
    user_payload,
)
from app.presa_in_carico import presa_in_carico


@pytest.fixture
def plant(db, controller, settings, tmp_path):
    return Plant(db, controller, settings, tmp_path / "backup")


async def people(plant):
    rule = {"target": "door", "target_id": plant.scala, "schedule": "standard_always"}
    category = await plant.rules.save_category(None, {"name": "Personale", "rules": [rule]}, "admin")
    plant.rules.set_default_category(category["id"], "admin")
    anna = await plant.access.create_person("Anna", "04AA", "admin")
    bruno = await plant.access.create_person(
        "Bruno", "04bb", "admin", valid_from="2026-01-01T08:00", valid_to="2027-01-01T00:00:00+01:00"
    )
    carla = await plant.access.create_person("Carla", "", "admin")
    await plant.access.add_card(carla["id"], "04cc", "admin")
    anna = await plant.access.add_card(anna["id"], "04ab", "admin")
    await plant.access.rename_person(bruno["id"], "Bruno Bianchi", "admin")
    await plant.access.set_person_access(anna["id"], "", "", "2028-06-30T00:00:00Z", "admin")
    second = next(c for c in anna["cards"] if c["number"] == "04ab")
    await plant.access.set_card_enabled(second["id"], False, "admin")
    return category


async def test_cards_and_users_are_what_the_app_writes(plant, controller, db):
    await people(plant)
    conf = load_conf(db, plant.access.settings.access_profile)
    objects = build(conf, plant.controller_id, ["utente", "tessera"])["objects"]

    checked = 0
    for person in conf["persons"]:
        token = person["axis_user_token"]
        assert user_payload(person) == controller.users[token] == objects["utente"][token]
        for card in person["cards"]:
            payload = credential_payload(card, person, conf["default_group"])
            written = controller.credentials[card["axis_credential_token"]]
            assert objects["tessera"][card["axis_credential_token"]] == payload
            if card["enabled"]:
                assert payload == written
            else:
                # DisableCredential cambia solo Enabled sul finto (T4): Status resta quello scritto, e non si confronta
                assert {k: v for k, v in payload.items() if k != "Status"} == {
                    k: v for k, v in written.items() if k != "Status"
                }
            assert signature("tessera", payload) == signature("tessera", project("tessera", written, payload))
            checked += 1
    assert checked == 4


async def test_category_profiles_are_what_the_app_writes(plant, controller, db):
    category = await people(plant)
    db.set_door_area(plant.scala, plant.area)
    office = (await plant.rules.create_schedule("Ufficio", "", OFFICE, False, "admin"))["token"]
    rules = [
        {"target": "door", "target_id": plant.scala, "schedule": "standard_always"},
        {"target": "area", "target_id": plant.area, "schedule": office},
    ]
    mixed = await plant.rules.save_category(None, {"name": "Turni", "description": "Due fasce", "rules": rules}, "a")
    conf = load_conf(db, plant.access.settings.access_profile)
    points = await controller.list_access_points()

    for token, category_id in ((category["token"], category["id"]), (mixed["token"], mixed["id"])):
        row = next(c for c in conf["categories"] if c["id"] == category_id)
        schedules = door_schedules(row["rules"], *places(conf, plant.controller_id))
        policies = profile_policies(schedules, points, set())
        assert profile_payload(row, policies, {"token": token}) == controller.profiles[token]
    assert controller.profiles[mixed["token"]]["Schedule"] == ["standard_always"]

    # Lo stesso dallo stato voluto completo, dopo la presa in carico
    adopted, _ = presa_in_carico(conf, await read_strict(controller), plant.controller_id)
    profiles = build(adopted, plant.controller_id, ["tipologia"])["objects"]["tipologia"]
    assert profiles[category["token"]] == controller.profiles[category["token"]]
    assert profiles[mixed["token"]] == controller.profiles[mixed["token"]]


async def test_inputs_and_button_profile_are_what_the_app_writes(plant, controller, db):
    await plant.programming.update_input("REX", {"name": "Relè Citofono", "role": "intercom", "door": "porta"}, "a")
    await plant.programming.update_input("IO1", {"name": "Uscita", "role": "rex", "door": "porta"}, "admin")
    rows = db.controller_inputs(plant.controller_id)
    [rex] = [p for p in controller.profiles.values() if p.get("Name") == programming.REX_PROFILE_NAME]

    # Le regole nell'ordine degli ingressi nel database; l'app le scrive nell'ordine in cui li ha salvati
    assert in_order(rex_payload(rex["token"], programming.rex_policies(rows.values()))) == in_order(rex)
    for io, row in rows.items():
        found = input_payloads(io, row)
        idpoint, point = row["idpoint_token"], row["access_point_token"]
        assert found["lettore"][idpoint] == controller.idpoints[idpoint]
        configuration = {c["Name"]: c["Value"] for c in found["config_lettore"][idpoint]["Configuration"]}
        assert {**configuration, "DeviceUUID": controller.device_uuid} == controller.idpoint_configs[idpoint]
        assert found["morsetto"][io] == {"IoName": io, **controller.io_assignments[io]}
        assert found["punto"][point] == next(p for p in controller.access_points if p["token"] == point)

    adopted, _ = presa_in_carico(load_conf(db), await read_strict(controller), plant.controller_id)
    [(token, payload)] = build(adopted, plant.controller_id, ["pulsanti"])["objects"]["pulsanti"].items()
    assert token == rex["token"] and in_order(payload) == in_order(rex)
    assert signature("pulsanti", payload) == signature("pulsanti", rex)


def in_order(profile):
    return {**profile, "AccessPolicy": sorted(profile["AccessPolicy"], key=lambda p: p["AccessPoint"])}


async def test_schedules_are_what_the_app_writes(plant, controller, db):
    office = (await plant.rules.create_schedule("Ufficio", "Personale", OFFICE, True, "admin"))["token"]
    night = [{"days": ["SA", "SU"], "start": "20:00", "end": "24:00"}]
    other = (await plant.rules.create_schedule("Notte", "", night, False, "admin"))["token"]
    await plant.rules.add_holidays([{"day": "2026-12-25", "name": "Natale", "yearly": True}], "admin")
    await plant.rules.add_holidays([{"day": "2027-03-29", "name": "Pasquetta"}], "admin")
    await plant.rules.delete_holiday("2027-03-29", "admin")

    holidays = db.list_holidays()
    assert schedule_payload(office, "Ufficio", "Personale", OFFICE, True, holidays) == controller.schedules[office]
    assert schedule_payload(other, "Notte", "", night, False, holidays) == controller.schedules[other]

    adopted, _ = presa_in_carico(load_conf(db), await read_strict(controller), plant.controller_id)
    schedules = build(adopted, plant.controller_id, ["fascia"])["objects"]["fascia"]
    assert schedules == {office: controller.schedules[office], other: controller.schedules[other]}


async def test_desired_state_reads_only_the_database(plant, controller, db):
    await people(plant)
    reads, calls = list(controller.reads), list(controller.calls)

    conf = load_conf(db, "profilo_sempre")
    result = build(conf, plant.controller_id)

    assert controller.reads == reads and controller.calls == calls
    assert "password" not in str(conf["controllers"]) and "segreta" not in str(conf)
    # Senza la presa in carico la programmazione è da prendere in carico
    assert set(result["pending"]) == set(result["kinds"]) - {"utente", "tessera"}
    assert set(result["objects"]) == {"utente", "tessera"}


def test_hardware_catalogue_is_the_prudent_one():
    assert hardware.APP_TOKEN_KINDS == {"utente"}
    assert set(hardware.LIMITS) == {"utenti", "tessere", "profili", "fasce", "eccezioni", "punti", "regole"}
    assert all(limit is None for limit in hardware.LIMITS.values())
    # Spostati da programming, che li usa ancora da qui
    assert programming.CONTROLLER_INPUTS is hardware.CONTROLLER_INPUTS
    assert programming.ALLOWED_ROLES is hardware.ALLOWED_ROLES
    assert programming._plain_mode("IO1") == "inout:pu/gnd" and programming._plain_mode("REX") == "in:pu/gnd"
    for kind, token in (
        ("fascia", "standard_always"),
        ("fascia", "standard_holidays"),
        ("modo", "CardOnly"),
        ("modo", "Fatto a mano"),
        ("gestiti", "Axis-b8a44fe6a074 AccessController"),
    ):
        assert hardware.is_factory(kind, token, "B8A44FE6A074"), (kind, token)
    for kind, token in (("fascia", "ufficio"), ("gestiti", "Axis-altro AccessController"), ("morsetto", "Ser0hd")):
        assert not hardware.is_factory(kind, token, "B8A44FE6A074"), (kind, token)
