from datetime import UTC, date, datetime

import pytest

from app.rules import (
    RuleError,
    check_validity,
    describe_rules,
    easter,
    holidays_ical,
    italian_holidays,
    normalize_rules,
    normalize_validity,
    schedule_ical,
    validity_state,
)


def test_normalize_rules_orders_days_and_checks_times():
    rules = normalize_rules([{"days": ["FR", "MO", "XX"], "start": "08:00", "end": "24:00"}])
    assert rules == [{"days": ["MO", "FR"], "start": "08:00", "end": "24:00"}]
    with pytest.raises(RuleError, match="dopo l'inizio"):
        normalize_rules([{"days": ["MO"], "start": "18:00", "end": "08:00"}])
    with pytest.raises(RuleError, match="almeno un giorno"):
        normalize_rules([{"days": [], "start": "08:00", "end": "09:00"}])
    with pytest.raises(RuleError):
        normalize_rules([{"days": ["MO"], "start": "8", "end": "09:00"}])
    with pytest.raises(RuleError):
        normalize_rules([])


def test_describe_rules():
    rules = [
        {"days": ["MO", "TU", "WE", "TH", "FR"], "start": "08:00", "end": "18:00"},
        {"days": ["SA", "SU"], "start": "09:00", "end": "12:00"},
        {"days": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"], "start": "00:00", "end": "24:00"},
    ]
    assert describe_rules(rules) == "lun-ven 08:00–18:00; sab, dom 09:00–12:00; tutti i giorni 00:00–24:00"


def test_schedule_ical_uses_weekly_local_events():
    ical = schedule_ical([{"days": ["MO", "FR"], "start": "08:30", "end": "24:00"}])
    assert ical.startswith("BEGIN:VCALENDAR\r\n")
    assert "DTSTART:19700101T083000\r\n" in ical
    assert "DTEND:19700102T000000\r\n" in ical
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO,FR\r\n" in ical
    assert "Z\r\n" not in ical


def test_holidays_ical():
    assert holidays_ical([]) is None
    ical = holidays_ical([{"day": "2026-12-25", "yearly": True}, {"day": "2026-04-06", "yearly": False}])
    assert "DTSTART;VALUE=DATE:20261225\r\nDTEND;VALUE=DATE:20261226\r\n" in ical
    assert "RRULE:FREQ=YEARLY;BYMONTH=12\r\n" in ical and "BYMONTHDAY" not in ical
    assert ical.count("RRULE") == 1


def test_easter_and_italian_holidays():
    assert easter(2026) == date(2026, 4, 5)
    assert easter(2027) == date(2027, 3, 28)
    holidays = italian_holidays([2026, 2027])
    assert {"day": "2026-04-06", "name": "Lunedì dell'Angelo", "yearly": False} in holidays
    assert {"day": "2026-12-25", "name": "Natale", "yearly": True} in holidays


def test_normalize_validity_converts_to_utc():
    assert normalize_validity("") == ""
    assert normalize_validity("2026-10-25T10:00:00+02:00") == "2026-10-25T08:00:00Z"
    assert normalize_validity("2026-10-25T08:00:00.000Z") == "2026-10-25T08:00:00Z"
    with pytest.raises(RuleError):
        normalize_validity("domani")
    with pytest.raises(RuleError):
        check_validity("2026-10-25T08:00:00Z", "2026-10-24T08:00:00Z")


def test_validity_state():
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert validity_state("", "", now) == ""
    assert validity_state("2026-10-26T00:00:00Z", "", now) == "futura"
    assert validity_state("", "2026-10-25T11:00:00Z", now) == "scaduta"
    assert validity_state("", "2026-10-25T13:00:00", now) == ""
