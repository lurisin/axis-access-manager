"""Finestra del videocitofono e scheda Videocitofoni (0.28.11): il videocitofono si sceglie tra quelli annunciati sul
broker dall'add-on Hikvision Doorbell, invece di scriverne il nome, e la porta salva anche il numero di serie. Il 17/11
il nome scritto nell'app non era più quello dell'add-on e per un giorno chiamate e aperture non sono arrivate.

Prima le righe della scelta calcolate dalla pagina com'è (node), poi la pagina vera in Chromium con l'app collegata a un
broker in memoria. Solo il FakeController; nomi e numeri di serie di fantasia."""

import json
import socket
import subprocess

import pytest
import uvicorn
from test_direct import ADMIN
from test_intercom_directory import ARTISTI, CARRAIO, SERIAL_A, SERIAL_B
from test_intercom_serial import site, wait  # noqa: F401
from test_page_morph import _has_playwright
from test_page_time_zone import needs_node, run_page

from app.direct import _Server

NAMES = ("named", "NO_INTERCOM", "INTERCOM_WAIT", "INTERCOM_CURRENT", "intercomRows", "intercomEmpty", "doorBody")
MODEL = "DS-KX0000-PROVA"
# Elenco di api/intercoms/found: l'Ingresso Artisti è delle porte 1 (relè 1) e 2 (relè 2), il Cancello Carraio libero
FOUND = {
    "broker": True,
    "since": 42.0,
    "devices": [
        {"serial": SERIAL_B, "name": CARRAIO, "names": [CARRAIO], "model": MODEL, "relays": [1, 2], "used": []},
        {
            "serial": SERIAL_A,
            "name": ARTISTI,
            "names": [ARTISTI],
            "model": MODEL,
            "relays": [1, 2],
            "used": [
                {"door_id": 1, "door": "Palcoscenico", "relay": 1},
                {"door_id": 2, "door": "Porta Cantina", "relay": 2},
            ],
        },
    ],
}


def _rows(rows):
    """Righe in breve: nome, sotto il nome, grigia, Non Trovato, relè (numero e porta che lo usa, se non si può)."""
    return [
        (r["name"], r["meta"], r["off"], r["missing"], [(x["n"], x["by"] if x["off"] else "") for x in r["relays"]])
        for r in rows
    ]


