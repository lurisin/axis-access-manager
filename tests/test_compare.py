"""Letture strict e confronto con lo stato voluto (compare.py): non letto non è vuoto, chiavi che il controller completa
da sole, differenze vere, doppioni, oggetti di fabbrica e nessuna scrittura."""

import json
import re

import httpx
import pytest
from conftest import FakeController
from test_presa_in_carico import app_made, plant  # noqa: F401
from test_rules_service import OFFICE

from app import vapix
from app.compare import (
    KINDS,
    LISTS,
    compare,
    counts,
    exceptions_meaning,
    merge_readings,
    project,
    read_strict,
    schedule_meaning,
)
from app.desired import build, load_conf, schedule_payload, signature
from app.presa_in_carico import confronto
from app.rules import holidays_ical, italian_holidays, schedule_ical
from app.vapix import A1210Client


def voluto(objects):
    return {"kinds": [k for k in KINDS if k in objects], "objects": objects, "pending": [], "known": {}}


def letto(**lists):
    return {"lists": lists, "unread": {}, "serial": "X"}


def writes(controller):
    return [name for name in controller.attempts if name.startswith(vapix.WRITE_PREFIXES) or name in ("access_door",)]


async def test_lists_without_their_key_are_unread_with_the_real_client():
    """Il client vero, davanti a un A1210 che risponde 200 senza la chiave dell'elenco (T11): ogni elenco, morsetti
    compresi, resta non letto e non diventa un elenco vuoto."""
    asked = []

    def handler(request):
        body = json.loads(request.content)
        method = body.get("method") or next(iter(body))
        asked.append(method)
        if request.url.path == "/axis-cgi/basicdeviceinfo.cgi":
            return httpx.Response(200, json={"data": {"propertyList": {"SerialNumber": "B8A44FE6A074"}}})
        if request.url.path.startswith("/axis-cgi/"):
            return httpx.Response(200, json={"data": {}})
        return httpx.Response(200, json={})

    reading = await read_strict(A1210Client("a1210.local", "u", "p", transport=httpx.MockTransport(handler)))

    assert set(reading["unread"]) == set(LISTS)
    assert all("risposta del controller non riconosciuta" in reason for reason in reading["unread"].values())
    assert list(reading["lists"]) == ["device"] and reading["serial"] == "B8A44FE6A074"
    # Solo letture
    assert all(m.split(":")[-1].startswith(("Get", "get")) for m in asked), asked

    cards = {"c1": {"token": "c1", "IdData": [{"Name": "Card", "Value": "04aa"}]}}
    result = compare(voluto({"tessera": cards, "utente": {}}), reading)
    assert result["rows"] == []
    assert [(r["kind"], r["action"]) for r in result["unread"]] == [("utente", "non_letto"), ("tessera", "non_letto")]


async def test_unread_list_is_not_empty(plant, controller, db):  # noqa: F811
    """Tessere non lette (risposta senza la chiave): nessuna tessera dell'app da ricreare, nessuna da togliere."""
    await app_made(plant)
    controller.keyless = {"list_credentials"}

    reading = await read_strict(controller, ["tessera", "utente"])
    result = compare(build(load_conf(db), plant.controller_id, ["utente", "tessera"]), reading)

    assert "credentials" in reading["unread"] and "credentials" not in reading["lists"]
    assert [r for r in result["rows"] if r["kind"] == "tessera"] == []
    assert [r["kind"] for r in result["unread"]] == ["tessera"]
    assert {r["action"] for r in result["rows"]} == {"uguale"}
    # Un elenco che il client legge strict solo a richiesta: senza strict è vuoto, come per il backup da sempre
    controller.keyless.add("list_access_points")
    assert await controller.list_access_points() == []
    assert "access_points" in (await read_strict(controller, ["punto"]))["unread"]


