"""Orari della pagina nel fuso dell'impianto (collaudo 0.28.0, punto F): le funzioni delle date di index.html, prese
dalla pagina com'è e fatte girare con node in un browser finto in un altro fuso (Europe/Athens, come il computer del
collaudo). Il fuso dell'impianto è quello di Home Assistant che la pagina riceve da api/me."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.health import ha_time_zone
from app.main import create_app

PAGE = Path(__file__).resolve().parents[1] / "axis_access_manager" / "app" / "static" / "index.html"
# Dichiarazioni della pagina che servono alle funzioni delle date (quelle che non ci sono si saltano)
NAMES = (
    "pad",
    "zoneChecked",
    "partsFormat",
    "plantZone",
    "zoned",
    "zoneParts",
    "plantDay",
    "shiftDay",
    "fmtDay",
    "fmtDateTime",
    "fmtFull",
    "fmtDate",
    "fmtTime",
    "relTime",
    "toLocalInput",
    "wallOf",
    "fromLocalInput",
    "dayTime",
    "aheadTime",
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="serve node")


def _declarations() -> str:
    page = PAGE.read_text(encoding="utf-8")
    found = []
    for name in NAMES:
        block = re.search(rf"^function {name}\(.*?^\}}$", page, re.M | re.S) or re.search(
            rf"^(?:const|let) {name} = .*$", page, re.M
        )
        if block:
            found.append(block.group(0))
    return "\n".join(found)


def run_page(zone: str, calls: dict[str, str], browser: str = "Europe/Athens", now: str = "") -> dict:
    """Valuta le espressioni calls con le funzioni della pagina: zone è il fuso di Home Assistant (api/me), browser
    quello del computer, now l'ora di adesso (ISO)."""
    lines = [
        f"const store = {{me: {{time_zone: {json.dumps(zone)}}}}};",
        f"if ({json.dumps(now)}) {{ const fixed = Date.parse({json.dumps(now)}); Date.now = () => fixed; }}",
        _declarations(),
        "const out = {};",
    ]
    for key, expression in calls.items():
        name = json.dumps(key)
        lines.append(f"try {{ out[{name}] = {expression}; }} catch (e) {{ out[{name}] = 'ERRORE ' + e; }}")
    script = "\n".join([*lines, "console.log(JSON.stringify(out));"])
    result = subprocess.run(
        ["node", "-e", script], env={**os.environ, "TZ": browser}, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_times_are_shown_in_the_plant_zone_from_a_browser_in_athens():
    """Gli esempi del collaudo: l'ultimo evento delle 19:04 di Roma e il coperchio aperto alle 17:18 si vedevano alle
    20:04 e alle 18:18 su un computer in Europe/Athens."""
    out = run_page(
        "Europe/Rome",
        {
            "event": 'fmtTime("2026-11-07T18:04:57+00:00")',
            "casing": 'fmtDateTime("2026-11-03T16:18:32Z")',
            "full": 'fmtFull("2026-11-07T18:04:57Z")',
            "date": 'fmtDate("2026-11-07T23:30:00Z")',
            "summer": 'fmtTime("2026-07-01T16:00:00Z")',
        },
    )
    assert out == {
        "event": "19:04",
        "casing": "03/11/2026 17:18",
        "full": "7 nov 2026, 19:04:57",
        "date": "8 nov 2026",
        "summer": "18:00",
    }


def test_today_and_yesterday_are_days_of_the_plant():
    """Alle 23:30 di Roma ad Atene è già domani: «oggi», «ieri», «domani» e le righe del registro seguono Roma."""
    now = "2026-11-08T22:30:00Z"
    out = run_page(
        "Europe/Rome",
        {
            "today": "plantDay()",
            "event": 'dayTime("2026-11-08T08:00:00Z")',
            "yesterday": 'dayTime("2026-11-07T22:59:00Z")',
            "older": 'dayTime("2026-11-06T22:59:00Z")',
            "tomorrow": 'aheadTime("2026-11-09T02:00:00Z")',
            "later": 'aheadTime("2026-11-08T22:45:00Z")',
            "recent": 'relTime("2026-11-08T22:25:00Z")',
        },
        now=now,
    )
    assert out == {
        "today": "2026-11-08",
        "event": "oggi 09:00",
        "yesterday": "ieri 23:59",
        "older": "06/11/2026 23:59",
        "tomorrow": "domani alle 03:00",
        "later": "alle 23:45",
        "recent": "5 min fa",
    }


def test_validity_written_in_the_page_is_in_the_plant_zone():
    """Una validità «fino alle 18:00» scritta da Atene è alle 18:00 di Roma, e si rilegge uguale; anche nei giorni
    del cambio dell'ora (l'ora che non c'è diventa quella dopo, quella doppia la prima, come fa il browser)."""
    out = run_page(
        "Europe/Rome",
        {
            "winter": 'fromLocalInput("2026-11-08T18:00")',
            "summer": 'fromLocalInput("2026-07-01T18:00")',
            "back": 'toLocalInput("2026-11-08T17:00:00Z")',
            "round": 'toLocalInput(fromLocalInput("2027-01-31T23:59"))',
            "midnight": 'toLocalInput("2026-11-08T23:00:00Z")',
            "missing": 'fromLocalInput("2026-03-29T02:30")',
            "twice": 'fromLocalInput("2026-10-25T02:30")',
            "empty": 'fromLocalInput("") + toLocalInput("")',
        },
    )
    assert out == {
        "winter": "2026-11-08T17:00:00.000Z",
        "summer": "2026-07-01T16:00:00.000Z",
        "back": "2026-11-08T18:00",
        "round": "2027-01-31T23:59",
        "midnight": "2026-11-09T00:00",
        "missing": "2026-03-29T01:30:00.000Z",
        "twice": "2026-10-25T00:30:00.000Z",
        "empty": "",
    }


@pytest.mark.parametrize("zone", ["", "Fuso/Inesistente"])
def test_without_a_known_zone_the_browser_zone_as_before(zone):
    """Fuso di Home Assistant sconosciuto (o che il browser non conosce): il fuso del browser, come prima."""
    out = run_page(
        zone,
        {
            "event": 'fmtTime("2026-11-07T18:04:57Z")',
            "written": 'fromLocalInput("2026-11-08T18:00")',
            "shown": 'toLocalInput("2026-11-08T16:00:00Z")',
        },
    )
    assert out == {"event": "20:04", "written": "2026-11-08T16:00:00.000Z", "shown": "2026-11-08T18:00"}


@pytest.mark.parametrize("browser", ["Europe/Athens", "Pacific/Kiritimati", "Pacific/Pago_Pago"])
def test_calendar_days_are_the_same_in_every_zone(browser):
    """Festività e colonne della settimana sono giorni, non istanti: lo stesso giorno da ogni browser."""
    out = run_page(
        "Europe/Rome",
        {
            "week": 'fmtDay("2026-11-08", {weekday: "short"})',
            "holiday": 'fmtDay("2026-12-25", {day: "numeric", month: "long", year: "numeric"})',
            "shift": 'shiftDay("2026-03-28", 1) + " " + shiftDay("2026-11-01", -1)',
        },
        browser=browser,
    )
    assert out == {"week": "dom", "holiday": "25 dicembre 2026", "shift": "2026-03-29 2026-10-31"}


def test_the_page_receives_the_zone_of_home_assistant(settings, controller, tmp_path, monkeypatch):
    """api/me dà alla pagina il fuso di Home Assistant (TZ dell'add-on), quello della scheda Stato."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setenv("TZ", "Europe/Rome")
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "z.db")
    with TestClient(app) as client:
        assert client.get("/api/me").json()["time_zone"] == ha_time_zone() == "Europe/Rome"
