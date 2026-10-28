import pytest

from app.rules_service import RulesService
from app.service import AccessService, ServiceError

OFFICE = [{"days": ["MO", "TU", "WE", "TH", "FR"], "start": "08:00", "end": "18:00"}]


@pytest.fixture
def rules(db, controller, settings):
    return RulesService(AccessService(db, controller, settings))


async def test_create_schedule_writes_calendar_and_remembers_rules(rules, controller, db):
    token = (await rules.create_schedule("Ufficio", "", OFFICE, True, "admin"))["token"]

    schedule = controller.schedules[token]
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR" in schedule["ScheduleDefinition"]
    assert schedule["ExceptionScheduleDefinition"] is None
    listed = {s["token"]: s for s in await rules.list_schedules()}
    assert listed[token]["managed"] is True
    assert listed[token]["summary"] == "lun-ven 08:00–18:00"
    assert listed["standard_always"]["name"] == "Sempre"
    assert listed["standard_always"]["groups"] == ["Sempre"]


async def test_holidays_are_written_as_exceptions(rules, controller):
    token = (await rules.create_schedule("Ufficio", "", OFFICE, True, "admin"))["token"]
    other = (await rules.create_schedule("Notte", "", OFFICE, False, "admin"))["token"]

    await rules.add_holidays([{"day": "2026-12-25", "name": "Natale", "yearly": True}], "admin")

    assert "DTSTART;VALUE=DATE:20261225" in controller.schedules[token]["ExceptionScheduleDefinition"]
    assert controller.schedules[other]["ExceptionScheduleDefinition"] is None
    await rules.delete_holiday("2026-12-25", "admin")
    assert controller.schedules[token]["ExceptionScheduleDefinition"] is None


async def test_startup_rewrites_only_schedules_with_missing_holidays(rules, controller):
    """Le festività rifiutate dalle versioni precedenti si riscrivono all'avvio, senza toccare le fasce già a posto."""
    token = (await rules.create_schedule("Ufficio", "", OFFICE, True, "admin"))["token"]
    await rules.add_holidays([{"day": "2026-12-25", "name": "Natale", "yearly": True}], "admin")
    assert await rules.reapply_holidays(only_changed=True) == 0

    controller.schedules[token]["ExceptionScheduleDefinition"] = None
    assert await rules.reapply_holidays(only_changed=True) == 1
    assert "BYMONTH=12" in controller.schedules[token]["ExceptionScheduleDefinition"]


async def test_standard_and_external_schedules_can_be_edited_and_hidden(rules, controller):
    controller.schedules["altra"] = {
        "token": "altra",
        "Name": "Notturno",
        "ScheduleDefinition": "BEGIN:VEVENT\r\nDTSTART:19700101T200000\r\nDTEND:19700101T230000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT",
    }
    listed = {s["token"]: s for s in await rules.list_schedules()}
    assert listed["altra"]["managed"] is False
    assert listed["altra"]["summary"] == "lun 20:00–23:00"

    await rules.update_schedule("standard_office_hours", "Ufficio", "", OFFICE, False, "admin")
    listed = {s["token"]: s for s in await rules.list_schedules()}
    assert listed["standard_office_hours"]["managed"] is True
    assert listed["standard_office_hours"]["name"] == "Ufficio"
    with pytest.raises(ServiceError, match="non trovata"):
        await rules.update_schedule("manca", "x", "", OFFICE, True, "admin")

    rules.set_hidden("schedule", "altra", True, "admin")
    listed = await rules.list_schedules()
    assert listed[-1]["token"] == "altra" and listed[-1]["hidden"] is True
    await rules.delete_schedule("altra", "admin")
    assert "altra" not in controller.schedules
    # La fascia dei profili con fasce diverse per porta
    with pytest.raises(ServiceError, match="non si può eliminare"):
        await rules.delete_schedule("standard_always", "admin")