async def test_keys_completed_by_the_controller_are_not_differences():
    wanted = {
        "config_lettore": {
            "rex": {
                "token": "rex",
                "DeviceUUID": "",
                "Configuration": [{"Name": "IdPoint.REX.Type", "Value": "ActiveLow"}],
            }
        },
        "config_porta": {
            "porta": {
                "token": "porta",
                "DeviceUUID": "",
                "Configuration": [
                    {"Name": "Lock.Type", "Value": "FailSecure"},
                    {"Name": "DoorMonitor.Type", "Value": "None"},
                ],
            }
        },
        "porta": {"porta": {"token": "porta", "Name": "Scala", "Description": "", "AccessTime": "PT7S"}},
        "tessera": {
            "c1": {
                "token": "c1",
                "UserToken": "u1",
                "Description": "Anna",
                "Enabled": True,
                "Status": "Enabled",
                "IdData": [{"Name": "Card", "Value": "04aa"}],
                "Attribute": [],
                "AuthenticationProfile": [],
                "ValidFrom": "2026-01-01T00:00:00Z",
                "ValidTo": "",
                "CredentialAccessProfile": [{"AccessProfile": "p1", "ValidFrom": "", "ValidTo": ""}],
            }
        },
        "fascia": {
            "s1": {
                "token": "s1",
                "Name": "Ufficio",
                "Description": "",
                "ScheduleDefinition": schedule_ical(OFFICE),
                "ExceptionScheduleDefinition": holidays_ical([{"day": "2026-12-25", "yearly": True}]),
                "Attribute": [],
            }
        },
    }
    days = [{"days": [d], "start": "08:00", "end": "18:00"} for d in OFFICE[0]["days"]]
    reading = letto(
        idpoint_configurations=[
            {
                "token": "rex",
                "DeviceUUID": "uuid",
                "Configuration": [
                    {"Name": "IdPoint.REX.Type", "Value": "ActiveLow"},
                    {"Name": "IdPoint.REX.Debounce", "Value": "50"},
                ],
            }
        ],
        door_configurations=[
            {
                "token": "porta",
                "DeviceUUID": "uuid",
                "Configuration": [{"Name": "Lock.Type", "Value": "FailSecure"}, {"Name": "Lock.Relay", "Value": "1"}],
            }
        ],
        doors=[{"token": "porta", "Name": "Scala", "AccessTime": "PT7.000S", "Priority": "Low"}],
        credentials=[
            {
                "token": "c1",
                "UserToken": "u1",
                "Description": "Anna",
                "IdData": [{"Name": "Card", "Value": "04AA"}],
                "ValidFrom": "2026-01-01T00:00:00+00:00",
                "CredentialAccessProfile": [{"AccessProfile": "p1"}],
                "Status": "Disabled",
                "Validity": "x",
            }
        ],
        schedules=[
            {
                "token": "s1",
                "Name": "Ufficio",
                # Stessi orari scritti un giorno per evento, con altre righe: stesso significato
                "ScheduleDefinition": schedule_ical(days).replace("\r\n", "\n").replace("Axis Access Manager", "x"),
                "ExceptionScheduleDefinition": "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;VALUE=DATE:20261225\n"
                "RRULE:FREQ=YEARLY;BYMONTH=12\nEND:VEVENT\nEND:VCALENDAR",
            }
        ],
    )

    result = compare(voluto(wanted), reading)

    assert [(r["key"], r["action"], r["changes"]) for r in result["rows"] if r["action"] != "uguale"] == []
    assert len(result["rows"]) == 5
    for kind, items in wanted.items():
        for key, payload in items.items():
            read = next(i for i in reading["lists"][compare_list(kind)] if i["token"] == key)
            assert signature(kind, project(kind, read, payload)) == signature(kind, payload)


def compare_list(kind):
    return {
        "config_lettore": "idpoint_configurations",
        "config_porta": "door_configurations",
        "porta": "doors",
        "tessera": "credentials",
        "fascia": "schedules",
    }[kind]


