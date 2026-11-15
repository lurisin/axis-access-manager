"""Colonne dell'elenco Persone scelte e ordinate da chi guarda (0.28.4), nella pagina vera in Chromium (Playwright):
finestra Colonne dell'Elenco con le frecce da tastiera, il trascinamento con il mouse e con il dito; la scelta che resta
dopo il ricaricamento e in un altro browser dello stesso utente, da Home Assistant e dall'accesso diretto, mai di un
altro utente; a 1280 (menu aperto e chiuso), 1000 e 390 px, in tema chiaro e scuro, senza scorrimento orizzontale e con
Cognome e Nome leggibili; il menu che si apre cambia le colonne in vista senza ridisegnare le righe, e l'aggiornamento
periodico lascia fuoco, scorrimento, ordine e colonne. Solo dati di fantasia."""

import json
import subprocess

import pytest
from test_direct import ADMIN, NEW_PASSWORD, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright
from test_photo_viewer import plain_png

# Testo di Cognome e Nome almeno così largo (un cognome di 8-10 lettere), come in test_person_list_page
MIN_TEXT = 75

PERSONS = [
    (
        "Marta",
        "Colombo",
        "0042",
        "Sala Prove",
        "+39 06 5550 4821",
        "marta.colombo@esempio.it",
        "Delta",
        "04a1b2c3d4e5f6",
    ),
    ("Paolo", "Esposito", "0107", "Biglietteria", "347 555 0101", "", "", "04b2c3d4e5f6a7"),
    ("Giulia", "De Santis", "", "Sartoria", "", "g.desantis@esempio.it", "", ""),
    ("Luca", "Ferrara", "0311", "Tecnici di Palco", "06 555 0177", "", "A-07", "04d4e5f6a7b8c9"),
    ("Sara", "Bernardi", "0099", "", "", "", "", ""),
    ("Anna", "Villa", "", "Ufficio Stampa", "333 555 0190", "anna.villa@esempio.it", "", ""),
]
# Altre persone di fantasia senza tessera, perché l'elenco scorra
EXTRA = [("Persona", f"Prova{i:02d}") for i in range(40)]

SCRIPT = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";

// Colonne della tabella: intestazioni in vista, attributi, testo più stretto di Cognome e Nome, scorrimento di lato
function table() {
  const vis = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";
  const t = document.querySelector("#p-list .p-table");
  const pad = (td) => parseFloat(getComputedStyle(td).paddingLeft) + parseFloat(getComputedStyle(td).paddingRight);
  const inner = (td) => td.clientWidth - pad(td);
  const cells = (cls) => [...document.querySelectorAll(`.p-table tbody tr[data-person] td.${cls}`)].filter(vis);
  const de = document.documentElement;
  return t ? {
    heads: [...t.querySelectorAll("thead th")].filter(vis).map((th) => th.textContent.replace(/[↑↓]/g, "").trim()),
    columns: t.dataset.columns, hide: t.dataset.hide,
    last: Math.min(...cells("c-last").map(inner)), first: Math.min(...cells("c-first").map(inner)),
    scroll: de.scrollWidth > de.clientWidth,
  } : {
    rows: [...document.querySelectorAll("#p-list .person-row")]
      .map((r) => r.querySelector(".p-sub")?.textContent ?? ""),
    scroll: de.scrollWidth > de.clientWidth,
  };
}
// Righe della finestra, con + per quelle spuntate
const order = (page) => page.$$eval(".p-col", (rows) => rows.map((li) => li.dataset.col
  + (li.querySelector("input").checked ? "+" : "")));
const center = async (page, sel) => {
  const b = await (await page.$(sel)).boundingBox();
  return [b.x + b.width / 2, b.y + b.height / 2, b];
};

async function open(browser, options, url) {
  const context = await browser.newContext(options);
  if (options.nav !== undefined) {
    await context.addInitScript((nav) => localStorage.setItem("aam-nav", nav), options.nav);
  }
  const page = await context.newPage();
  page.errors = [];
  page.on("pageerror", (e) => page.errors.push(String(e)));
  await page.goto(url);
  return [context, page];
}
async function login(page, user) {
  await page.waitForSelector("#auth form");
  await page.fill("[name=username]", user);
  await page.fill("[name=password]", config.password);
  await Promise.all([page.waitForNavigation(), page.click("#auth button[type=submit]")]);
  await page.evaluate(() => { location.hash = "#persone"; });
}
const listed = (page) => page.waitForSelector("#p-list .p-table, #p-list .person-row");
const closed = (page) => page.waitForSelector(".p-cols", {state: "hidden"});

