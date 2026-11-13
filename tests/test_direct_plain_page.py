"""La pagina vera in Chromium, dall'accesso diretto e da Home Assistant (0.28.3): il personale senza permessi tecnici
visita ogni pagina che può vedere, apre la finestra dell'utente, i guasti in alto, il catalogo del cruscotto, i dettagli
degli eventi, le notifiche e la pillola dell'app non raggiungibile, e non legge mai Home Assistant, MQTT, broker,
add-on e simili: né nel testo, né nei suggerimenti (title), né nelle etichette (aria-label). L'unica eccezione è il
registro, dove Home Assistant è chi ha aperto la porta a distanza. Lo stesso giro come amministratore dall'accesso
diretto, e da Home Assistant come amministratore e come operatore, trova i testi di sempre."""

import json
import socket
import subprocess
import time

import pytest
import uvicorn
from test_direct import ADMIN, NEW_PASSWORD, clients, fast_hash  # noqa: F401
from test_direct_plain import HA_SOURCE, OPERATOR, faults, plant  # noqa: F401
from test_page_morph import _has_playwright

from app.direct import _Server

ROUTES = (
    "#cruscotto",
    "#porte",
    "#porte/{door}",
    "#persone",
    "#persone/tipologie",
    "#persone/{person}",
    "#da-registrare",
    "#registro",
    "#regole",
    "#regole/tipologie",
    "#notifiche",
    "#impostazioni",
)

TOUR = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
// Le parole vietate (HA solo maiuscolo: «ha» è il verbo; ingress, non ingresso)
const WORDS = /home[ -]?assistant|mqtt|broker|mosquitto|add-?on|supervisor|\bingress\b|integrazion/i;
const UPPER = /\bHA\b/;

// Testo visibile, suggerimenti ed etichette con una parola vietata. Ammesse: le righe del registro degli eventi aperti
// da Home Assistant e l'Origine nel loro Dettaglio Evento. Gira nella pagina: le espressioni arrivano come testo
function look([step, allowed, words, upper]) {
  const [wordsRe, upperRe] = [new RegExp(words, "i"), new RegExp(upper)];
  const says = (t) => Boolean(t) && (wordsRe.test(t) || upperRe.test(t));
  const fine = (el) => {
    const row = el.closest("[data-event]");
    if (row && allowed.includes(Number(row.dataset.event))) return true;
    const dd = el.closest("dd");
    return Boolean(dd && dd.previousElementSibling?.textContent === "Origine" && dd.closest(".log-detail, #modal"));
  };
  const found = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || ["SCRIPT", "STYLE"].includes(el.tagName) || !el.checkVisibility({visibilityProperty: true})) continue;
    if (says(n.textContent) && !fine(el)) found.push(`${step} · testo: ${n.textContent.trim().slice(0, 160)}`);
  }
  for (const el of document.querySelectorAll("[title], [aria-label], [placeholder], [alt]")) {
    for (const name of ["title", "aria-label", "placeholder", "alt"]) {
      const value = el.getAttribute(name);
      if (says(value) && !fine(el)) found.push(`${step} · ${name}: ${value.slice(0, 160)}`);
    }
  }
  if (says(document.title)) found.push(`${step} · titolo: ${document.title}`);
  return {found, text: document.body.innerText, wide: document.documentElement.scrollWidth};
}

async function settle(page) {
  const busy = "#view .loading, #st-body .loading";
  await page.waitForFunction((sel) => !document.querySelector(sel), busy, {timeout: 10000});
  await page.waitForTimeout(250);
}

async function tour(browser, run) {
  const context = await browser.newContext({viewport: {width: run.width, height: 900}, colorScheme: run.theme,
    extraHTTPHeaders: run.headers || {}});
  const page = await context.newPage();
  const out = {steps: {}, errors: []};
  page.on("pageerror", (e) => out.errors.push(String(e)));
  const step = async (name) => {
    out.steps[name] = await page.evaluate(look, [name, config.allowed, WORDS.source, UPPER.source]);
  };
  await page.goto(run.base + "/");
  if (run.login) {
    await page.waitForSelector("#auth form");
    await step("accesso");
    await page.fill("[name=username]", run.login[0]);
    await page.fill("[name=password]", "sbagliata-123");
    await page.click("#auth button[type=submit]");
    await page.waitForSelector("#auth-err:not([hidden])");
    await step("accesso sbagliato");
    await page.fill("[name=password]", run.login[1]);
    await Promise.all([page.waitForNavigation(), page.click("#auth button[type=submit]")]);
  }
  await page.waitForSelector("#nav-user:not([hidden])");
  for (const route of config.routes) {
    await page.evaluate((r) => { location.hash = r; }, route);
    await settle(page);
    await step(route);
  }
  // Registro: tutti gli eventi, e il Dettaglio Evento di quelli che contano
  await page.evaluate(() => { location.hash = "#registro"; });
  await settle(page);
  await page.click('[data-view="tutto"]');
  await page.waitForTimeout(500);
  await step("registro, tutto");
  for (const id of config.events) {
    await page.click(`#l-list [data-event="${id}"]`);
    await page.waitForTimeout(200);
    await step(`evento ${id}`);
    if (await page.$("#modal[open]")) await page.keyboard.press("Escape");
  }
  if (run.menu) {
    await page.click("#nav-user");
    await page.waitForSelector("#modal[open]");
    await step("utente");
    await page.keyboard.press("Escape");
  }
  await page.evaluate(() => { location.hash = "#cruscotto"; });
  await settle(page);
  if (await page.$('[data-pop="faults"]')) {
    await page.click('[data-pop="faults"]');
    await page.waitForTimeout(200);
    await step("guasti");
    await page.click('[data-pop="faults"]');
  }
  await page.click("#top-edit");
  await page.click("#dash-add");
  await page.waitForSelector("#modal[open]");
  await step("catalogo");
  await page.keyboard.press("Escape");
  await page.click("#dash-cancel");
  // App che non risponde
  await page.route("**/api/summary", (r) => r.abort());
  await page.waitForSelector("#offline-pill:not([hidden])", {timeout: 15000});
  await step("non raggiungibile");
  out.pill = await page.$eval("#offline-pill", (p) => p.textContent);
  await context.close();
  return out;
}