@needs_node
def test_the_choice_greys_the_intercoms_whose_relays_other_doors_use():
    second = {"id": 2, "intercom_name": ARTISTI, "intercom_serial": SERIAL_A, "intercom_relay": 2}
    legacy = {"id": 3, "intercom_name": "Citofono Sottopalco", "intercom_serial": "", "intercom_relay": 2}
    found = json.dumps(FOUND)
    out = run_page(
        "Europe/Rome",
        {
            # Aggiunto alla porta 3: i due relè dell'Ingresso Artisti sono già delle porte 1 e 2
            "add": f"intercomRows({found}, undefined, 3)",
            # Quello della porta 2: scelto (lo stesso numero di serie), il relè 1 resta della porta 1
            "edit": f"intercomRows({found}, {json.dumps(second)}, 2)",
            # Porta 2 spostata sulla porta 1: il videocitofono della porta 1 si sostituisce, i relè tornano liberi
            "moved": f"intercomRows({found}, {json.dumps(second)}, 1)",
            # Nome di prima della 0.28.11 che il broker non annuncia: per primo, scelto, Non Trovato
            "legacy": f"intercomRows({found}, {json.dumps(legacy)}, 3).map((r) => [r.value, r.serial, r.current])",
            "legacy_rows": f"intercomRows({found}, {json.dumps(legacy)}, 3)",
            # Broker scollegato: l'elenco è vuoto, ma non si può dire che non c'è
            "legacy_offline": f"intercomRows({{broker: false, since: null, devices: []}}, {json.dumps(legacy)}, 3)",
            "empty": "[null, {broker: false, since: null, devices: []}, {broker: true, since: 1, devices: []},"
            f" {{broker: true, since: 12, devices: []}}, {found}].map(intercomEmpty)",
            # Ogni porta salvata porta il numero di serie; tolto il videocitofono, anche il numero di serie
            "body": f"doorBody({{name: 'Cantina', door_token: 'porta2', intercom_name: '{ARTISTI}',"
            f" intercom_serial: '{SERIAL_A}', intercom_relay: 2}})",
            "removed": f"doorBody({{name: 'Cantina', door_token: 'porta2', intercom_name: '{ARTISTI}',"
            f" intercom_serial: '{SERIAL_A}'}}, NO_INTERCOM)",
        },
        names=NAMES,
    )
    assert _rows(out["add"]) == [
        (CARRAIO, MODEL, False, False, [(1, ""), (2, "")]),
        (ARTISTI, "Porta Palcoscenico, Porta Cantina", True, False, [(1, "Porta Palcoscenico"), (2, "Porta Cantina")]),
    ]
    assert _rows(out["edit"]) == [
        (CARRAIO, MODEL, False, False, [(1, ""), (2, "")]),
        (ARTISTI, MODEL, False, False, [(1, "Porta Palcoscenico"), (2, "")]),
    ]
    assert [r["current"] for r in out["edit"]] == [False, True]
    assert _rows(out["moved"]) == [
        (CARRAIO, MODEL, False, False, [(1, ""), (2, "")]),
        (ARTISTI, MODEL, False, False, [(1, ""), (2, "")]),
    ]
    assert out["legacy"] == [["-", "", True], [SERIAL_B, SERIAL_B, False], [SERIAL_A, SERIAL_A, False]]
    assert _rows(out["legacy_rows"])[0] == ("Citofono Sottopalco", "", False, True, [(1, ""), (2, "")])
    assert _rows(out["legacy_offline"]) == [("Citofono Sottopalco", "", False, False, [(1, ""), (2, "")])]
    assert out["empty"] == [
        "Elenco non disponibile.",
        "Broker MQTT non collegato.",
        '<span class="spinner" aria-label="Ricerca"></span>',
        "Nessun videocitofono trovato.",
        "",
    ]
    assert out["body"]["intercom_serial"] == SERIAL_A
    assert out["removed"] == {
        "name": "Cantina",
        "door_token": "porta2",
        "intercom_name": "",
        "intercom_serial": "",
        "intercom_open_door": False,
    }