(async () => {
  const browser = await chromium.launch();
  const out = {};
  const admin = {viewport: {width: 1280, height: 800}, extraHTTPHeaders: config.admin};

  // A. Amministratore da Home Assistant, 1280 col menu aperto: frecce da tastiera, caselle con lo spazio, mouse
  let [context, page] = await open(browser, admin, `${config.ingress}/#persone`);
  await listed(page);
  out.before = await page.evaluate(table);
  await page.click("#p-cols");
  await page.waitForSelector(".p-cols");
  out.dialog = {
    order: await order(page),
    fixed: await page.$$eval(".p-col input:disabled", (els) => els.map((el) => el.closest(".p-col").dataset.col)),
    upFirst: await page.$eval(".p-col:first-child [data-move='-1']", (b) => b.disabled),
    downLast: await page.$eval(".p-col:last-child [data-move='1']", (b) => b.disabled),
  };
  await page.focus('.p-col[data-col="stato"] [data-move="-1"]');
  await page.keyboard.press("Enter");
  await page.keyboard.press("Enter");
  out.keys = {
    order: await order(page),
    focus: await page.evaluate(() => document.activeElement.getAttribute("aria-label")),
    said: (await page.textContent("[data-live]")).trim(),
  };
  // In cima la freccia su si spegne e il fuoco passa alla freccia giù
  await page.focus('.p-col[data-col="cognome"] [data-move="-1"]');
  await page.keyboard.press("Enter");
  out.keys.top = await page.evaluate(() => [document.activeElement.getAttribute("aria-label"),
    document.querySelector(".p-col").dataset.col]);
  await page.keyboard.press("Enter");
  await page.focus('.p-col[data-col="telefono"] input');
  await page.keyboard.press("Space");
  await page.focus('.p-col[data-col="note"] input');
  await page.keyboard.press("Space");
  await page.focus('.p-col[data-col="tessere"] input');
  await page.keyboard.press("Space");
  // Mouse: Telefono trascinato sopra Matricola
  let [x, y] = await center(page, '.p-col[data-col="telefono"] .p-col-grip');
  let [, , target] = await center(page, '.p-col[data-col="matricola"]');
  await page.mouse.move(x, y);
  await page.mouse.down();
  await page.mouse.move(x, target.y + 6, {steps: 10});
  await page.mouse.up();
  out.mouse = {order: await order(page), said: (await page.textContent("[data-live]")).trim(),
    dragging: await page.$$eval(".dragging", (els) => els.length)};
  await page.click(".dlg-foot button.primary");
  await closed(page);
  await page.waitForTimeout(150);
  out.saved = {table: await page.evaluate(table), prefs: await page.evaluate(() => api("api/me/preferences"))};
  // Annulla non cambia niente
  await page.click("#p-cols");
  await page.waitForSelector(".p-cols");
  await page.click('.p-col[data-col="matricola"] [data-move="-1"]');
  await page.click(".dlg-foot button:not(.primary):not(.p-cols-reset)");
  await closed(page);
  out.cancel = {columns: await page.$eval(".p-table", (t) => t.dataset.columns),
    prefs: await page.evaluate(() => api("api/me/preferences"))};
  await page.reload();
  await listed(page);
  out.reloaded = await page.evaluate(table);
  // Menu chiuso: le colonne in vista cambiano, le righe restano le stesse (nessun ridisegno)
  await page.evaluate(() => { document.querySelector("#p-list tr[data-person]").mark = "stessa"; });
  await page.click("#nav-toggle");
  await page.waitForTimeout(400);
  out.menu = {table: await page.evaluate(table),
    same: await page.evaluate(() => document.querySelector("#p-list tr[data-person]").mark === "stessa")};
  // Aggiornamento ogni 15 secondi con un dato cambiato: fuoco, scorrimento, ordine e colonne restano
  await page.selectOption("#p-sort", "telefono:desc");
  await page.evaluate(() => {
    const rows = document.querySelectorAll("#p-list tr[data-person]");
    rows[20].focus();
    window.scrollTo(0, 600);
  });
  await page.waitForTimeout(100);
  const look = () => ({focus: document.activeElement.dataset.person, scroll: Math.round(window.scrollY),
    sort: document.querySelector("#p-sort").value, columns: document.querySelector(".p-table").dataset.columns,
    hide: document.querySelector(".p-table").dataset.hide,
    ids: [...document.querySelectorAll("#p-list tr[data-person]")].map((r) => r.dataset.person).join(",")});
  out.refresh = {before: await page.evaluate(look)};
  await page.evaluate(async (id) => {
    await api(`api/persons/${id}`, {method: "PATCH", body: {company: "Sartoria Nuova"}});
    await refreshPersons();
  }, config.changed);
  out.refresh.after = await page.evaluate(look);
  out.refresh.company = await page.$eval(`tr[data-person="${config.changed}"] td.c-company`, (td) => td.textContent);
  out.errorsA = page.errors;
  await context.close();

  // Lo stesso amministratore in un altro browser, a 1000 px: la stessa scelta, con meno colonne in vista
  [context, page] = await open(browser, {...admin, viewport: {width: 1000, height: 800}}, `${config.ingress}/#persone`);
  await listed(page);
  out.other = await page.evaluate(table);
  out.errorsB = page.errors;
  await context.close();

  // B. Personale dall'accesso diretto, telefono a 390 px in tema scuro: il dito
  const phone = {viewport: {width: 390, height: 844}, hasTouch: true, isMobile: true, colorScheme: "dark"};
  [context, page] = await open(browser, phone, `${config.direct}/`);
  await login(page, "personale");
  await listed(page);
  out.phone = {before: await page.evaluate(table)};
  await page.tap("#p-cols");
  await page.waitForSelector(".p-cols");
  out.phone.dialog = await page.evaluate(() => {
    const color = getComputedStyle(document.querySelector(".p-col")).backgroundColor.match(/\d+/g).map(Number);
    const de = document.documentElement;
    const box = document.querySelector("#modal").getBoundingClientRect();
    const names = [...document.querySelectorAll(".p-col-name > span:first-of-type")];
    return {order: [...document.querySelectorAll(".p-col")].map((li) => li.dataset.col),
      dark: color[0] + color[1] + color[2] < 200, inside: box.left >= 0 && box.right <= window.innerWidth,
      scroll: de.scrollWidth > de.clientWidth, names: names.every((s) => s.scrollWidth <= s.clientWidth)};
  });
  await page.tap('.p-col[data-col="telefono"] .p-col-name');
  const cdp = await context.newCDPSession(page);
  [x, y] = await center(page, '.p-col[data-col="telefono"] .p-col-grip');
  [, , target] = await center(page, '.p-col[data-col="foto"]');
  // Un dito vero: un movimento ogni 20 ms
  await cdp.send("Input.dispatchTouchEvent", {type: "touchStart", touchPoints: [{x, y}]});
  for (let i = 1; i <= 10; i++) {
    await page.waitForTimeout(20);
    const touchPoints = [{x, y: y + (target.y + 4 - y) * i / 10}];
    await cdp.send("Input.dispatchTouchEvent", {type: "touchMove", touchPoints});
  }
  await page.waitForTimeout(100);
  await cdp.send("Input.dispatchTouchEvent", {type: "touchEnd", touchPoints: []});
  await page.waitForTimeout(100);
  out.phone.touch = {order: await order(page), said: (await page.textContent("[data-live]")).trim()};
  await page.tap(".dlg-foot button.primary");
  await closed(page);
  await page.waitForTimeout(150);
  out.phone.saved = await page.evaluate(table);
  await page.reload();
  await listed(page);
  out.phone.reloaded = await page.evaluate(table);
  out.errorsC = page.errors;
  await context.close();

  // Lo stesso utente dell'accesso diretto da un computer, a 1000 px
  [context, page] = await open(browser, {viewport: {width: 1000, height: 800}}, `${config.direct}/`);
  await login(page, "personale");
  await listed(page);
  out.desk = await page.evaluate(table);
  // Ripristina Predefinite e Salva: di nuovo le predefinite
  await page.click("#p-cols");
  await page.waitForSelector(".p-cols");
  await page.click(".p-cols-reset");
  out.desk.reset = {order: await order(page), said: (await page.textContent("[data-live]")).trim(),
    open: await page.$eval("#modal", (d) => d.open)};
  await page.click(".dlg-foot button.primary");
  await closed(page);
  out.desk.prefs = await page.evaluate(() => api("api/me/preferences"));
  out.errorsD = page.errors;
  await context.close();
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""

DEFAULT_HEADS = ["Foto", "Cognome", "Nome", "Matricola", "Ditta", "Tipologia", "Tessere", "Ultimo Accesso", "Stato"]


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_columns_chosen_with_keyboard_mouse_and_finger_follow_the_user(plant, ingress_server):  # noqa: F811
    ingress, _, _, app = plant
    ids = {}
    for first, last, employee_id, company, phone, email, code, card in PERSONS:
        body = {"first_name": first, "last_name": last, "employee_id": employee_id, "company": company, "phone": phone,
                "email": email, "notes": f"Note di {first}\nseconda riga", "referent": "Direzione di Sala"}  # fmt: skip
        if card:
            body.update(card=card, printed_code=code)
        response = ingress.post("/api/persons", json=body, headers=ADMIN)
        assert response.status_code == 200, response.text
        ids[last] = response.json()["id"]
    for first, last in EXTRA:
        assert (
            ingress.post("/api/persons", json={"first_name": first, "last_name": last}, headers=ADMIN).status_code
            == 200
        )
    config = {
        "ingress": ingress_server,
        "direct": f"http://127.0.0.1:{app.state.direct.bound_port}",
        "admin": ADMIN,
        "password": NEW_PASSWORD,
        "changed": ids["Ferrara"],
    }
    result = subprocess.run(["node", "-e", SCRIPT, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for key in ("errorsA", "errorsB", "errorsC", "errorsD"):
        assert out[key] == [], key

    # Prima: le colonne della 0.28.3, tutte a 1280 col menu aperto
    assert out["before"]["heads"] == DEFAULT_HEADS and not out["before"]["scroll"]
    dialog = out["dialog"]
    assert dialog["order"] == [
        *("foto+ cognome+ nome+ matricola+ ditta+ tipologia+ tessere+ accesso+ stato+".split()),
        *("telefono email referente note inserimento modifica".split()),
    ]
    assert dialog["fixed"] == ["cognome", "nome"] and dialog["upFirst"] and dialog["downLast"]
    default_order = [c.rstrip("+") for c in dialog["order"]]
    # Frecce da tastiera: Stato su di due posti, il fuoco resta sul pulsante, la posizione annunciata
    assert out["keys"]["order"][5:8] == ["tipologia+", "stato+", "tessere+"]
    assert out["keys"]["focus"] == "Sposta Stato in su"
    assert out["keys"]["said"] == "Stato: posizione 7 di 15"
    # Cognome in cima: la freccia su si spegne e il fuoco passa alla freccia giù (che lo rimette al suo posto)
    assert out["keys"]["top"] == ["Sposta Cognome in giù", "cognome"]
    # Mouse: Telefono (spuntato con lo spazio) sopra Matricola
    mouse = out["mouse"]
    assert mouse["order"][:5] == ["foto+", "cognome+", "nome+", "telefono+", "matricola+"]
    assert "note+" in mouse["order"] and "tessere" in mouse["order"]
    assert mouse["said"] == "Telefono: posizione 4 di 15" and mouse["dragging"] == 0
    chosen = ["foto", "cognome", "nome", "telefono", "matricola", "ditta", "tipologia", "stato", "accesso", "note"]
    assert out["saved"]["prefs"] == {"person_columns": chosen}
    assert out["saved"]["table"]["columns"] == " ".join(chosen)
    # Annulla: niente di salvato né di cambiato
    assert out["cancel"] == {"columns": " ".join(chosen), "prefs": {"person_columns": chosen}}

    # 1280 col menu aperto: restano le prime dell'ordine, Cognome e Nome leggibili, niente scorrimento di lato
    for name in ("saved", "reloaded"):
        shown = out[name]["table"] if name == "saved" else out[name]
        assert shown["columns"] == " ".join(chosen), name
        hidden = shown["hide"].split()
        assert hidden == chosen[len(chosen) - len(hidden) :], name
        assert shown["last"] >= MIN_TEXT and shown["first"] >= MIN_TEXT and not shown["scroll"], name
    assert out["reloaded"]["heads"][:4] == ["Foto", "Cognome", "Nome", "Telefono"]
    # Menu chiuso: più colonne in vista, con le stesse righe
    menu = out["menu"]
    assert menu["same"] is True
    assert len(menu["table"]["hide"].split()) < len(out["reloaded"]["hide"].split())
    assert menu["table"]["last"] >= MIN_TEXT and not menu["table"]["scroll"]
    # Aggiornamento con un dato cambiato: fuoco sulla stessa riga, stesso scorrimento, stesso ordine e colonne
    refresh = out["refresh"]
    assert refresh["after"] == refresh["before"]
    assert refresh["before"]["sort"] == "telefono:desc" and refresh["before"]["scroll"] == 600
    assert refresh["company"] == "Sartoria Nuova"

    # Un altro browser dello stesso utente: la stessa scelta; a 1000 col menu aperto ne restano meno, sempre le prime
    other = out["other"]
    assert other["columns"] == " ".join(chosen)
    hidden = other["hide"].split()
    assert len(hidden) > len(out["reloaded"]["hide"].split()) and hidden == chosen[len(chosen) - len(hidden) :]
    assert other["heads"][:3] == ["Foto", "Cognome", "Nome"] and other["last"] >= MIN_TEXT and not other["scroll"]

    # Personale dall'accesso diretto: le sue colonne, non quelle dell'amministratore. Telefono, righe con sotto il nome
    # i primi dati delle colonne scelte
    phone = out["phone"]
    assert "Matricola 0042 · Sala Prove" in phone["before"]["rows"] and not phone["before"]["scroll"]
    assert phone["dialog"]["order"] == default_order
    assert phone["dialog"]["dark"] and phone["dialog"]["inside"] and not phone["dialog"]["scroll"]
    assert phone["dialog"]["names"] is True
    # Il dito: Telefono spuntato e portato in cima
    assert phone["touch"]["order"][:2] == ["telefono+", "foto+"]
    assert phone["touch"]["said"] == "Telefono: posizione 1 di 15"
    for name in ("saved", "reloaded"):
        assert "+39 06 5550 4821 · Matricola 0042" in phone[name]["rows"], name
        assert "347 555 0101 · Matricola 0107" in phone[name]["rows"], name
        assert not phone[name]["scroll"], name
    # Dal computer lo stesso utente ritrova la sua scelta
    desk = out["desk"]
    assert desk["columns"].split()[:2] == ["telefono", "foto"] and desk["heads"][0] == "Telefono"
    assert desk["last"] >= MIN_TEXT and not desk["scroll"]
    # Ripristina Predefinite rimette l'ordine di partenza senza chiudere la finestra; salvate, valgono le predefinite
    assert [c.rstrip("+") for c in desk["reset"]["order"]] == default_order
    assert desk["reset"]["said"] == "Colonne predefinite: salva per tenerle" and desk["reset"]["open"] is True
    assert desk["prefs"] == {"person_columns": None}
    # E l'amministratore ha ancora la sua
    assert ingress.get("/api/me/preferences", headers=ADMIN).json() == {"person_columns": chosen}


REVIEW = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
const look = (page) => page.evaluate(() => {
  const t = document.querySelector("#p-list .p-table");
  return {list: document.querySelector("#p-list").clientWidth, hide: t.dataset.hide,
    menu: document.body.classList.contains("nav-open")};
});
const settle = (page) => page.waitForTimeout(450);

(async () => {
  const browser = await chromium.launch();
  const out = {};
  const context = await browser.newContext({viewport: {width: 1280, height: 800}, extraHTTPHeaders: config.admin});
  await context.addInitScript(() => localStorage.setItem("aam-nav", "1"));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${config.ingress}/#persone`);
  await page.waitForSelector("#p-list .p-table");
  await settle(page);
  // Colonne tolte per lo spazio: a 1280 col menu aperto, poi 1100, poi di nuovo 1280
  out.window = [await look(page)];
  for (const width of [1100, 1280]) {
    await page.setViewportSize({width, height: 800});
    await settle(page);
    out.window.push(await look(page));
  }
  // A 1100 col menu chiuso, poi aperto e di nuovo chiuso
  await page.setViewportSize({width: 1100, height: 800});
  out.menu = [];
  for (let i = 0; i < 3; i++) {
    await page.click("#nav-toggle");
    await settle(page);
    out.menu.push(await look(page));
  }
  // Da tastiera: ordine per Telefono dalla tendina (la colonna si aggiunge), poi Invio su Ultimo Accesso
  await page.setViewportSize({width: 1280, height: 800});
  out.focus = [];
  for (const sort of ["telefono:asc", "email:asc", "referente:asc"]) {
    await page.selectOption("#p-sort", sort);
    await settle(page);
    await page.focus("#p-list [data-sort='accesso']");
    await page.keyboard.press("Enter");
    await settle(page);
    out.focus.push(await page.evaluate(() => ({sort: document.querySelector("#p-sort").value,
      focus: document.activeElement.dataset?.sort || document.activeElement.tagName})));
  }
  // Colonne dell'Elenco su uno schermo basso: ↓ da tastiera su Foto fino in fondo, la riga resta in vista
  await page.setViewportSize({width: 1366, height: 768});
  await page.click("#p-cols");
  await page.waitForSelector(".p-cols");
  await settle(page);
  out.scrolls = await page.evaluate(() => {
    const body = document.querySelector("#modal .dlg-body");
    return body.scrollHeight > body.clientHeight;
  });
  await page.focus('.p-col[data-col="foto"] [data-move="1"]');
  out.keys = [];
  for (let i = 0; i < 14; i++) {
    await page.keyboard.press("Enter");
    await page.waitForTimeout(50);
    out.keys.push(await page.evaluate(() => {
      const body = document.querySelector("#modal .dlg-body").getBoundingClientRect();
      const el = document.activeElement, row = el.closest(".p-col");
      const inside = (r) => r.top >= body.top - 0.5 && r.bottom <= body.bottom + 0.5;
      return {at: [...row.parentNode.children].indexOf(row) + 1, col: row.dataset.col,
        button: inside(el.getBoundingClientRect()), row: inside(row.getBoundingClientRect())};
    }));
  }
  out.errors = errors;
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_columns_review_width_memory_header_focus_and_rows_in_view(plant, ingress_server):  # noqa: F811
    """Revisione delle colonne: (1) una colonna tolta per lo spazio torna alla stessa larghezza dopo un cambio grande
    (finestra, menu): a 1280 col menu aperto le 9 colonne anche dopo un passaggio a 1100, e a 1100 il menu aperto e
    richiuso lascia le colonne di prima; (2) da tastiera, dopo l'ordine per Telefono, Email o Referente dalla tendina,
    Invio su Ultimo Accesso lascia il fuoco su quel pulsante (prima finiva sul body); (3) nella finestra Colonne
    dell'Elenco, su uno schermo basso, ↓ da tastiera tiene in vista la riga spostata e il pulsante col fuoco."""
    ingress, _, _, _ = plant
    for first, last, employee_id, company, phone, email, code, card in PERSONS:
        body = {"first_name": first, "last_name": last, "employee_id": employee_id, "company": company, "phone": phone,
                "email": email, "referent": "Direzione di Sala"}  # fmt: skip
        if card:
            body.update(card=card, printed_code=code)
        assert ingress.post("/api/persons", json=body, headers=ADMIN).status_code == 200
    config = {"ingress": ingress_server, "admin": ADMIN}
    result = subprocess.run(["node", "-e", REVIEW, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert out["errors"] == []

    first, narrow, back = out["window"]
    assert first["menu"] and first["hide"] == "" and narrow["hide"] != ""
    assert back["list"] == first["list"] and back["hide"] == "", out["window"]
    closed, opened, closed_again = out["menu"]
    assert not closed["menu"] and opened["menu"] and not closed_again["menu"], out["menu"]
    assert closed["hide"] == "" and opened["hide"] != "", out["menu"]
    assert closed_again["list"] == closed["list"] and closed_again["hide"] == "", out["menu"]

    assert out["focus"] == [{"sort": "accesso:desc", "focus": "accesso"}] * 3, out["focus"]

    assert out["scrolls"] is True
    keys = out["keys"]
    assert [k["at"] for k in keys] == list(range(2, 16)) and {k["col"] for k in keys} == {"foto"}
    assert all(k["button"] and k["row"] for k in keys), keys


BAND = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
// Barra di scorrimento classica di Windows, 17 px. Chromium di Playwright parte con --hide-scrollbars, che toglie ogni
// barra (come le barre sovrapposte del telefono e del Mac, che non tolgono spazio): senza quell'opzione Chromium su
// Linux ha la barra classica (15 px), e ::-webkit-scrollbar la porta a 17 px come su Windows
const WINDOWS_BAR = "::-webkit-scrollbar { width: 17px; height: 17px; } "
  + "::-webkit-scrollbar-thumb { background: #999; }";
const look = (page, label) => page.evaluate((label) => {
  const t = document.querySelector("#p-list .p-table");
  const vis = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";
  const pad = (td) => parseFloat(getComputedStyle(td).paddingLeft) + parseFloat(getComputedStyle(td).paddingRight);
  const inner = (cls) => Math.min(...[...t.querySelectorAll(`tbody tr[data-person] td.${cls}`)].filter(vis)
    .map((td) => td.clientWidth - pad(td)));
  const de = document.documentElement;
  return {label, bar: innerWidth - de.clientWidth, list: document.querySelector("#p-list").clientWidth,
    hide: t.dataset.hide, heads: [...t.querySelectorAll("thead th")].filter(vis).length,
    last: inner("c-last"), first: inner("c-first"), side: de.scrollWidth > de.clientWidth};
}, label);

async function run(browser, windowsBar) {
  const context = await browser.newContext({viewport: {width: 1280, height: 800}, extraHTTPHeaders: config.admin});
  await context.addInitScript((css) => {
    localStorage.setItem("aam-nav", "1");
    if (!css) return;
    const add = () => {
      const style = document.createElement("style");
      style.textContent = css;
      (document.head || document.documentElement).appendChild(style);
    };
    if (document.documentElement) add(); else document.addEventListener("DOMContentLoaded", add);
  }, windowsBar ? WINDOWS_BAR : "");
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(`${config.ingress}/#persone`);
  await page.waitForSelector("#p-list .p-table");
  await page.waitForTimeout(400);
  const size = async (width, height = 800) => {
    await page.setViewportSize({width, height});
    await page.waitForTimeout(200);
    return look(page, `${width}x${height}`);
  };
  const out = {errors};
  // Finestra stretta di poco e riallargata, un px alla volta intorno al limite e di colpo
  out.band = [];
  for (const width of [1280, 1270, 1262, 1261, 1258, 1261, 1262, 1270, 1280, 1258, 1280]) {
    out.band.push(await size(width));
  }
  // Barra che va e viene: la finestra alta quanto tutto l'elenco (niente barra) e più bassa (la barra), e la ricerca
  // che lascia poche righe. Si contano i cambi delle colonne in vista
  out.swing = {};
  for (const width of [1280, 1275]) {
    await size(width);
    await page.evaluate(() => {
      window.hideChanges = [];
      new MutationObserver((ms) => ms.forEach((m) => window.hideChanges.push(m.target.dataset.hide)))
        .observe(document.querySelector("#p-list"), {subtree: true, attributes: true, attributeFilter: ["data-hide"]});
    });
    const seq = [];
    for (const height of [800, 6000, 800, 6000, 800]) seq.push(await size(width, height));
    for (const q of ["Colombo", "", "Prova0", ""]) {
      await page.fill("#p-q", q);
      await page.waitForTimeout(200);
      seq.push(await look(page, `${width} «${q}»`));
    }
    out.swing[width] = {seq, changes: await page.evaluate(() => window.hideChanges)};
  }
  await context.close();
  return out;
}

(async () => {
  const out = {};
  let browser = await chromium.launch();
  out.overlay = await run(browser, false);
  await browser.close();
  browser = await chromium.launch({ignoreDefaultArgs: ["--hide-scrollbars"]});
  out.windows = await run(browser, true);
  await browser.close();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_same_window_width_same_columns_with_and_without_the_scrollbar(plant, ingress_server):  # noqa: F811
    """Verifica della 0.28.4: a 1280 col menu aperto, con le predefinite, l'elenco è di 998 px e ne servono 980.
    Stringendo di poco la finestra (1258-1261) Stato andava via e riallargandola a 1280 non tornava più fino al
    ricaricamento. Ora la pagina conta sempre la barra di scorrimento verticale, che ci sia o no: alla stessa larghezza
    della finestra le stesse colonne, qualunque cosa sia successa prima. Senza barra (Chromium di Playwright, come le
    barre sovrapposte del telefono e del Mac) e con la barra classica di Windows simulata (17 px): a 1280 col menu
    aperto tutte e 9 le colonne, Tessere compresa; la barra che compare e sparisce (finestra più alta o più bassa, la
    ricerca che lascia poche righe) non cambia le colonne; Cognome e Nome mai sotto i 75 px, mai lo scorrimento di
    lato."""
    ingress, _, _, _ = plant
    for first, last, employee_id, company, phone, email, code, card in PERSONS:
        body = {"first_name": first, "last_name": last, "employee_id": employee_id, "company": company, "phone": phone,
                "email": email, "referent": "Direzione di Sala"}  # fmt: skip
        if card:
            body.update(card=card, printed_code=code)
        assert ingress.post("/api/persons", json=body, headers=ADMIN).status_code == 200
    for first, last in EXTRA:
        assert (
            ingress.post("/api/persons", json={"first_name": first, "last_name": last}, headers=ADMIN).status_code
            == 200
        )
    config = {"ingress": ingress_server, "admin": ADMIN}
    result = subprocess.run(["node", "-e", BAND, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for name, bar in (("overlay", 0), ("windows", 17)):
        run = out[name]
        assert run["errors"] == [], name
        band = run["band"]
        # La barra c'è (o non c'è) davvero: l'elenco di 46 persone scorre a 800 px di altezza
        assert {step["bar"] for step in band} == {bar}, (name, band)
        by_width = {}
        for step in band:
            by_width.setdefault(step["label"], set()).add(step["hide"])
            assert step["last"] >= MIN_TEXT and step["first"] >= MIN_TEXT and not step["side"], (name, step)
        # Stessa larghezza, stesse colonne, in qualunque ordine ci si arrivi
        assert all(len(hides) == 1 for hides in by_width.values()), (name, band)
        # A 1280 col menu aperto ci sono tutte, Tessere compresa; a 1258 va via solo Stato
        assert by_width["1280x800"] == {""} and band[0]["heads"] == 9, (name, band)
        assert by_width["1258x800"] == {"stato"}, (name, band)
        for width, swing in run["swing"].items():
            seq = swing["seq"]
            # Senza barra con la finestra alta e con la ricerca che lascia poche righe, con la barra a 800 px
            assert {step["bar"] for step in seq} == {0, bar}, (name, width, seq)
            assert swing["changes"] == [], (name, width, swing)
            assert len({step["hide"] for step in seq}) == 1, (name, width, seq)
            assert all(s["last"] >= MIN_TEXT and s["first"] >= MIN_TEXT and not s["side"] for s in seq), (name, seq)
        assert run["swing"]["1280"]["seq"][0]["hide"] == "", name
    # Con la barra di Windows a 1275 Stato non c'è, con la barra e senza; senza barra (telefono, Mac) c'è
    assert out["windows"]["swing"]["1275"]["seq"][0]["hide"] == "stato"
    assert out["overlay"]["swing"]["1275"]["seq"][0]["hide"] == ""


UNDER_PHOTO = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
// Barra classica di Windows simulata come in BAND
const WINDOWS_BAR = "::-webkit-scrollbar { width: 17px; height: 17px; } "
  + "::-webkit-scrollbar-thumb { background: #999; }";
const look = (page, label) => page.evaluate((label) => {
  const t = document.querySelector("#p-list .p-table");
  const vis = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";
  const de = document.documentElement;
  return {label, viewer: document.querySelector("#viewer").open, overflow: getComputedStyle(de).overflowY,
    bar: innerWidth - de.clientWidth, gap: Math.round(innerWidth - de.getBoundingClientRect().width),
    list: document.querySelector("#p-list").clientWidth, measured: personListWidth(document.querySelector("#p-list")),
    hide: t.dataset.hide, heads: [...t.querySelectorAll("thead th")].filter(vis).length,
    changes: window.hideChanges.length};
}, label);
const settle = (page, ms = 250) => page.waitForTimeout(ms);

async function run(browser, windowsBar) {
  const context = await browser.newContext({viewport: {width: 1280, height: 800}, extraHTTPHeaders: config.admin});
  await context.addInitScript((css) => {
    localStorage.setItem("aam-nav", "1");
    if (!css) return;
    const add = () => {
      const style = document.createElement("style");
      style.textContent = css;
      (document.head || document.documentElement).appendChild(style);
    };
    if (document.documentElement) add(); else document.addEventListener("DOMContentLoaded", add);
  }, windowsBar ? WINDOWS_BAR : "");
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  const load = async () => {
    await page.waitForSelector("#p-list .p-table");
    await settle(page, 400);
    // Ogni cambio delle colonne in vista
    await page.evaluate(() => {
      window.hideChanges = [];
      new MutationObserver((ms) => ms.forEach((m) => window.hideChanges.push(m.target.dataset.hide)))
        .observe(document.querySelector("#p-list"), {subtree: true, attributes: true, attributeFilter: ["data-hide"]});
    });
  };
  await page.goto(`${config.ingress}/#persone`);
  await load();
  const out = {errors, steps: [await look(page, "caricata")]};
  // Foto ingrandita dalla colonna Foto
  await page.locator("#p-list td.c-photo img").first().click();
  await page.waitForFunction(() => document.querySelector("#viewer img"));
  await settle(page);
  out.steps.push(await look(page, "foto"));
  // Un altro utente cambia la ditta di una persona (fuori dalla pagina, e diversa a ogni giro: il dato cambia davvero);
  // l'aggiornamento dei 15 secondi (refreshView, chiamato qui subito) ridisegna l'elenco sotto la foto
  out.company = {sent: windowsBar ? "Sartoria Nord" : "Sartoria Sud"};
  const changed = await context.request.patch(`${config.ingress}/api/persons/${config.changed}`,
    {data: {company: out.company.sent}});
  out.company.status = changed.status();
  await page.evaluate(() => refreshView());
  await settle(page);
  out.company.shown = await page.$eval(`tr[data-person="${config.changed}"] td.c-company`, (td) => td.textContent);
  out.steps.push(await look(page, "foto, aggiornata"));
  // La finestra stretta e riallargata con la foto aperta
  for (const width of [1275, 1258, 1280]) {
    await page.setViewportSize({width, height: 800});
    await settle(page);
    out.steps.push(await look(page, `foto ${width}`));
  }
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#viewer").open);
  await settle(page, 500);
  out.steps.push(await look(page, "foto chiusa"));
  // Colonne dell'Elenco e «Attendere…»: non cambiano l'overflow della pagina, la misura resta
  await page.click("#p-cols");
  await page.waitForSelector(".p-cols");
  await settle(page);
  out.steps.push(await look(page, "Colonne"));
  await page.keyboard.press("Escape");
  await page.waitForSelector(".p-cols", {state: "hidden"});
  await page.evaluate(() => { busy("Prova", new Promise((resolve) => setTimeout(resolve, 1200))); });
  await page.waitForFunction(() => document.querySelector("#busy").open);
  out.steps.push(await look(page, "Attendere"));
  await page.waitForFunction(() => !document.querySelector("#busy").open);
  // Riferimento: la pagina ricaricata alle stesse larghezze
  out.reloaded = {};
  for (const width of [1275, 1258, 1280]) {
    await page.setViewportSize({width, height: 800});
    await page.reload();
    await load();
    out.reloaded[width] = await look(page, `ricaricata ${width}`);
  }
  await context.close();
  return out;
}

(async () => {
  const out = {};
  let browser = await chromium.launch();
  out.overlay = await run(browser, false);
  await browser.close();
  browser = await chromium.launch({ignoreDefaultArgs: ["--hide-scrollbars"]});
  out.windows = await run(browser, true);
  await browser.close();
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_columns_stay_the_same_under_the_enlarged_photo(plant, ingress_server, tmp_path):  # noqa: F811
    """Verifica della 0.28.4: con la barra classica di Windows, a 1280 col menu aperto, la foto ingrandita aperta dalla
    colonna Foto mette html.viewer-open (overflow: hidden) e scrollbar-gutter: stable. Lo spazio della barra resta e
    l'elenco è largo uguale (981 px), ma clientWidth di html diventa la finestra intera: l'aggiornamento dei 15 secondi
    con un dato cambiato ridisegnava l'elenco contando 17 px in meno e Stato andava via, anche dopo aver chiuso la foto,
    fino al giro dopo; la pagina ricaricata alla stessa larghezza ce l'aveva. Ora la pagina misura lo spazio che la
    barra occupa davvero: sotto la foto, aggiornando l'elenco e stringendo e riallargando la finestra, le colonne sono
    quelle della pagina ricaricata alla stessa larghezza, e chiusa la foto non cambiano. Con la finestra Colonne
    dell'Elenco e con il riquadro «Attendere…» l'overflow della pagina non cambia e la misura resta la stessa. Anche
    senza barra (telefono, Mac)."""
    ingress, _, _, app = plant
    app.state.ha.snapshot_dir = tmp_path / "foto"
    app.state.ha.snapshot_dir.mkdir()
    ids = {}
    for first, last, employee_id, company, phone, email, code, card in PERSONS:
        body = {"first_name": first, "last_name": last, "employee_id": employee_id, "company": company, "phone": phone,
                "email": email, "referent": "Direzione di Sala"}  # fmt: skip
        if card:
            body.update(card=card, printed_code=code)
        response = ingress.post("/api/persons", json=body, headers=ADMIN)
        assert response.status_code == 200, response.text
        ids[last] = response.json()["id"]
    for first, last in EXTRA:
        assert (
            ingress.post("/api/persons", json={"first_name": first, "last_name": last}, headers=ADMIN).status_code
            == 200
        )
    photo = ingress.put(
        f"/api/persons/{ids['Colombo']}/photo",
        content=plain_png(480, 600, 120),
        headers={**ADMIN, "Content-Type": "image/png"},
    )
    assert photo.status_code == 200, photo.text
    config = {"ingress": ingress_server, "admin": ADMIN, "changed": ids["Ferrara"]}
    result = subprocess.run(
        ["node", "-e", UNDER_PHOTO, json.dumps(config)], capture_output=True, text=True, timeout=300
    )
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for name, bar in (("overlay", 0), ("windows", 17)):
        run = out[name]
        assert run["errors"] == [], name
        steps = {step["label"]: step for step in run["steps"]}
        reloaded = run["reloaded"]
        loaded = steps["caricata"]
        # A 1280 col menu aperto tutte e 9 le colonne; la barra c'è (o non c'è) davvero
        assert loaded["bar"] == loaded["gap"] == bar and loaded["hide"] == "" and loaded["heads"] == 9, (name, loaded)
        assert loaded == {**reloaded["1280"], "label": "caricata"}, (name, loaded, reloaded["1280"])
        # Con la foto aperta la pagina non scorre più (overflow: hidden) ma lo spazio della barra resta, come l'elenco
        for label in ("foto", "foto, aggiornata"):
            step = steps[label]
            assert step["viewer"] and step["overflow"] == "hidden", (name, step)
            assert step["gap"] == bar and step["list"] == loaded["list"], (name, step)
            # Stessa misura, stesse colonne, nessun cambio: anche dopo il ridisegno dell'aggiornamento
            assert step["measured"] == loaded["measured"] and step["hide"] == "" and step["changes"] == 0, (name, step)
        # L'elenco sotto la foto è stato ridisegnato davvero: c'è la ditta nuova
        assert run["company"]["status"] == 200 and run["company"]["shown"] == run["company"]["sent"], (
            name,
            run["company"],
        )
        # La finestra stretta e riallargata sotto la foto: le colonne della pagina ricaricata a quella larghezza
        for width in ("1275", "1258", "1280"):
            step, ref = steps[f"foto {width}"], reloaded[width]
            assert step["viewer"] and step["gap"] == bar, (name, step)
            assert (step["list"], step["measured"], step["hide"]) == (ref["list"], ref["measured"], ref["hide"]), (
                name,
                step,
                ref,
            )
        # Chiusa la foto: la barra torna al suo spazio, niente cambia (né colonne né elenco)
        closed = steps["foto chiusa"]
        assert not closed["viewer"] and closed["overflow"] != "hidden" and closed["bar"] == bar, (name, closed)
        assert closed["changes"] == steps["foto 1280"]["changes"], (name, closed)
        assert {k: closed[k] for k in ("list", "measured", "hide", "heads")} == {
            k: reloaded["1280"][k] for k in ("list", "measured", "hide", "heads")
        }, (name, closed, reloaded["1280"])
        # Colonne dell'Elenco (una finestra modal) e «Attendere…»: l'overflow della pagina non cambia, la misura resta
        for label in ("Colonne", "Attendere"):
            step = steps[label]
            assert step["overflow"] == closed["overflow"] and step["bar"] == step["gap"] == bar, (name, step)
            assert (step["measured"], step["hide"], step["changes"]) == (
                closed["measured"],
                closed["hide"],
                closed["changes"],
            ), (name, step)
        # A 1258 Stato va via, con la barra e senza; a 1275 solo con la barra di Windows (come in BAND)
        assert reloaded["1258"]["hide"] == "stato", name
        assert reloaded["1275"]["hide"] == ("stato" if bar else ""), name
