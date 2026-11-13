"""Elenco Persone nella pagina vera, in Chromium, con il menu com'è davvero: le colonne che restano a 900, 1024 e 1280
px, con il menu aperto (com'è la prima volta, senza niente salvato nel browser) e chiuso, ordinate per Cognome, per
Ultima Modifica, per Ultimo Accesso e per Matricola, con e senza una ricerca che trova delle tessere. Cognome e Nome non
si schiacciano mai sotto un nome leggibile, la colonna Tessere a 1280 col menu aperto c'è, e la pagina non scorre mai di
lato. Solo dati di fantasia."""

import json
import subprocess

import pytest
from test_direct import ADMIN, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright

# Larghezza minima del testo di Cognome (e degli altri campi larghi quanto avanza): un cognome di 8-10 lettere
MIN_TEXT = 75

PERSONS = [
    ("Marta", "Colombo", "04a1b2c3d4e5f6", "Ciccio", "Sala Prove", "0042"),
    ("Paolo", "Esposito", "04b2c3d4e5f6a7", "Cicciobello", "Biglietteria", "0107"),
    ("Giulia", "De Santis", "04c3d4e5f6a7b8", "A-07", "Sartoria", ""),
    ("Luca", "Ferrara", "04d4e5f6a7b8c9", "", "Tecnici di Palco", "0311"),
    ("Sara", "Bernardi", "04e5f6a7b8c9d0", "1234", "", "0099"),
    ("Anna", "Villa", "", "", "Ufficio Stampa", ""),
]

LIST = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const [width, nav, sort, q] of config.cases) {
    const context = await browser.newContext({viewport: {width, height: 800}, extraHTTPHeaders: config.headers});
    // Senza niente salvato il menu sul computer parte aperto (initNav); "0" è il menu chiuso dall'utente
    if (nav !== null) await context.addInitScript((nav) => localStorage.setItem("aam-nav", nav), nav);
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    await page.goto(`${config.base}/#persone`);
    await page.waitForSelector("#p-list table");
    await page.selectOption("#p-sort", sort);
    await page.fill("#p-q", q);
    await page.waitForTimeout(150);
    const result = await page.evaluate(() => {
      const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";
      const pad = (td) => parseFloat(getComputedStyle(td).paddingLeft) + parseFloat(getComputedStyle(td).paddingRight);
      const inner = (td) => td.clientWidth - pad(td);
      const rows = [...document.querySelectorAll(".p-table tbody tr[data-person]")];
      const cells = (cls) => rows.map((tr) => tr.querySelector(`td.${cls}`)).filter((td) => td && visible(td));
      const de = document.documentElement;
      return {
        navOpen: document.body.classList.contains("nav-open"),
        box: Math.round(document.querySelector(".p-table-wrap").getBoundingClientRect().width),
        heads: [...document.querySelectorAll(".p-table thead th")].filter(visible)
          .map((th) => th.textContent.replace(/[↑↓]/g, "").trim()),
        // Il testo più stretto di Cognome e di Nome, e quanti sono tagliati con i puntini
        last: Math.min(...cells("c-last").map(inner)),
        first: Math.min(...cells("c-first").map(inner)),
        cut: cells("c-last").filter((td) => td.scrollWidth > td.clientWidth).map((td) => td.textContent),
        // Numeri delle tessere visibili, e se sono interi
        numbers: [...document.querySelectorAll(".p-card .card-no, .p-card > .mono")].filter(visible)
          .map((el) => [el.textContent, el.scrollWidth <= el.getBoundingClientRect().width + 0.5]),
        rows: rows.length,
        scroll: de.scrollWidth > de.clientWidth,
      };
    });
    out.push({width, nav, sort, q, errors, ...result});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_person_columns_in_the_real_page_with_the_menu_open_and_closed(plant, ingress_server):  # noqa: F811
    ingress, _, _, _ = plant
    for first, last, card, code, company, employee_id in PERSONS:
        body = {"first_name": first, "last_name": last, "company": company, "employee_id": employee_id}
        if card:
            body.update(card=card, printed_code=code)
        response = ingress.post("/api/persons", json=body, headers=ADMIN)
        assert response.status_code == 200, response.text
    cases = [
        [width, nav, sort, q]
        for width in (900, 1024, 1280)
        for nav in (None, "0")
        for sort in ("cognome:asc", "modifica:desc")
        for q in ("", "cic")
    ]
    # Con la colonna dell'ordine che resta sempre (Ultimo Accesso, Matricola) e la ricerca che tiene Tessere
    cases += [
        [width, nav, sort, "cic"]
        for width in (900, 1024, 1280)
        for nav in (None, "0")
        for sort in ("accesso:desc", "matricola:asc")
    ]
    config = {"base": ingress_server, "headers": ADMIN, "cases": cases}
    result = subprocess.run(["node", "-e", LIST, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert len(out) == len(cases)
    for case in out:
        name = f"{case['width']} px, menu {'chiuso' if case['nav'] else 'predefinito'}, {case['sort']}, «{case['q']}»"
        assert case["errors"] == [], name
        # Senza niente salvato nel browser il menu sul computer è aperto
        assert case["navOpen"] is (case["nav"] is None), name
        assert not case["scroll"], name
        assert {"Cognome", "Nome"} <= set(case["heads"]), name
        # Cognome e Nome leggibili, anche quando la ricerca tiene in vista Tessere e con la colonna della data
        assert case["last"] >= MIN_TEXT and case["first"] >= MIN_TEXT, (name, case)
        assert case["cut"] == [], (name, case)
        # La colonna dell'ordine scelto resta sempre
        column = {"modifica:desc": "Ultima Modifica", "accesso:desc": "Ultimo Accesso", "matricola:asc": "Matricola"}
        assert column.get(case["sort"], "Cognome") in case["heads"], name
        if case["q"]:
            # La ricerca trova Ciccio e Cicciobello: Tessere in vista, con i numeri interi
            assert case["rows"] == 2 and "Tessere" in case["heads"], name
            assert sorted(n for n, _ in case["numbers"]) == ["04a1b2c3d4e5f6", "04b2c3d4e5f6a7"], name
            assert all(whole for _, whole in case["numbers"]), name
    by = {(c["width"], c["nav"], c["sort"], c["q"]): c for c in out}
    # 1280 px col menu aperto, com'è la prima volta: riquadro di circa 1000 px, con Tessere e i numeri interi
    wide = by[(1280, None, "cognome:asc", "")]
    assert 990 <= wide["box"] <= 1010
    assert wide["heads"] == [
        "Foto",
        "Cognome",
        "Nome",
        "Matricola",
        "Ditta",
        "Tipologia",
        "Tessere",
        "Ultimo Accesso",
        "Stato",
    ]
    assert len(wide["numbers"]) == 5 and all(whole for _, whole in wide["numbers"])
    assert "Tessere" in by[(1280, "0", "cognome:asc", "")]["heads"]
    # Riquadro stretto (900 px col menu aperto): con la ricerca Tessere prende il posto di Ultimo Accesso
    narrow = by[(900, None, "cognome:asc", "cic")]
    assert "Tessere" in narrow["heads"] and "Ultimo Accesso" not in narrow["heads"]
    assert "Tessere" not in by[(900, None, "cognome:asc", "")]["heads"]