FLOW = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({viewport: {width: 390, height: 844}, extraHTTPHeaders: config.headers});
  const page = await context.newPage();
  const out = {errors: [], puts: []};
  page.on("pageerror", (e) => out.errors.push(String(e)));
  page.on("response", async (r) => {
    if (r.request().method() === "PUT" && r.url().includes("/api/doors/")) {
      out.puts.push({door: r.url().split("/").pop(), status: r.status(), body: JSON.parse(r.request().postData())});
    }
  });
  const tab = `${config.base}/#impianti/c${config.controller}/citofoni`;
  const rows = () => page.$$eval("#view .list > .item", (items) => items.map((i) => ({
    title: i.querySelector(".title").textContent.replace(/\s+/g, " ").trim(),
    meta: [...i.querySelectorAll(".meta span")].map((s) => s.textContent)})));
  const choices = () => page.$$eval("#modal [data-ic-list] > *", (items) => items.map((i) => ({
    text: i.textContent.replace(/\s+/g, " ").trim(), checked: Boolean(i.querySelector("input:checked")),
    disabled: Boolean(i.querySelector("input:disabled"))})));
  const relays = () => page.$$eval("#modal [name=intercom_relay] option",
    (os) => os.map((o) => [o.textContent, o.disabled, o.selected]));
  await page.goto(tab);
  await page.waitForSelector("[data-ic-edit]");
  out.tab = await rows();
  // Finestra del Palcoscenico: il fuoco sul pallino scelto (il secondo), non sul primo
  await page.click(`[data-ic-edit="${config.door}"]`);
  await page.waitForSelector("#modal[open] [data-ic-list] label");
  out.focus = await page.evaluate(() => {
    const el = document.activeElement;
    return el?.matches("[name=intercom]") ? `${el.value}${el.checked ? "" : " (non scelto)"}` : el?.tagName || "";
  });
  await page.click("#modal [data-x]");
  await page.waitForSelector("#modal", {state: "hidden"});
  // Porta della cantina: il suo videocitofono non c'è, sceglie l'Ingresso Artisti (relè 1 già dell'altra porta)
  await page.click(`[data-ic-edit="${config.cellar}"]`);
  await page.waitForSelector("#modal[open] [data-ic-list] label");
  out.dialog = await choices();
  out.dialogRelays = await relays();
  out.dialogScroll = await page.$eval("#modal .dlg-body", (b) => b.scrollWidth > b.clientWidth);
  await page.check(`#modal [name=intercom][value="${config.serial}"]`);
  out.chosenRelays = await relays();
  await page.click("#modal .dlg-foot button.primary");
  await page.waitForSelector("#modal", {state: "hidden"});
  // La scheda rifatta dopo il salvataggio: tutte e due le porte con l'Ingresso Artisti
  await page.waitForFunction((name) => [...document.querySelectorAll("#view .list > .item .title")]
    .filter((t) => t.textContent.trim() === name).length === 2, config.name);
  // Togli il videocitofono legato per numero di serie
  await page.click(`[data-ic-del="${config.door}"]`);
  await page.waitForSelector("#modal[open] .dlg-foot button.primary");
  await page.click("#modal .dlg-foot button.primary");
  await page.waitForFunction(() => document.querySelectorAll("#view .list > .item").length === 1);
  // Broker scollegato: una riga sola
  const offline = {broker: false, since: null, devices: []};
  await page.route("**/api/intercoms/found", (route) => route.fulfill({json: offline}));
  await page.click("#ic-new");
  await page.waitForSelector("#modal[open] [data-ic-list] .ic-empty");
  out.offline = await choices();
  await page.click("#modal [data-x]");
  await page.waitForSelector("#modal", {state: "hidden"});
  // Appena collegati: la rotellina, e dopo qualche secondo l'elenco chiesto di nuovo (questa volta dall'app vera). Il
  // videocitofono della cantina, prima non ancora annunciato, resta scelto quando arriva
  await page.unroute("**/api/intercoms/found");
  let asked = 0;
  await page.route("**/api/intercoms/found", (route) => {
    asked += 1;
    return asked === 1 ? route.fulfill({json: {broker: true, since: 0.5, devices: []}}) : route.continue();
  });
  await page.click(`[data-ic-edit="${config.cellar}"]`);
  await page.waitForSelector("#modal[open] [data-ic-list] .ic-empty .spinner");
  out.waiting = await choices();
  await page.waitForFunction(() => document.querySelectorAll("#modal [data-ic-list] label").length === 2, null,
    {timeout: 8000});
  out.refetched = await choices();
  out.refetchedRelays = await relays();
  out.asked = asked;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture
