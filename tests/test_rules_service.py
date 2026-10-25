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


async def test_standard_and_external_schedules_are_read_only(rules):
    with pytest.raises(ServiceError):
        await rules.update_schedule("standard_always", "x", "", OFFICE, True, "admin")
    with pytest.raises(ServiceError):
        await rules.delete_schedule("standard_always", "admin")
    with pytest.raises(ServiceError, match="fuori dall'add-on"):
        await rules.update_schedule("altra", "x", "", OFFICE, True, "admin")


async def test_create_group_copies_default_profile_with_new_schedule(rules, controller):
    schedule = (await rules.create_schedule("Ufficio", "", OFFICE, True, "admin"))["token"]

    token = (await rules.create_group("Dipendenti", "fissi", schedule, "admin"))["token"]

    profile = controller.profiles[token]
    assert profile["Name"] == "Dipendenti"
    assert profile["AccessPolicy"][0]["AccessPoint"] == "ap-in"
    assert profile["AccessPolicy"][0]["Schedule"] == [schedule]
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


async def test_group_rules(rules, controller, db):
    with pytest.raises(ServiceError, match="non esiste"):
        await rules.create_group("X", "", "manca", "admin")
    with pytest.raises(ServiceError, match="predefinito"):
        await rules.update_group("profilo_sempre", "X", "", "standard_always", "admin")
    with pytest.raises(ServiceError, match="predefinito"):
        await rules.delete_group("profilo_sempre", "admin")

    token = (await rules.create_group("Ospiti", "", "standard_always", "admin"))["token"]
    await rules.update_group(token, "Visitatori", "", "standard_office_hours", "admin")
    assert controller.profiles[token]["Name"] == "Visitatori"
    assert controller.profiles[token]["AccessPolicy"][0]["Schedule"] == ["standard_office_hours"]

    await rules.access.create_person("Anna", "04aa", "admin", group=token)
    groups = {g["token"]: g for g in (await rules.list_groups())["groups"]}
    assert groups[token]["persons"] == 1
    assert groups["profilo_sempre"]["default"] is True
    with pytest.raises(ServiceError, match="assegnato a 1 persone"):
        await rules.delete_group(token, "admin")
