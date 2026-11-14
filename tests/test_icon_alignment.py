"""Icone allineate al nome accanto (0.28.4), nella pagina vera in Chromium a 1280 e 390 px: il centro di ogni icona
del menu, dei pulsanti e dei titoli sta a metà delle maiuscole del testo accanto, entro un pixel. Prima le icone del
menu (uno span con dentro l'icona, poggiata sulla linea di base del testo) stavano 2,5 px più in alto di Porte,
Anagrafica, Da Registrare e degli altri nomi, e l'icona di ☰ 3,25 px più in alto di Controllo Accessi. Lo stesso nella
finestra Colonne dell'Elenco dell'Anagrafica: la maniglia di ogni riga accanto al nome della colonna e l'icona della
maniglia nel suggerimento in cima."""

import json
import subprocess

import pytest
from test_direct import ADMIN, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright

ALIGN = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
function measure() {
  const ctx = document.createElement("canvas").getContext("2d");
  const out = [];
  const shown = (el) => {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cs = getComputedStyle(e);
      if (cs.display === "none" || cs.visibility === "hidden" || Number(cs.opacity) === 0) return false;
    }
    return true;
  };
  for (const svg of document.querySelectorAll("svg.icon")) {
    const r = svg.getBoundingClientRect();
    const host = svg.closest(".nav a, .nav-toggle, button, .btn, h2");
    if (!r.height || !host || svg.closest(".count") || !shown(svg)) continue;
    // Il testo subito a destra dell'icona, sulla stessa riga
    const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    let best = null;
    while (walker.nextNode()) {
      const t = walker.currentNode;
      if (!t.textContent.trim() || t.parentElement.closest("svg, .count") || !shown(t.parentElement)) continue;
      const range = document.createRange();
      range.selectNodeContents(t);
      for (const rect of range.getClientRects()) {
        const gap = rect.left - r.right;
        if (gap < -1 || gap > 24 || rect.bottom < r.top - 6 || rect.top > r.bottom + 6) continue;
        if (!best || gap < best.gap) best = {gap, rect, el: t.parentElement, text: t.textContent.trim()};
      }
    }
    if (!best) continue;
    const cs = getComputedStyle(best.el);
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    // Linea di base: in alto del testo più la salita del carattere; metà delle maiuscole: metà dell'altezza di «H»
    const baseline = best.rect.top + ctx.measureText(best.text).fontBoundingBoxAscent;
    const cap = baseline - ctx.measureText("H").actualBoundingBoxAscent / 2;
    out.push({menu: Boolean(host.closest(".nav")), text: best.text, delta: +(r.top + r.height / 2 - cap).toFixed(2)});
  }
  return out;
}
// Colonne dell'Elenco: la maniglia di ogni riga rispetto al nome della colonna (dopo la casella, troppo lontano per
// measure), e l'icona nel suggerimento rispetto al testo della sua riga
function measureColumns() {
  const ctx = document.createElement("canvas").getContext("2d");
  const delta = (svg, host) => {
    const r = svg.getBoundingClientRect();
    const walker = document.createTreeWalker(host, NodeFilter.SHOW_TEXT);
    let best = null;
    while (walker.nextNode()) {
      const t = walker.currentNode;
      if (!t.textContent.trim() || t.parentElement.closest("svg")) continue;
      const range = document.createRange();
      range.selectNodeContents(t);
      for (const rect of range.getClientRects()) {
        const overlap = Math.min(rect.bottom, r.bottom) - Math.max(rect.top, r.top);
        if (overlap <= 0 || (best && overlap <= best.overlap)) continue;
        best = {overlap, rect, el: t.parentElement, text: t.textContent.trim()};
      }
    }
    const cs = getComputedStyle(best.el);
    ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const baseline = best.rect.top + ctx.measureText(best.text).fontBoundingBoxAscent;
    const cap = baseline - ctx.measureText("H").actualBoundingBoxAscent / 2;
    return +(r.top + r.height / 2 - cap).toFixed(2);
  };
  const out = [...document.querySelectorAll(".p-col")].map((li) => {
    const name = li.querySelector(".p-col-name > span");
    return {menu: false, text: name.textContent, delta: delta(li.querySelector(".p-col-grip svg.icon"), name)};
  });
  const hint = document.querySelector(".p-cols-hint");
  out.push({menu: false, text: "suggerimento", delta: delta(hint.querySelector("svg.icon"), hint)});
  return out;
}
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const width of [1280, 390]) {
    const context = await browser.newContext({viewport: {width, height: 900}, extraHTTPHeaders: config.headers});
    await context.addInitScript(() => localStorage.setItem("aam-nav", "1"));
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    // Prima delle pagine: sul telefono il menu allargato da ☰ coprirebbe il pulsante Colonne
    await page.goto(`${config.base}/#persone`);
    await page.waitForSelector("#view .card");
    await page.click("#p-cols");
    await page.waitForSelector(".p-cols");
    await page.waitForTimeout(300);
    for (const row of await page.evaluate(measureColumns)) out.push({width, hash: "colonne", ...row});
    await page.keyboard.press("Escape");
    await page.waitForSelector(".p-cols", {state: "hidden"});
    for (const hash of ["#persone", `#persone/${config.person}`, "#regole", "#impostazioni"]) {
      await page.goto(`${config.base}/${hash}`);
      await page.waitForSelector("#view .card, #sheet[open]");
      await page.waitForTimeout(300);
      // Sul telefono il menu si allarga dal pulsante ☰ (non con la scheda della persona aperta sopra)
      if (width === 390 && !hash.includes("/")) { await page.click("#nav-toggle"); await page.waitForTimeout(300); }
      for (const row of await page.evaluate(measure)) out.push({width, hash, ...row});
    }
    // L'icona della fotocamera nel testo del registro tiene il suo spostamento
    out.push({width, errors, camera: await page.evaluate(() => {
      const el = document.createElement("div");
      el.className = "ev";
      el.innerHTML = '<span class="what">Mario <svg class="icon"></svg></span>';
      document.body.append(el);
      const value = getComputedStyle(el.querySelector("svg")).verticalAlign;
      el.remove();
      return value;
    })});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_icons_sit_in_the_middle_of_the_capitals_next_to_them(plant, ingress_server):  # noqa: F811
    ingress, _, _, _ = plant
    person = ingress.post(
        "/api/persons", json={"first_name": "Marta", "last_name": "Colombo", "card": "04a1b2c3"}, headers=ADMIN
    ).json()
    config = {"base": ingress_server, "headers": ADMIN, "person": person["id"]}
    result = subprocess.run(["node", "-e", ALIGN, json.dumps(config)], capture_output=True, text=True, timeout=180)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    rows = [r for r in out if "delta" in r]
    for extra in (r for r in out if "errors" in r):
        assert extra["errors"] == [], extra
        assert extra["camera"] == "-2px", extra
    menu = {(r["width"], r["text"]) for r in rows if r["menu"]}
    names = {"Controllo Accessi", "Cruscotto", "Porte", "Anagrafica", "Da Registrare", "Registro", "Fasce Orarie"}
    for width in (1280, 390):
        assert names <= {text for w, text in menu if w == width}, menu
    # Pulsanti con icona: Nuova Persona, Colonne, Rileggi dal Controller, Aggiungi e Codice della scheda, Esporta…
    assert len([r for r in rows if not r["menu"]]) >= 10, rows
    assert {(r["width"], r["text"]) for r in rows if r["hash"] == "#persone"} >= {(1280, "Colonne"), (390, "Colonne")}
    # Finestra Colonne dell'Elenco: le 15 maniglie e il suggerimento, a 1280 e a 390 px
    columns = [r for r in rows if r["hash"] == "colonne"]
    assert len(columns) == 2 * 16 and {"Cognome", "Email", "suggerimento"} <= {r["text"] for r in columns}, columns
    off = [r for r in rows if abs(r["delta"]) > 1]
    assert off == [], off
