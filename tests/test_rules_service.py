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
    assert listed[token]["summary"] == "lun-ven 08:00-18:00"
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


async def test_standard_and_external_schedules_can_be_edited_and_hidden(rules, controller):
    controller.schedules["altra"] = {
        "token": "altra",
        "Name": "Notturno",
        "ScheduleDefinition": "BEGIN:VEVENT\r\nDTSTART:19700101T200000\r\nDTEND:19700101T230000\r\n"
        "RRULE:FREQ=WEEKLY;BYDAY=MO\r\nEND:VEVENT",
    }
    listed = {s["token"]: s for s in await rules.list_schedules()}
    assert listed["altra"]["managed"] is False
    assert listed["altra"]["summary"] == "lun 20:00-23:00"

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
    # Usata dal gruppo predefinito
    with pytest.raises(ServiceError, match="usata dai gruppi"):
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
    with pytest.raises(ServiceError, match="usata dai gruppi Dipendenti"):
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
    with pytest.raises(ServiceError, match="assegnato a 1 persone"):
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


async def test_categories_assign_group_and_validity(rules, controller, db):
    group = (await rules.create_group("Manutenzione", "", "standard_always", "admin"))["token"]
    maint = (await rules.save_category(None, {"name": "Manutentore", "group_token": group, "color": "orange"}, "a"))[
        "id"
    ]
    guest = (await rules.save_category(None, {"name": "Ospite", "valid_days": 7}, "a"))["id"]
    with pytest.raises(ServiceError, match="esiste già"):
        await rules.save_category(None, {"name": "manutentore"}, "a")
    with pytest.raises(ServiceError, match="non esiste più"):
        await rules.save_category(None, {"name": "X", "group_token": "manca"}, "a")

    anna = await rules.access.create_person("Anna", "04aa", "admin", category_id=maint)
    assert anna["category_id"] == maint
    assert anna["cards"][0]["access_profile"] == group
    bruno = await rules.access.create_person("Bruno", "04bb", "admin", category_id=guest)
    assert bruno["cards"][0]["access_profile"] == "profilo_sempre"
    assert bruno["cards"][0]["valid_to"].endswith("Z")
    with pytest.raises(ServiceError, match="usato dalle tipologie Manutentore"):
        await rules.delete_group(group, "admin")

    # Cambio di gruppo della tipologia applicato a chi ne fa parte
    other = (await rules.create_group("Notte", "", "standard_office_hours", "admin"))["token"]
    result = await rules.save_category(maint, {"name": "Manutentore", "group_token": other}, "a", apply=True)
    assert result["updated"] == 1
    assert db.get_person(anna["id"])["cards"][0]["access_profile"] == other

    # Cambio di tipologia di una persona: prende il gruppo della nuova tipologia
    person = await rules.access.set_person_access(bruno["id"], "", "", "", "admin", category_id=maint)
    assert person["category_id"] == maint and person["cards"][0]["access_profile"] == other

    rules.delete_category(maint, "admin")
    assert db.get_person(anna["id"])["category_id"] == 0
    assert [c["name"] for c in rules.list_categories()] == ["Ospite"]