async def test_create_group_copies_default_profile_with_new_schedule(rules, controller):
    schedule = (await rules.create_schedule("Ufficio", "", OFFICE, True, "admin"))["token"]

    token = (await rules.create_group("Dipendenti", "fissi", schedule, "admin"))["token"]

    profile = controller.profiles[token]
    assert profile["Name"] == "Dipendenti"
    assert profile["AccessPolicy"][0]["AccessPoint"] == "ap-in"
    assert profile["AccessPolicy"][0]["Schedule"] == [schedule]
    # L'A1210 valuta anche la fascia del profilo: se manca non entra nessuno
    assert profile["Schedule"] == [schedule]
    # Il profilo predefinito non cambia
    assert controller.profiles["profilo_sempre"]["AccessPolicy"][0]["Schedule"] == ["standard_always"]
    with pytest.raises(ServiceError, match="usata dal profilo Dipendenti"):
        await rules.delete_schedule(schedule, "admin")


async def test_create_group_without_default_profile_uses_door_access_points(rules, controller):
    controller.profiles.clear()
    token = (await rules.create_group("Tutti", "", "standard_always", "admin"))["token"]
    assert controller.profiles[token]["AccessPolicy"] == [
        {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_always"]}
    ]
    assert controller.profiles[token]["Schedule"] == ["standard_always"]


async def test_repair_group_schedules(rules, controller, db):
    """Gruppi salvati dalle versioni precedenti, con la fascia solo nelle regole: si riparano all'avvio."""
    policy = {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": []}
    controller.profiles["vecchio"] = {
        "token": "vecchio",
        "Name": "Ospiti",
        "Schedule": [],
        "AccessPolicy": [{**policy, "Schedule": ["standard_office_hours"]}],
    }
    # Fasce diverse nelle regole: non si indovina, si lascia com'è
    controller.profiles["misto"] = {
        "token": "misto",
        "Name": "Misto",
        "Schedule": [],
        "AccessPolicy": [
            {**policy, "Schedule": ["standard_always"]},
            {**policy, "AccessPoint": "ap-out", "Schedule": ["standard_office_hours"]},
        ],
    }

    assert await rules.repair_group_schedules() == 1
    assert controller.profiles["vecchio"]["Schedule"] == ["standard_office_hours"]
    assert controller.profiles["misto"]["Schedule"] == []
    assert ("set_access_profile", "profilo_sempre") not in controller.calls
    assert await rules.repair_group_schedules() == 0


async def test_group_rules(rules, controller, db):
    with pytest.raises(ServiceError, match="non esiste"):
        await rules.create_group("X", "", "manca", "admin")
    with pytest.raises(ServiceError, match="predefinito"):
        await rules.delete_group("profilo_sempre", "admin")
    with pytest.raises(ServiceError, match="predefinito"):
        rules.set_hidden("group", "profilo_sempre", True, "admin")

    token = (await rules.create_group("Ospiti", "", "standard_always", "admin"))["token"]
    await rules.update_group(token, "Visitatori", "", "standard_office_hours", "admin")
    assert controller.profiles[token]["Name"] == "Visitatori"
    assert controller.profiles[token]["AccessPolicy"][0]["Schedule"] == ["standard_office_hours"]
    assert controller.profiles[token]["Schedule"] == ["standard_office_hours"]

    await rules.access.create_person("Anna", "04aa", "admin", group=token)
    groups = {g["token"]: g for g in (await rules.list_groups())["groups"]}
    assert groups[token]["persons"] == 1
    assert groups["profilo_sempre"]["default"] is True
    with pytest.raises(ServiceError, match="assegnato a 1 persona"):
        await rules.delete_group(token, "admin")


async def test_default_group_is_chosen_in_the_app(rules, controller, db):
    token = (await rules.create_group("Dipendenti", "", "standard_always", "admin"))["token"]
    await rules.set_default_group(token, "admin")
    assert rules.access.default_group == token
    person = await rules.access.create_person("Anna", "04aa", "admin")
    assert person["cards"][0]["access_profile"] == token
    groups = {g["token"]: g for g in (await rules.list_groups())["groups"]}
    assert groups[token]["default"] is True and groups["profilo_sempre"]["default"] is False
    # Il vecchio predefinito ora si può modificare, nascondere ed eliminare
    await rules.update_group("profilo_sempre", "Tutti", "", "standard_always", "admin")
    rules.set_hidden("group", "profilo_sempre", True, "admin")
    assert {g["token"]: g for g in (await rules.list_groups())["groups"]}["profilo_sempre"]["hidden"] is True
    await rules.delete_group("profilo_sempre", "admin")
    with pytest.raises(ServiceError, match="non trovato"):
        await rules.set_default_group("manca", "admin")


DOOR = {
    "snapshot_camera": "",
    "snapshot_on_granted": 0,
    "ha_buttons": 1,
    "intercom_name": "",
    "intercom_open_door": 0,
    "intercom_relay": 1,
    "intercom_mqtt_prefix": "hmd",
}


def place(db):
    """Via Consulta: Varco Scala nell'area Camere (dal controller), Cantina nei Locali Tecnici."""
    controller_id = db.save_controller(None, {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "verify_ssl": 0})
    site = db.save_site(None, "Via Consulta", "")
    rooms = db.save_area(None, site, "Camere")
    tech = db.save_area(None, site, "Locali Tecnici")
    db.set_controller_area(controller_id, rooms)
    scala = db.save_door(None, controller_id, DOOR | {"door_token": "porta", "name": "Varco Scala"})
    cantina = db.save_door(None, controller_id, DOOR | {"door_token": "porta2", "name": "Cantina", "area_id": tech})
    return {"site": site, "rooms": rooms, "tech": tech, "scala": scala, "cantina": cantina}


def row(target, target_id, schedule="standard_always"):
    return {"target": target, "target_id": target_id, "schedule": schedule}


async def test_categories_own_a_profile_built_from_doors_areas_and_sites(rules, controller, db):
    ids = place(db)
    controller.access_points.append(
        {"token": "ap-cantina", "Entity": "porta2", "Attribute": [{"Name": "Direction", "Value": "in"}]}
    )
    # Il pulsante di uscita apre senza tessera: nessuna regola per lui
    controller.access_points.append({"token": "ap-rex", "Entity": "porta", "AuthenticationProfile": ["REXOnly"]})

    maint = await rules.save_category(
        None,
        {"name": "Manutentore", "color": "orange", "rules": [row("door", ids["scala"]), row("door", ids["cantina"])]},
        "a",
    )
    profile = controller.profiles[maint["token"]]
    assert profile["Name"] == "Manutentore" and profile["Schedule"] == ["standard_always"]
    assert sorted(p["AccessPoint"] for p in profile["AccessPolicy"]) == ["ap-cantina", "ap-in"]

    # Area: solo le sue porte, anche quella che eredita l'area dal controller
    clean = await rules.save_category(
        None, {"name": "Pulizie", "rules": [row("area", ids["rooms"], "standard_office_hours")]}, "a"
    )
    profile = controller.profiles[clean["token"]]
    assert profile["AccessPolicy"] == [
        {"AccessPoint": "ap-in", "AuthorizationProfile": [], "Attribute": [], "Schedule": ["standard_office_hours"]}
    ]
    assert profile["Schedule"] == ["standard_office_hours"]

    # Fasce diverse per porta: quella del profilo lascia passare sempre (l'A1210 le valuta in AND)
    mixed = await rules.save_category(
        None,
        {"name": "Tecnico", "rules": [row("site", ids["site"], "standard_office_hours"), row("door", ids["scala"])]},
        "a",
    )
    profile = controller.profiles[mixed["token"]]
    assert profile["Schedule"] == ["standard_always"]
    schedules = {p["AccessPoint"]: p["Schedule"] for p in profile["AccessPolicy"]}
    assert schedules == {"ap-in": ["standard_office_hours", "standard_always"], "ap-cantina": ["standard_office_hours"]}

    guest = await rules.save_category(
        None, {"name": "Ospite", "valid_days": 7, "rules": [row("door", ids["scala"])]}, "a"
    )
    with pytest.raises(ServiceError, match="esiste già"):
        await rules.save_category(None, {"name": "manutentore", "rules": [row("door", ids["scala"])]}, "a")
    with pytest.raises(ServiceError, match="almeno una riga"):
        await rules.save_category(None, {"name": "Vuota", "rules": []}, "a")
    with pytest.raises(ServiceError, match="non esiste più"):
        await rules.save_category(None, {"name": "X", "rules": [row("area", 999)]}, "a")

    # La tessera prende il profilo e la durata della tipologia
    anna = await rules.access.create_person("Anna", "04aa", "admin", category_id=clean["id"])
    assert anna["category_id"] == clean["id"] and anna["cards"][0]["access_profile"] == clean["token"]
    bruno = await rules.access.create_person("Bruno", "04bb", "admin", category_id=guest["id"])
    assert bruno["cards"][0]["access_profile"] == guest["token"] and bruno["cards"][0]["valid_to"].endswith("Z")

    # La Cantina entra nell'area Camere: Pulizie la comprende da sola, Manutentore non cambia
    db.set_door_area(ids["cantina"], ids["rooms"])
    assert await rules.refresh_category_profiles() == 1
    assert {p["AccessPoint"] for p in controller.profiles[clean["token"]]["AccessPolicy"]} == {"ap-in", "ap-cantina"}
    assert await rules.refresh_category_profiles() == 0

    # Accesso diverso da quello della tipologia: segnalato e riallineabile
    await rules.access.set_person_access(anna["id"], "profilo_sempre", "", "", "admin")
    assert {c["name"]: c["custom"] for c in rules.list_categories()}["Pulizie"] == 1
    person = await rules.align_person(anna["id"], "admin")
    assert person["cards"][0]["access_profile"] == clean["token"]

    with pytest.raises(ServiceError, match="usata dalla tipologia Pulizie"):
        rules.check_place_unused("area", ids["rooms"])
    rules.set_default_category(guest["id"], "admin")
    assert rules.access.default_group == guest["token"]
    assert (await rules.access.create_person("Carla", "04cc", "admin"))["category_id"] == guest["id"]
    with pytest.raises(ServiceError, match="predefinita"):
        await rules.delete_category(guest["id"], "admin")
    with pytest.raises(ServiceError, match="ci sono persone"):
        await rules.delete_category(clean["id"], "admin")
    await rules.delete_category(maint["id"], "admin")
    assert maint["token"] not in controller.profiles


async def test_groups_become_categories_without_changing_access(rules, controller, db):
    ids = place(db)
    db.set_setting("default_group", "profilo_sempre")
    # Prima della 0.10: tipologia senza gruppo (= predefinito), persona senza tipologia
    guest = db.save_category(
        None, {"name": "Ospite", "color": "purple", "group_token": "", "valid_days": 1, "description": ""}
    )
    anna = await rules.access.create_person("Anna", "04aa", "admin", group="profilo_sempre")
    bruno = await rules.access.create_person("Bruno", "04bb", "admin", category_id=guest)
    db.set_person_category(anna["id"], 0)
    calls = len(controller.calls)

    assert await rules.adopt_groups() > 0
    assert len(controller.calls) == calls  # nessuna scrittura sul controller
    categories = {c["name"]: c for c in rules.list_categories()}
    assert categories["Ospite"]["group_token"] == "profilo_sempre"
    assert categories["Ospite"]["rules"] == [row("door", ids["scala"])]
    assert categories["Sempre"]["group_token"] == "profilo_sempre" and categories["Sempre"]["default"] is True
    assert db.get_person(anna["id"])["category_id"] == categories["Sempre"]["id"]
    assert await rules.adopt_groups() == 0

    # Profilo in comune: modificando Ospite nasce il suo profilo, Bruno passa al nuovo, Anna resta com'è
    result = await rules.save_category(
        guest, {"name": "Ospite", "valid_days": 1, "rules": [row("door", ids["scala"], "standard_office_hours")]}, "a"
    )
    assert result["token"] != "profilo_sempre" and result["updated"] == 1
    assert db.get_person(bruno["id"])["cards"][0]["access_profile"] == result["token"]
    assert db.get_person(anna["id"])["cards"][0]["access_profile"] == "profilo_sempre"
    assert controller.profiles["profilo_sempre"]["AccessPolicy"][0]["Schedule"] == ["standard_always"]