def served(site):  # noqa: F811
    """L'app sul broker in memoria, servita davvero per il browser: Palcoscenico con il nome dell'Ingresso Artisti
    (preso per numero di serie dal nome) e Cantina con un videocitofono che il broker non annuncia."""
    site.retain(ARTISTI, SERIAL_A)
    site.retain(CARRAIO, SERIAL_B)
    client = site.start(intercom_name=ARTISTI)
    assert wait(lambda: site.door()["intercom_serial"] == SERIAL_A)
    stage = {**site.door(), "name": "Palcoscenico"}
    response = client.put(f"/api/doors/{stage['id']}", json=stage, headers=ADMIN)
    assert response.status_code == 200, response.text
    door = site.door()
    assert (door["name"], door["intercom_serial"]) == ("Palcoscenico", SERIAL_A)
    cellar = {"door_token": "porta2", "name": "Cantina", "intercom_name": "Citofono Cantina"}
    response = client.post(f"/api/controllers/{door['controller_id']}/doors", json=cellar, headers=ADMIN)
    assert response.status_code == 200, response.text
    state = site.app.state
    assert wait(lambda: state.monitor.connected and len(state.intercom_directory.devices()) == 2)
    # Collegati da mezzo minuto: l'elenco è completo e la cantina risulta Non Trovata
    state.intercom_directory.connected_at -= 30
    server = _Server(uvicorn.Config(site.app, lifespan="off", ws="none", log_config=None, log_level=None))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    done = client.portal.start_task_soon(server.serve, [sock])
    assert wait(lambda: server.started or done.done(), 5)
    assert server.started
    yield f"http://127.0.0.1:{sock.getsockname()[1]}", door, site.door("porta2")
    server.should_exit = True
    done.result(timeout=10)
    sock.close()


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_the_intercom_is_chosen_from_the_broker_and_saved_with_its_serial(served, site):  # noqa: F811
    base, door, cellar = served
    config = {
        "base": base,
        "headers": ADMIN,
        "controller": door["controller_id"],
        "door": door["id"],
        "cellar": cellar["id"],
        "serial": SERIAL_A,
        "name": ARTISTI,
    }
    result = subprocess.run(["node", "-e", FLOW, json.dumps(config)], capture_output=True, text=True, timeout=180)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["errors"] == []
    # Scheda: il modello del videocitofono trovato, Non Trovato quello che il broker non annuncia
    assert out["tab"] == [
        {"title": ARTISTI, "meta": [MODEL, "porta Palcoscenico", "apre la porta con il relè 1",
                                    "nessun messaggio dall'avvio"]},
        {"title": "Citofono Cantina Non Trovato", "meta": ["porta Cantina", "registra le chiamate, non apre la porta",
                                                           "nessun messaggio dall'avvio"]},
    ]  # fmt: skip
    # Finestra: quello di adesso per primo e scelto, poi gli annunciati; il relè 1 dell'Ingresso Artisti è di un'altra
    # porta
    assert out["dialog"] == [
        {"text": "Citofono Cantina Non Trovato", "checked": True, "disabled": False},
        {"text": f"{CARRAIO}{MODEL}", "checked": False, "disabled": False},
        {"text": f"{ARTISTI}{MODEL}", "checked": False, "disabled": False},
    ]
    assert out["dialogRelays"] == [["Relè 1", False, True], ["Relè 2", False, False]]
    assert not out["dialogScroll"]
    assert out["chosenRelays"] == [["Relè 1 · Porta Palcoscenico", True, False], ["Relè 2", False, True]]
    # Salvato con il numero di serie e il nome annunciato; tolto, senza numero di serie (con, la porta lo rifiuterebbe)
    [saved, removed] = out["puts"]
    assert saved["status"] == 200 and saved["door"] == str(cellar["id"])
    keys = ("intercom_name", "intercom_serial", "intercom_relay", "intercom_mqtt_prefix")
    assert {k: saved["body"][k] for k in keys} == {
        "intercom_name": ARTISTI,
        "intercom_serial": SERIAL_A,
        "intercom_relay": 2,
        "intercom_mqtt_prefix": "hmd",
    }
    assert removed["status"] == 200 and removed["door"] == str(door["id"])
    assert removed["body"]["intercom_name"] == removed["body"]["intercom_serial"] == ""
    stored = {d["door_token"]: d for d in site.app.state.db.list_doors()}
    assert (stored["porta2"]["intercom_name"], stored["porta2"]["intercom_serial"]) == (ARTISTI, SERIAL_A)
    assert stored["porta2"]["intercom_relay"] == 2
    assert (stored["porta"]["intercom_name"], stored["porta"]["intercom_serial"]) == ("", "")
    # Elenchi vuoti: una riga sola; appena collegati la rotellina, poi l'elenco chiesto di nuovo, con il videocitofono
    # della porta ancora scelto (prima nella riga di quello non annunciato, poi in quella del dispositivo trovato)
    assert out["offline"] == [{"text": "Broker MQTT non collegato.", "checked": False, "disabled": False}]
    assert out["waiting"] == [
        {"text": ARTISTI, "checked": True, "disabled": False},
        {"text": "", "checked": False, "disabled": False},
    ]
    assert out["refetched"] == [
        {"text": f"{CARRAIO}{MODEL}", "checked": False, "disabled": False},
        {"text": f"{ARTISTI}{MODEL}", "checked": True, "disabled": False},
    ]
    assert out["refetchedRelays"] == [["Relè 1", False, False], ["Relè 2", False, True]]
    assert out["asked"] == 2
    # Aprendo una finestra il fuoco va sul pallino scelto, come con Tab
    assert out["focus"] == SERIAL_A