@pytest.mark.parametrize(
    ("change", "label"),
    [
        (lambda c: c["IdData"].append({"Name": "PIN", "Value": "1234"}), "Numero e PIN"),
        (lambda c: c.update(Attribute=[{"Name": "Piano", "Value": "3"}]), "Attributi"),
        (lambda c: c.update(Description="Anna Rossi"), "Descrizione"),
        (lambda c: c.update(Enabled=False), "Stato"),
        (lambda c: c.update(ValidTo="2026-01-01T00:00:00Z"), "Valida fino al"),
    ],
)
async def test_changes_made_by_hand_are_differences(plant, controller, db, change, label):  # noqa: F811
    await app_made(plant)
    card = db.card_by_number("04bb")
    change(controller.credentials[card["axis_credential_token"]])

    reading = await read_strict(controller, ["tessera"])
    result = compare(build(load_conf(db), plant.controller_id, ["tessera"]), reading)

    [row] = [r for r in result["rows"] if r["action"] != "uguale"]
    assert (row["key"], row["action"]) == (f"tessera:{card['axis_credential_token']}", "riporta")
    assert [c["label"] for c in row["changes"]] == [label]
    assert row["label"] == "Tessera 04bb (Bruno)"


async def test_duplicate_card_numbers_are_found(plant, controller, db):  # noqa: F811
    await app_made(plant)
    mine = db.card_by_number("04aa")["axis_credential_token"]
    other = controller.add_existing("utente_copia_250101120000", "Copia", "04aa")

    result = compare(build(load_conf(db), plant.controller_id, ["tessera"]), await read_strict(controller, ["tessera"]))

    rows = {r["token"]: r for r in result["rows"]}
    assert rows[other]["action"] == "togli" and rows[mine]["action"] == "uguale"
    for token in (mine, other):
        assert "doppione per numero di tessera 04aa: 2 tessere" in rows[token]["surprises"]
    assert counts(result)["surprises"] == 2


async def test_factory_objects_are_never_removed(db):
    controller = FakeController.factory()
    fields = {f: "" for f in db.CONTROLLER_FIELDS} | {"name": "A1210", "host": "a1210.local", "verify_ssl": 0}
    controller_id = db.save_controller(None, fields)

    async def configurations(*, strict=False):
        return []

    # Come l'A1210 del banco, che le configurazioni dei lettori le restituisce
    controller.list_idpoint_configurations = configurations

    result = confronto(load_conf(db), await read_strict(controller), controller_id)

    assert result["unread"] == [] and [r for r in result["rows"] if r["action"] == "togli"] == []
    assert not {r["token"] for r in result["rows"]} & {"standard_always", "standard_after_hours", "Ser0hd"}
    # Anche con lo stato voluto senza nessun oggetto: fasce standard_*, controllo accessi e morsetti di serie restano
    reading = await read_strict(controller)
    empty = {kind: {} for kind in ("fascia", "morsetto", "punto", "lettore", "porta")}
    assert [r for r in compare(voluto(empty), reading)["rows"] if r["action"] == "togli"] == []
    # Sempre cambiata: da riportare com'è di fabbrica, mai da togliere
    controller.schedules["standard_always"]["ScheduleDefinition"] = schedule_ical(OFFICE)
    [row] = compare(voluto({"fascia": {}}), await read_strict(controller, ["fascia"]))["rows"]
    assert (row["token"], row["action"]) == ("standard_always", "riporta")
    assert row["surprises"] == ["la fascia di fabbrica Sempre non vale più sempre"]


async def test_reading_and_comparing_never_write(plant, controller, db):  # noqa: F811
    await app_made(plant)
    controller.add_existing("utente_script_250101120000", "Script", "04ff")
    controller.doors["porta"]["AccessTime"] = "PT90S"
    controller.calls.clear()
    controller.attempts.clear()

    result = confronto(load_conf(db), await read_strict(controller), plant.controller_id)

    assert [r for r in result["rows"] if r["action"] == "togli"]
    assert controller.calls == [] and writes(controller) == []

    # Spento: niente righe, tutto non letto, e una sola chiamata tentata
    controller.down = True
    controller.attempts.clear()
    reading = await read_strict(controller)
    result = confronto(load_conf(db), reading, plant.controller_id)

    assert controller.attempts == ["device_info"] and controller.calls == []
    assert reading["lists"] == {} and set(reading["unread"]) == {*LISTS, "device"}
    assert result["rows"] == [] and {r["kind"] for r in result["unread"]} == {
        "dispositivo",
        "fascia",
        "porta",
        "config_porta",
        "lettore",
        "config_lettore",
        "morsetto",
        "punto",
        "gestiti",
        "pulsanti",
        "tipologia",
        "utente",
        "tessera",
    }
    assert result["notes"][0]["text"].startswith("Non si fa: elenchi non letti")