(async () => {
  const browser = await chromium.launch();
  const result = {};
  for (const run of config.runs) result[run.name] = await tour(browser, run);
  await browser.close();
  console.log(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.fixture
def ingress_server(plant):  # noqa: F811
    """L'app servita davvero anche come da Home Assistant (le intestazioni dell'utente le mette il browser)."""
    ingress, _, _, app = plant
    server = _Server(uvicorn.Config(app, lifespan="off", ws="none", log_config=None, log_level=None))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    done = ingress.portal.start_task_soon(server.serve, [sock])
    for _ in range(200):
        if server.started or done.done():
            break
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    server.should_exit = True
    done.result(timeout=10)
    sock.close()


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_direct_access_pages_never_name_home_assistant(plant, ingress_server, monkeypatch):  # noqa: F811
    ingress, _, _, app = plant
    faults(app, monkeypatch)
    door = app.state.doors[0]
    person = ingress.post(
        "/api/persons", json={"first_name": "Anna", "last_name": "Verdi", "card": "04aa11bb"}, headers=ADMIN
    ).json()
    db = app.state.db
    now = db.list_events(limit=1)[0]["ts"]
    db.add_event(ts=now, kind="concesso", door=door["door_token"], card="04aa11bb", person_id=person["id"],
                 person_name="Anna Verdi", source="controller", recovered=True)  # fmt: skip
    events = db.list_events(limit=50)
    allowed = [e["id"] for e in events if "Home Assistant" in (e.get("source") or "")]
    shown = allowed + [e["id"] for e in events if e.get("recovered") or e.get("snapshot_error")]
    direct = f"http://127.0.0.1:{app.state.direct.bound_port}"
    runs = [
        {"name": "personale", "base": direct, "login": ["personale", NEW_PASSWORD], "menu": True, "width": 390},
        {"name": "amministratore", "base": direct, "login": ["admin", NEW_PASSWORD], "menu": True, "width": 1280},
        {"name": "ingress", "base": ingress_server, "headers": ADMIN, "width": 1280},
        {"name": "operatore", "base": ingress_server, "headers": OPERATOR, "width": 390},
    ]
    for run, theme in zip(runs, ("dark", "light", "light", "dark"), strict=True):
        run["theme"] = theme
    config = {
        "runs": runs,
        "routes": [r.format(door=door["id"], person=person["id"]) for r in ROUTES],
        "events": sorted(set(shown)),
        "allowed": allowed,
    }
    result = subprocess.run(["node", "-e", TOUR, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    text = {name: {step: data["text"] for step, data in run["steps"].items()} for name, run in out.items()}
    for name, run in out.items():
        assert run["errors"] == [], name

    # Personale dall'accesso diretto: nessuna parola vietata in nessun passo, a 390 px senza scorrere di lato
    staff = out["personale"]
    assert [f for data in staff["steps"].values() for f in data["found"]] == []
    assert {step: data["wide"] for step, data in staff["steps"].items() if data["wide"] > 390} == {}
    assert staff["pill"] == "App Non Raggiungibile"
    assert "Ruolo" in text["personale"]["utente"] and "Nome Utente" in text["personale"]["utente"]
    assert "Accesso" not in text["personale"]["utente"].replace("Accessi", "")
    assert "Videocitofoni Senza Collegamento" in text["personale"]["#notifiche"]
    assert "Orologio del Server" in text["personale"]["#notifiche"]
    assert "Opzioni dell'App" in text["personale"]["#impostazioni"]
    assert "Controller, eventi in diretta, coperchio e ultimo evento." in text["personale"]["catalogo"]
    # Il registro dice ancora chi ha aperto da Home Assistant
    assert HA_SOURCE in text["personale"]["registro, tutto"]

    # Amministratore dall'accesso diretto e da Home Assistant, operatore da Home Assistant: i testi di sempre
    for name in ("amministratore", "ingress", "operatore"):
        assert "Nessun Broker MQTT" in text[name]["#notifiche"], name
        assert "Orologio di Home Assistant" in text[name]["#notifiche"], name
        assert "Opzioni dell'Add-on" in text[name]["#impostazioni"], name
        assert "Controller, eventi MQTT, coperchio e ultimo evento." in text[name]["catalogo"], name
        assert any("MQTT o add-on fermi in quel momento" in t for t in text[name].values()), name
    assert "Database Ripartito Vuoto" in text["amministratore"]["#notifiche"]
    assert "Database dell'Add-on" in text["ingress"]["#impostazioni"]
    assert out["amministratore"]["pill"] == "App Non Raggiungibile"
    assert out["ingress"]["pill"] == out["operatore"]["pill"] == "Add-on Non Raggiungibile"
    assert "Diretto, fuori da Home Assistant" not in text["amministratore"]["utente"]
    # Per tutti: la porta non parla più dei pulsanti di Home Assistant
    for name in out:
        assert "pulsanti di Home Assistant" not in text[name][f"#porte/{door['id']}"], name
