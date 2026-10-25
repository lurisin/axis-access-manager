"""Regole di accesso: fasce orarie settimanali, festività e validità delle tessere in formato Axis."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

DAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
DAY_NAMES = {"MO": "lun", "TU": "mar", "WE": "mer", "TH": "gio", "FR": "ven", "SA": "sab", "SU": "dom"}
TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$|^24:00$")

# Fasce predefinite del controller (Sempre, Orario d'ufficio, Fine settimana)
STANDARD_PREFIX = "standard_"

ITALIAN_HOLIDAYS = [
    ("01-01", "Capodanno"),
    ("01-06", "Epifania"),
    ("04-25", "Festa della Liberazione"),
    ("05-01", "Festa dei lavoratori"),
    ("06-02", "Festa della Repubblica"),
    ("08-15", "Ferragosto"),
    ("11-01", "Ognissanti"),
    ("12-08", "Immacolata Concezione"),
    ("12-25", "Natale"),
    ("12-26", "Santo Stefano"),
]


class RuleError(ValueError):
    """Regola non valida: il messaggio va mostrato all'utente."""


def normalize_rules(rules: list[dict]) -> list[dict]:
    """Controlla le fasce settimanali [{days: [MO...], start: "08:00", end: "18:00"}]."""
    if not rules:
        raise RuleError("aggiungi almeno un intervallo")
    if len(rules) > 20:
        raise RuleError("massimo 20 intervalli per fascia oraria")
    result = []
    for rule in rules:
        days = [d for d in DAYS if d in set(rule.get("days") or [])]
        if not days:
            raise RuleError("ogni intervallo deve avere almeno un giorno")
        start, end = str(rule.get("start") or ""), str(rule.get("end") or "")
        if not TIME_PATTERN.match(start) or start == "24:00":
            raise RuleError(f"ora di inizio non valida: {start or 'vuota'}")
        if not TIME_PATTERN.match(end):
            raise RuleError(f"ora di fine non valida: {end or 'vuota'}")
        if end <= start:
            raise RuleError(f"l'intervallo {start}-{end} deve finire dopo l'inizio, nello stesso giorno")
        result.append({"days": days, "start": start, "end": end})
    return result


def describe_rules(rules: list[dict]) -> str:
    """Testo breve, ad esempio "lun-ven 08:00-18:00; sab 09:00-12:00"."""
    parts = []
    for rule in rules:
        parts.append(f"{_describe_days(rule['days'])} {rule['start']}-{rule['end']}")
    return "; ".join(parts)


def _describe_days(days: list[str]) -> str:
    indexes = [DAYS.index(d) for d in days]
    if indexes == list(range(7)):
        return "tutti i giorni"
    if len(indexes) > 2 and indexes == list(range(indexes[0], indexes[-1] + 1)):
        return f"{DAY_NAMES[days[0]]}-{DAY_NAMES[days[-1]]}"
    return ", ".join(DAY_NAMES[d] for d in days)


def _ical_time(value: str) -> str:
    return value.replace(":", "") + "00"