async def test_controller_lost_while_reading_leaves_the_rest_unread(plant, controller):  # noqa: F811
    controller.lose_response("list_door_configurations")

    reading = await read_strict(controller, ["porta", "config_porta", "lettore", "tessera"])

    assert list(reading["lists"]) == ["doors"]
    assert set(reading["unread"]) == {"door_configurations", "idpoints", "credentials"}
    assert "list_idpoints" not in controller.attempts and "list_credentials" not in controller.attempts
    assert controller.calls == []
    # Una seconda lettura in un altro blocco completa la prima
    merged = merge_readings(reading, await read_strict(controller, ["config_porta", "lettore", "tessera"]))
    assert set(merged["lists"]) == {"doors", "door_configurations", "idpoints", "credentials"}
    assert merged["unread"] == {}


async def test_only_the_kinds_of_the_desired_state_are_compared(plant, controller, db):  # noqa: F811
    """Un voluto per nessun tipo, o solo per le tessere, non fa diventare «da togliere» porte e utenti letti."""
    await app_made(plant)
    reading = await read_strict(controller)

    assert compare(build(load_conf(db), plant.controller_id, []), reading) == {"rows": [], "unread": []}
    result = compare(build(load_conf(db), plant.controller_id, ["tessera"]), reading)
    assert {r["kind"] for r in result["rows"]} == {"tessera"}
    assert {r["action"] for r in result["rows"]} == {"uguale"}


def test_schedules_are_compared_by_meaning():
    split = [{"days": ["MO"], "start": "08:00", "end": "12:00"}, {"days": ["MO"], "start": "12:00", "end": "18:00"}]
    assert schedule_meaning(schedule_ical(split)) == "lun 08:00–18:00"
    assert schedule_meaning(schedule_ical(OFFICE)) == "lun-ven 08:00–18:00"
    assert schedule_meaning(schedule_ical(OFFICE)) != schedule_meaning(schedule_ical(split))
    holidays = [{"day": "2026-12-25", "yearly": True}, {"day": "2026-04-06", "yearly": False}]
    assert exceptions_meaning(holidays_ical(holidays)) == "06/04/2026; 25/12/2026 ogni anno"
    assert exceptions_meaning(holidays_ical(holidays[::-1])) == exceptions_meaning(holidays_ical(holidays))


# Fasce per significato: uguale solo ciò che vale allo stesso modo

CHRISTMAS_EVE = holidays_ical([{"day": "2026-12-24", "yearly": True}])
WEEKDAYS = schedule_ical(OFFICE)


def schedule_of(definition, exceptions=CHRISTMAS_EVE):
    return {
        "token": "s1",
        "Name": "Ufficio",
        "Description": "",
        "ScheduleDefinition": definition,
        "ExceptionScheduleDefinition": exceptions,
        "Attribute": [],
    }


