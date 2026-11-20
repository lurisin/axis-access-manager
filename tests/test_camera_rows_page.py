"""Righe delle schede Telecamere e Videocitofoni di un controller sul telefono (0.28.10), nella pagina vera in Chromium
a 360, 390 e 1280 px, in chiaro e in scuro. Prima Modifica e Togli stavano accanto al nome anche sul telefono: il nome
restava in una colonna di 30-60 px e andava a capo dentro le parole («Elettric-o»). Ora, come nelle altre liste con i
pulsanti (Tessere, Da Registrare, Backup, Utenti), sul telefono i pulsanti vanno sotto e il nome usa tutta la riga."""

import json
import subprocess

import pytest
from test_direct import ADMIN, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright

# Telecamere di Home Assistant con i nomi lunghi di un teatro (inventati)
CAMERAS = [
    {"entity_id": "camera.artisti", "name": "Ingresso Artisti Lato Palcoscenico", "state": "streaming"},
    {"entity_id": "camera.quadro", "name": "Quadro Elettrico Sottoscala", "state": "streaming"},
    {"entity_id": "camera.cortile", "name": "Cortile Interno", "state": "streaming"},
]
WARNING = "Cortile Interno Da Controllare"

ROWS = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
// Ogni riga dell'elenco: il nome e il blocco di testo rispetto alla riga, i pulsanti, le parole spezzate a metà
function measure() {
  return [...document.querySelectorAll("#view .list > .item")].map((row) => {
    const box = row.getBoundingClientRect();
    const right = box.right - parseFloat(getComputedStyle(row).paddingRight);
    const title = row.querySelector(".title");
    const text = row.querySelector(".grow").getBoundingClientRect();
    const name = title.getBoundingClientRect();
    const buttons = [...row.querySelectorAll("button")].map((b) => b.getBoundingClientRect());
    // Una parola del nome su due righe: più di un rettangolo per la stessa parola
    const split = [];
    const walker = document.createTreeWalker(title, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.parentElement.closest(".pill")) continue;
      for (const m of node.textContent.matchAll(/\S+/g)) {
        const range = document.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        if (range.getClientRects().length > 1) split.push(m[0]);
      }
    }
    return {
      name: title.textContent.trim(),
      buttons: buttons.length,
      // Il testo arriva fino al bordo destro della riga (non schiacciato dai pulsanti)
      full: text.right >= right - 1,
      below: buttons.every((b) => b.top >= text.bottom - 1),
      beside: buttons.every((b) => b.top < name.bottom && b.bottom > name.top),
      split,
    };
  });
}
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const [width, scheme] of [[360, "dark"], [390, "light"], [1280, "light"], [1280, "dark"]]) {
    const context = await browser.newContext({viewport: {width, height: 844}, colorScheme: scheme,
      extraHTTPHeaders: config.headers});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    for (const [tab, button] of [["telecamere", "[data-cam-edit]"], ["citofoni", "[data-ic-edit]"]]) {
      await page.goto(`${config.base}/#impianti/c${config.controller}/${tab}`);
      await page.waitForSelector(button);
      await page.waitForTimeout(200);
      const scroll = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      out.push({width, scheme, tab, scroll, rows: await page.evaluate(measure)});
    }
    // I pulsanti sotto il nome aprono ancora la finestra
    await page.goto(`${config.base}/#impianti/c${config.controller}/telecamere`);
    await page.click(`[data-cam-edit="${config.door}"]`);
    await page.waitForSelector("#modal [name=camera]");
    out.push({width, errors});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_camera_and_intercom_rows_leave_the_name_the_whole_line_on_the_phone(plant, ingress_server, monkeypatch):  # noqa: F811
    """Sul telefono (360 e 390 px) il nome della telecamera o del videocitofono arriva fino al bordo della riga, va a
    capo solo tra le parole, e Modifica e Togli stanno sotto; sul computer restano sulla riga del nome. Anche con la
    riga più larga: «Da Controllare» e l'ultima foto non riuscita. Mai scorrimento orizzontale."""
    ingress, _, _, app = plant

    async def states():
        return CAMERAS

    monkeypatch.setattr(app.state.ha, "states", states)
    [ctrl] = ingress.get("/api/installation", headers=ADMIN).json()["controllers"]
    door = next(d for d in ctrl["doors"] if d["door_token"] == "porta")
    body = {"door_token": "porta", "name": door["name"], "snapshot_camera": "camera.artisti"}
    body |= {"snapshot_on_granted": True, "snapshot_delay": 2.5, "intercom_name": "Citofono Ingresso Artisti"}
    assert ingress.put(f"/api/doors/{door['id']}", json=body, headers=ADMIN).status_code == 200
    cellar = {"door_token": "porta2", "name": "Cantina", "snapshot_camera": "camera.cortile"}
    assert ingress.post(f"/api/controllers/{ctrl['id']}/doors", json=cellar, headers=ADMIN).status_code == 200
    cover = ingress.put(f"/api/controllers/{ctrl['id']}/camera", json={"camera": "camera.quadro"}, headers=ADMIN)
    assert cover.status_code == 200
    error = {"ts": "2026-11-20T08:00:00+00:00", "error": "Home Assistant non risponde (tempo scaduto)"}
    monkeypatch.setitem(app.state.snapshot_errors, "camera.cortile", error)
    config = {"base": ingress_server, "headers": ADMIN, "controller": ctrl["id"], "door": door["id"]}
    result = subprocess.run(["node", "-e", ROWS, json.dumps(config)], capture_output=True, text=True, timeout=180)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert [r["errors"] for r in out if "errors" in r] == [[], [], [], []]
    tabs = [r for r in out if "tab" in r]
    assert len(tabs) == 8
    for tab in tabs:
        assert not tab["scroll"], tab
        names = [row["name"] for row in tab["rows"]]
        if tab["tab"] == "telecamere":
            # Il coperchio del controller (cameraRow) e le due porte, con «Da Controllare» sulla telecamera in errore
            assert names == ["Quadro Elettrico Sottoscala", "Ingresso Artisti Lato Palcoscenico", WARNING], tab
        else:
            assert names == ["Citofono Ingresso Artisti"], tab
        for row in tab["rows"]:
            assert row["buttons"] == 2 and row["split"] == [], (tab["width"], tab["tab"], row)
            if tab["width"] < 1000:
                assert row["full"] and row["below"], (tab["width"], tab["tab"], row)
            else:
                assert row["beside"], (tab["width"], tab["tab"], row)