def _calendar(events: list[str]) -> str:
    lines = ["BEGIN:VCALENDAR", "PRODID:Axis Access Manager", "VERSION:2.0", *events, "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def schedule_ical(rules: list[dict]) -> str:
    """Calendario iCalendar in ora locale del controller, come negli esempi Axis (eventi settimanali)."""
    events = []
    for rule in rules:
        # 1 gennaio 1970: data di partenza delle ricorrenze, come nella documentazione Axis
        end = "19700102T000000" if rule["end"] == "24:00" else f"19700101T{_ical_time(rule['end'])}"
        events += [
            "BEGIN:VEVENT",
            f"DTSTART:19700101T{_ical_time(rule['start'])}",
            f"DTEND:{end}",
            f"RRULE:FREQ=WEEKLY;BYDAY={','.join(rule['days'])}",
            "END:VEVENT",
        ]
    return _calendar(events)


def parse_schedule_ical(text: str | None) -> list[dict] | None:
    """Intervalli settimanali di un calendario Axis; None se usa regole che l'editor non sa mostrare."""
    if not text:
        return None
    rules = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text.replace("\r\n ", ""), re.S):
        props: dict[str, str] = {}
        for line in block.strip().splitlines():
            key, _, value = line.strip().partition(":")
            props[key.split(";")[0].upper()] = value.strip()
        try:
            start = datetime.strptime(props["DTSTART"][:15], "%Y%m%dT%H%M%S")
            end = datetime.strptime(props["DTEND"][:15], "%Y%m%dT%H%M%S")
        except (KeyError, ValueError):
            return None
        if end <= start or end - start > timedelta(days=1):
            return None
        if end.date() != start.date() and end.time() != datetime.min.time():
            return None
        rrule = dict(part.split("=", 1) for part in props.get("RRULE", "").split(";") if "=" in part)
        freq = rrule.get("FREQ", "")
        if freq == "DAILY" and not rrule.get("BYDAY"):
            days = list(DAYS)
        elif freq in ("WEEKLY", "DAILY"):
            days = [d for d in DAYS if d in rrule.get("BYDAY", DAYS[start.weekday()]).split(",")]
        else:
            return None
        if set(rrule) - {"FREQ", "BYDAY", "INTERVAL", "WKST"} or rrule.get("INTERVAL", "1") != "1" or not days:
            return None
        rules.append(
            {
                "days": days,
                "start": start.strftime("%H:%M"),
                "end": "24:00" if end.date() != start.date() else end.strftime("%H:%M"),
            }
        )
    return rules or None


def holidays_ical(holidays: list[dict]) -> str | None:
    """Eccezioni: giornate intere in cui la fascia non vale. None se non ci sono festività."""
    if not holidays:
        return None
    events = []
    for holiday in holidays:
        day = date.fromisoformat(holiday["day"])
        events += [
            "BEGIN:VEVENT",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
        ]
        if holiday.get("yearly"):
            # Lo stesso giorno ogni anno: il giorno viene da DTSTART, l'A1210 non accetta BYMONTHDAY
            events.append(f"RRULE:FREQ=YEARLY;BYMONTH={day.month}")
        events.append("END:VEVENT")
    return _calendar(events)


def easter(year: int) -> date:
    """Domenica di Pasqua (algoritmo di Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(year, month, day)


def italian_holidays(years: list[int]) -> list[dict]:
    """Festività nazionali: quelle fisse ogni anno, la Pasquetta per gli anni indicati."""
    base_year = years[0]
    holidays = [{"day": f"{base_year}-{md}", "name": name, "yearly": True} for md, name in ITALIAN_HOLIDAYS]
    for year in years:
        holidays.append(
            {"day": (easter(year) + timedelta(days=1)).isoformat(), "name": "Lunedì dell'Angelo", "yearly": False}
        )
    return holidays


def normalize_holiday(day: str, name: str) -> tuple[str, str]:
    try:
        parsed = date.fromisoformat((day or "").strip())
    except ValueError as err:
        raise RuleError(f"data non valida: {day or 'vuota'}") from err
    name = " ".join((name or "").split())[:60]
    return parsed.isoformat(), name


def normalize_validity(value: str | None) -> str:
    """Data e ora di validità in formato Axis (UTC, YYYY-MM-DDThh:mm:ssZ); vuoto = nessun limite."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as err:
        raise RuleError(f"data di validità non valida: {value}") from err
    if parsed.tzinfo is None:
        # Senza fuso si intende l'ora locale dell'add-on
        parsed = parsed.astimezone()
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_validity(valid_from: str, valid_to: str) -> None:
    if valid_from and valid_to and valid_to <= valid_from:
        raise RuleError("la fine della validità deve essere dopo l'inizio")


def validity_state(valid_from: str, valid_to: str, now: datetime | None = None) -> str:
    """'' (valida senza limiti o nel periodo), 'futura' o 'scaduta'."""
    now = now or datetime.now(tz=UTC)
    try:
        if valid_from and _parse_utc(valid_from) > now:
            return "futura"
        if valid_to and _parse_utc(valid_to) <= now:
            return "scaduta"
    except ValueError:
        return ""
    return ""


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