# Cambi fatti dal sito Axis che cambiano quando vale la fascia: (orari, festività, campo, significato letto)
CHANGED_SCHEDULES = {
    # Natale allungato fino all'Epifania: dal 25/12 al 06/01 si entrerebbe
    "festivita_allungata": (
        WEEKDAYS,
        CHRISTMAS_EVE.replace("DTEND;VALUE=DATE:20261225", "DTEND;VALUE=DATE:20270107"),
        "Festività",
        "dal 24/12/2026 al 06/01/2027 ogni anno",
    ),
    # Una volta sola invece che ogni anno
    "festivita_count": (
        WEEKDAYS,
        CHRISTMAS_EVE.replace("BYMONTH=12", "BYMONTH=12;COUNT=1"),
        "Festività",
        "24/12/2026 ogni anno per 1 volta",
    ),
    "festivita_until": (
        WEEKDAYS,
        CHRISTMAS_EVE.replace("BYMONTH=12", "BYMONTH=12;UNTIL=20281231"),
        "Festività",
        "24/12/2026 ogni anno fino al 31/12/2028",
    ),
    "festivita_exdate": (
        WEEKDAYS,
        CHRISTMAS_EVE.replace("END:VEVENT", "EXDATE;VALUE=DATE:20271224\r\nEND:VEVENT"),
        "Festività",
        "24/12/2026 ogni anno tranne il 24/12/2027",
    ),
    # In vigore solo dal 2027
    "orari_dal_2027": (
        WEEKDAYS.replace("19700101T080000", "20270101T080000").replace("19700101T180000", "20270101T180000"),
        CHRISTMAS_EVE,
        "Orari",
        "lun-ven 08:00–18:00 dal 01/01/2027",
    ),
    "orari_exdate": (
        WEEKDAYS.replace("RRULE:FREQ=WEEKLY", "EXDATE:20261109T080000\r\nRRULE:FREQ=WEEKLY"),
        CHRISTMAS_EVE,
        "Orari",
        "lun-ven 08:00–18:00 tranne il 09/11/2026 08:00",
    ),
    "orari_count": (
        WEEKDAYS.replace("BYDAY=", "COUNT=10;BYDAY="),
        CHRISTMAS_EVE,
        "Orari",
        "lun-ven 08:00–18:00 per 10 volte",
    ),
    "orari_until": (
        WEEKDAYS.replace("BYDAY=", "UNTIL=20271231T180000;BYDAY="),
        CHRISTMAS_EVE,
        "Orari",
        "lun-ven 08:00–18:00 fino al 31/12/2027 18:00",
    ),
}


@pytest.mark.parametrize(
    ("definition", "exceptions", "label", "meaning"), CHANGED_SCHEDULES.values(), ids=CHANGED_SCHEDULES
)
def test_schedule_changes_in_when_it_applies_are_differences(definition, exceptions, label, meaning):
    """Durata delle eccezioni, data di partenza, EXDATE, COUNT e UNTIL fanno parte del significato: una fascia che
    vale in modo diverso non risulta mai uguale a quella dell'app."""
    app, read = schedule_of(WEEKDAYS), schedule_of(definition, exceptions)
    assert read != app

    [row] = compare(voluto({"fascia": {"s1": app}}), letto(schedules=[read]))["rows"]

    assert row["action"] == "riporta"
    [change] = row["changes"]
    assert change["label"] == label and change["old"] == meaning
    assert change["new"] == ("lun-ven 08:00–18:00" if label == "Orari" else "24/12/2026 ogni anno")


# Orari con qualcosa che il confronto non sa interpretare
UNREADABLE_SCHEDULES = {
    "rdate": WEEKDAYS.replace("END:VEVENT", "RDATE:20261107T080000\r\nEND:VEVENT"),
    "uid": WEEKDAYS.replace("END:VEVENT", "UID:fatta-a-mano\r\nEND:VEVENT"),
    "valarm": WEEKDAYS.replace("END:VEVENT", "BEGIN:VALARM\r\nEND:VALARM\r\nEND:VEVENT"),
    "vtimezone": WEEKDAYS.replace("VERSION:2.0", "VERSION:2.0\r\nBEGIN:VTIMEZONE\r\nTZID:Europe/Rome\r\nEND:VTIMEZONE"),
    "proprieta_del_calendario": WEEKDAYS.replace("VERSION:2.0", "VERSION:2.0\r\nX-WR-TIMEZONE:Europe/Rome"),
    "tzid": WEEKDAYS.replace("DTSTART:", "DTSTART;TZID=Europe/Rome:"),
    # In UTC invece che nell'ora del controller
    "utc": WEEKDAYS.replace("19700101T080000", "19700101T080000Z"),
    # Parti della ricorrenza che l'app non scrive o che non si leggono
    "byday_numerato": WEEKDAYS.replace("BYDAY=MO,", "BYDAY=1MO,"),
    "interval": WEEKDAYS.replace("FREQ=WEEKLY", "FREQ=WEEKLY;INTERVAL=2"),
    "monthly": WEEKDAYS.replace("FREQ=WEEKLY", "FREQ=MONTHLY"),
    "due_rrule": WEEKDAYS.replace("RRULE:", "RRULE:FREQ=DAILY\r\nRRULE:"),
    "count_e_until": WEEKDAYS.replace("BYDAY=", "COUNT=3;UNTIL=20271231T180000;BYDAY="),
    # Secondi: 08:00:30 non è 08:00
    "secondi": WEEKDAYS.replace("19700101T080000", "19700101T080030"),
    # Riga piegata con LF e uno spazio: si riunisce, e BYDAY=M O non è BYDAY=MO
    "riga_piegata": WEEKDAYS.replace("BYDAY=MO", "BYDAY=M\n  O"),
}


@pytest.mark.parametrize("definition", UNREADABLE_SCHEDULES.values(), ids=UNREADABLE_SCHEDULES)
def test_schedules_the_comparison_cannot_read_are_compared_as_text(definition):
    """Qualsiasi cosa che il confronto non sa interpretare fa confrontare il testo: diverso dalla fascia dell'app,
    uguale solo a un testo uguale (anche con altri fine riga)."""
    app, read = schedule_of(WEEKDAYS), schedule_of(definition)

    [row] = compare(voluto({"fascia": {"s1": app}}), letto(schedules=[read]))["rows"]

    assert (row["action"], [c["label"] for c in row["changes"]]) == ("riporta", ["Orari"])
    assert schedule_meaning(definition).startswith("BEGIN:VCALENDAR")
    same = schedule_of(definition.replace("\r\n", "\n"))
    [row] = compare(voluto({"fascia": {"s1": read}}), letto(schedules=[same]))["rows"]
    assert row["action"] == "uguale"


# Festività con qualcosa che il confronto non sa interpretare
UNREADABLE_HOLIDAYS = {
    "senza_value_date": CHRISTMAS_EVE.replace(";VALUE=DATE:20261224", ":20261224"),
    "dtend_con_ora": CHRISTMAS_EVE.replace("DTEND;VALUE=DATE:20261225", "DTEND:20261225T000000"),
    "dtend_non_dopo": CHRISTMAS_EVE.replace("DTEND;VALUE=DATE:20261225", "DTEND;VALUE=DATE:20261224"),
    "bymonth_diverso": CHRISTMAS_EVE.replace("BYMONTH=12", "BYMONTH=1"),
    "bymonthday": CHRISTMAS_EVE.replace("BYMONTH=12", "BYMONTH=12;BYMONTHDAY=24"),
    "interval": CHRISTMAS_EVE.replace("BYMONTH=12", "BYMONTH=12;INTERVAL=2"),
    "rdate": CHRISTMAS_EVE.replace("END:VEVENT", "RDATE;VALUE=DATE:20270101\r\nEND:VEVENT"),
    "exdate_con_ora": CHRISTMAS_EVE.replace("END:VEVENT", "EXDATE:20271224T000000\r\nEND:VEVENT"),
    # Non è un calendario: non si confonde con il significato «24/12/2026 ogni anno»
    "non_calendario": "24/12/2026 ogni anno",
}


@pytest.mark.parametrize("exceptions", UNREADABLE_HOLIDAYS.values(), ids=UNREADABLE_HOLIDAYS)
def test_holidays_the_comparison_cannot_read_are_compared_as_text(exceptions):
    app, read = schedule_of(WEEKDAYS), schedule_of(WEEKDAYS, exceptions)

    [row] = compare(voluto({"fascia": {"s1": app}}), letto(schedules=[read]))["rows"]

    assert (row["action"], [c["label"] for c in row["changes"]]) == ("riporta", ["Festività"])
    assert exceptions_meaning(exceptions).startswith(("BEGIN:VCALENDAR", "testo: "))


SPLIT = [{"days": ["MO"], "start": "08:00", "end": "12:00"}, {"days": ["MO"], "start": "13:00", "end": "18:00"}]
APP_RULES = [
    OFFICE,
    SPLIT,
    [{"days": ["SA", "SU"], "start": "00:00", "end": "24:00"}],
    [{"days": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"], "start": "00:00", "end": "24:00"}],
    [{"days": ["MO", "FR"], "start": "08:30", "end": "24:00"}, {"days": ["SA"], "start": "09:00", "end": "12:30"}],
]
APP_HOLIDAYS = [
    [],
    italian_holidays([2026, 2027]),
    [{"day": "2028-02-29", "yearly": True}, {"day": "2026-11-09", "yearly": False}, *italian_holidays([2026])],
]


def reversed_events(text):
    """Lo stesso calendario con gli eventi in ordine inverso."""
    if not text:
        return text
    events = re.findall(r"BEGIN:VEVENT.*?END:VEVENT\r\n", text, re.S)
    first, last = text.index("BEGIN:VEVENT"), text.rindex("END:VEVENT\r\n") + len("END:VEVENT\r\n")
    return text[:first] + "".join(events[::-1]) + text[last:]


@pytest.mark.parametrize("rules", APP_RULES)
@pytest.mark.parametrize("holidays", APP_HOLIDAYS)
async def test_schedules_written_by_the_app_stay_equal(rules, holidays):
    """Le fasce dell'app (orari e festività di serie) si leggono per significato: uguali a se stesse e a come le
    restituisce il controller (il finto: com'è stata scritta), anche con altri fine riga e gli eventi in un altro
    ordine."""
    app = schedule_payload("s1", "Ufficio", "", rules, bool(holidays), holidays)
    controller = FakeController()
    await controller.set_schedule(app)
    read = next(s for s in await controller.list_schedules() if s["token"] == "s1")
    shuffled = {
        **read,
        "ScheduleDefinition": reversed_events(app["ScheduleDefinition"]).replace("\r\n", "\n"),
        "ExceptionScheduleDefinition": reversed_events(app["ExceptionScheduleDefinition"]),
    }
    assert len(rules) == 1 or shuffled["ScheduleDefinition"] != app["ScheduleDefinition"].replace("\r\n", "\n")

    for current in (app, read, shuffled):
        [row] = compare(voluto({"fascia": {"s1": app}}), letto(schedules=[current]))["rows"]
        assert (row["action"], row["changes"]) == ("uguale", [])
    assert not schedule_meaning(app["ScheduleDefinition"]).startswith(("BEGIN", "testo"))
    assert not exceptions_meaning(app["ExceptionScheduleDefinition"]).startswith(("BEGIN", "testo"))
    assert signature("fascia", project("fascia", read, app)) == signature("fascia", app)


async def test_schedule_changed_by_hand_is_a_difference_after_the_presa(plant, controller, db):  # noqa: F811
    """La fascia Ufficio dell'app, con Natale tra le festività: senza differenze com'è stata scritta; con l'eccezione
    allungata o una data di partenza messe dal sito Axis, da riportare."""
    await app_made(plant)
    token = next(t for t, s in controller.schedules.items() if s.get("Name") == "Ufficio")

    async def row():
        result = confronto(load_conf(db), await read_strict(controller), plant.controller_id)
        return next(r for r in result["rows"] if r["key"] == f"fascia:{token}")

    # Presa in carico com'è: uguale a come la vuole l'app
    assert ((found := await row())["action"], found["changes"]) == ("prende", [])

    schedule = controller.schedules[token]
    written = dict(schedule)
    schedule["ExceptionScheduleDefinition"] = written["ExceptionScheduleDefinition"].replace(
        "DTEND;VALUE=DATE:20261226", "DTEND;VALUE=DATE:20270107"
    )
    found = await row()
    assert found["action"] == "riporta"
    assert found["changes"] == [
        {"label": "Festività", "old": "dal 25/12/2026 al 06/01/2027 ogni anno", "new": "25/12/2026 ogni anno"}
    ]

    schedule["ExceptionScheduleDefinition"] = written["ExceptionScheduleDefinition"]
    schedule["ScheduleDefinition"] = written["ScheduleDefinition"].replace("19700101T", "20270104T")
    found = await row()
    assert found["changes"] == [
        {"label": "Orari", "old": "lun-ven 08:00–18:00 dal 04/01/2027", "new": "lun-ven 08:00–18:00"}
    ]
