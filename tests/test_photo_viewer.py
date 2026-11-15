"""Foto ingrandita (0.28.4) nella pagina vera, in Chromium a 390 e 1280 px, in chiaro e in scuro: una finestra larga
quanto la foto, al centro su sfondo scuro, alla grandezza della foto e mai più grande dello schermo meno i margini,
senza deformarla. Prima la finestra era sempre larga al massimo e la foto restava a sinistra con lo spazio vuoto a
destra. Si apre dalla Foto della Persona nella scheda, dalla foto della telecamera nel Dettaglio Evento (anche da
tastiera), dalla Foto in Anagrafica, dalle notifiche e, sul computer, dalla colonna Foto dell'elenco Persone (senza
aprire la scheda); si chiude con Esc, con un clic fuori e con la ×, il fuoco resta
sulla × e poi torna su chi l'ha aperta, e la finestra di sotto (scheda, Dettaglio Evento) resta. Mentre carica c'è la
rotellina, e una foto che non arriva dice «Foto non disponibile». Solo immagini di fantasia disegnate qui."""

import json
import struct
import subprocess
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from test_direct import ADMIN, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright


def plain_png(width: int, height: int, shade: int) -> bytes:
    """PNG in scala di grigi a fasce, scritto a mano: nessuna dipendenza e nessuna persona vera."""
    rows = b"".join(bytes([0]) + bytes([shade if (y // 40) % 2 else shade + 30]) * width for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


VIEWER = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const [width, scheme] of [[390, "light"], [390, "dark"], [1280, "light"], [1280, "dark"]]) {
    const height = width === 390 ? 844 : 900;
    const context = await browser.newContext({
      viewport: {width, height}, colorScheme: scheme, extraHTTPHeaders: config.headers,
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const shown = async (name) => {
      await page.waitForFunction(() => { const d = document.querySelector("#viewer");
        return d.open && (d.querySelector("img") || d.querySelector(".viewer-wait.failed")); }, null, {timeout: 10000});
      await page.waitForTimeout(100);
      const info = await page.evaluate(() => {
        const d = document.querySelector("#viewer"), img = d.querySelector("img");
        const box = (el) => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; };
        return {dialog: box(d), img: img && box(img), natural: img && [img.naturalWidth, img.naturalHeight],
          title: d.querySelector("h2").textContent, focus: document.activeElement.className,
          wait: d.querySelector(".viewer-wait")?.textContent.trim() || "",
          backdrop: getComputedStyle(d, "::backdrop").backgroundColor,
          scroll: document.documentElement.scrollWidth > document.documentElement.clientWidth};
      });
      out.push({width, scheme, name, ...info});
    };
    const after = async (name) => {
      await page.waitForFunction(() => !document.querySelector("#viewer").open, null, {timeout: 3000});
      out.push({width, scheme, name, closed: await page.evaluate(() => ({focus: document.activeElement.className,
        row: document.activeElement.dataset?.person || "",
        sheet: document.querySelector("#sheet").open, modal: document.querySelector("#modal").open,
        empty: document.querySelector("#viewer").childElementCount === 0}))});
    };
    // Foto della Persona dalla scheda: Tab resta sulla ×, Esc chiude e il fuoco torna sulla foto piccola
    await page.goto(`${config.base}/#persone/${config.person}`);
    await page.locator("#sheet .dlg-head [data-photo-view]").click();
    await shown("persona");
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    out.push({width, scheme, name: "tab", focus: await page.evaluate(() => document.activeElement.className)});
    await page.keyboard.press("Escape");
    await after("persona");
    // Foto della telecamera nel Dettaglio Evento, da tastiera; si chiude con la ×
    await page.goto(`${config.base}/#registro`);
    await page.locator(`.list.compact > .ev[data-event="${config.event}"]`).click();
    const detail = width >= 1100 ? "#l-detail" : "#modal";
    await page.locator(`${detail} img[data-snap]`).focus();
    await page.keyboard.press("Enter");
    await shown("telecamera");
    await page.locator("#viewer .viewer-x").click();
    await after("telecamera");
    // Foto in Anagrafica nel Dettaglio Evento: si chiude con un clic fuori
    await page.locator(`${detail} .ev-face-img`).click();
    await shown("anagrafica");
    await page.mouse.click(4, 4);
    await after("anagrafica");
    // Notifiche: la foto dell'allarme
    await page.goto(`${config.base}/#notifiche`);
    await page.locator("img.n-snap").first().click();
    await shown("notifica");
    await page.keyboard.press("Escape");
    await after("notifica");
    // Sul computer, la foto della colonna Foto dell'elenco Persone: si ingrandisce senza aprire la scheda, e il fuoco
    // torna sulla riga
    if (width >= 1000) {
      await page.goto(`${config.base}/#persone`);
      await page.locator(`#p-list tr[data-person="${config.person}"] td.c-photo img`).click();
      await shown("elenco");
      await page.keyboard.press("Escape");
      await after("elenco");
    }
    // Caricamento lento, poi una foto che non arriva
    await page.route("**/api/events/*/snapshot", async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.continue();
    });
    await page.evaluate((id) => showImage(`api/events/${id}/snapshot`, "Foto lenta"), config.event);
    await page.waitForTimeout(300);
    const wait = await page.evaluate(() => document.querySelector("#viewer .viewer-wait")?.textContent.trim());
    out.push({width, scheme, name: "caricamento", wait});
    await page.keyboard.press("Escape");
    await page.unroute("**/api/events/*/snapshot");
    await page.route("**/api/events/*/snapshot", (route) => route.fulfill({status: 404, body: ""}));
    await page.evaluate((id) => showImage(`api/events/${id}/snapshot`, "Foto che non arriva"), config.event);
    await shown("non disponibile");
    await page.keyboard.press("Escape");
    out.push({width, scheme, errors});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_enlarged_photo_is_as_wide_as_the_photo(plant, ingress_server, tmp_path):  # noqa: F811
    ingress, _, _, app = plant
    state = app.state
    state.ha.snapshot_dir = tmp_path / "foto"
    state.ha.snapshot_dir.mkdir()
    person = ingress.post(
        "/api/persons", json={"first_name": "Marta", "last_name": "Colombo", "card": "04a1b2c3"}, headers=ADMIN
    ).json()
    photo = ingress.put(
        f"/api/persons/{person['id']}/photo",
        content=plain_png(480, 600, 120),
        headers={**ADMIN, "Content-Type": "image/png"},
    )
    assert photo.status_code == 200, photo.text
    door = state.doors[0]["door_token"]
    now = datetime.now(UTC)
    camera = plain_png(1920, 1080, 60)
    alarm = state.db.add_event(
        ts=(now - timedelta(minutes=5)).isoformat(), kind="allarme", door=door, detail="porta forzata"
    )
    event = state.db.add_event(
        ts=now.isoformat(),
        kind="negato",
        detail="fuori fascia oraria",
        door=door,
        card="04A1B2C3",
        person_id=person["id"],
        person_name="Marta Colombo",
    )
    for event_id in (alarm, event):
        (state.ha.snapshot_dir / f"{event_id}_foto.png").write_bytes(camera)
        state.db.set_event_snapshot(event_id, f"{event_id}_foto.png")
    config = {"base": ingress_server, "headers": ADMIN, "person": person["id"], "event": event}
    result = subprocess.run(["node", "-e", VIEWER, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    for row in out:
        width = row["width"]
        height = 844 if width == 390 else 900
        if "errors" in row:
            assert row["errors"] == [], row
        elif "closed" in row:
            closed = row["closed"]
            assert closed["empty"], row
            # Il fuoco torna su chi l'ha aperta, e la finestra di sotto resta aperta
            back = {"persona": "avatar", "telecamera": "thumb", "anagrafica": "ev-face-img", "notifica": "n-snap"}
            if row["name"] == "elenco":
                assert closed["row"] == str(person["id"]), row
            else:
                assert back[row["name"]] in closed["focus"], row
            assert closed["sheet"] is (row["name"] == "persona"), row
            assert closed["modal"] is (width == 390 and row["name"] in ("telecamera", "anagrafica")), row
        elif row["name"] == "tab":
            assert row["focus"] == "viewer-x", row
        elif row["name"] == "caricamento":
            assert row["wait"] == "Caricamento della foto…", row
        else:
            box, img = row["dialog"], row["img"]
            assert row["focus"] == "viewer-x", row
            assert not row["scroll"], row
            # Al centro dello schermo, dentro lo schermo
            assert abs(box["x"] + box["w"] / 2 - width / 2) <= 1 and abs(box["y"] + box["h"] / 2 - height / 2) <= 1, row
            assert box["x"] >= 8 and box["y"] >= 8 and box["x"] + box["w"] <= width - 8, row
            assert row["backdrop"] in ("rgba(6, 8, 12, 0.9)",), row
            if row["name"] == "non disponibile":
                assert img is None and row["wait"] == "Foto non disponibile", row
                continue
            # Finestra larga quanto la foto, foto non deformata, grande al massimo come la foto vera
            assert abs(box["w"] - img["w"]) <= 1, row
            natural_w, natural_h = row["natural"]
            assert abs(img["w"] / img["h"] - natural_w / natural_h) < 0.01, row
            expected = min(natural_w, width - 32, (height - 92) * natural_w / natural_h)
            assert abs(img["w"] - expected) <= 1, row
    names = [row["name"] for row in out if "dialog" in row]
    phone = ["persona", "telecamera", "anagrafica", "notifica", "non disponibile"]
    computer = ["persona", "telecamera", "anagrafica", "notifica", "elenco", "non disponibile"]
    assert names == phone * 2 + computer * 2


STILL = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const [width, scheme] of [[390, "dark"], [1280, "light"]]) {
    const phone = width === 390;
    const context = await browser.newContext({viewport: {width, height: phone ? 844 : 900}, colorScheme: scheme,
      hasTouch: phone, isMobile: phone, extraHTTPHeaders: config.headers});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    // La scheda della persona sopra l'Anagrafica, che è lunga: la foto della persona si apre dalla scheda
    await page.goto(`${config.base}/#persone/${config.person}`);
    const opener = page.locator("#sheet .dlg-head [data-photo-view]");
    await opener.waitFor();
    await page.waitForTimeout(300);
    const look = () => page.evaluate(() => ({y: Math.round(scrollY), open: document.querySelector("#viewer").open,
      lock: document.documentElement.className, width: document.querySelector(".main").clientWidth}));
    const before = await look();
    if (phone) await opener.tap(); else await opener.click();
    await page.waitForFunction(() => document.querySelector("#viewer img"));
    const opened = await look();
    if (phone) {
      // Un dito che trascina in su sullo sfondo scuro, sotto la foto
      const cdp = await context.newCDPSession(page);
      const x = 195, from = 820, to = 420;
      await cdp.send("Input.dispatchTouchEvent", {type: "touchStart", touchPoints: [{x, y: from}]});
      for (let i = 1; i <= 10; i++) {
        await page.waitForTimeout(16);
        const touchPoints = [{x, y: from + (to - from) * i / 10}];
        await cdp.send("Input.dispatchTouchEvent", {type: "touchMove", touchPoints});
      }
      await cdp.send("Input.dispatchTouchEvent", {type: "touchEnd", touchPoints: []});
    } else {
      // La rotella del mouse sullo sfondo
      await page.mouse.move(40, 450);
      await page.mouse.wheel(0, 800);
    }
    await page.waitForTimeout(500);
    const moved = await look();
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.querySelector("#viewer").open);
    const closed = await look();
    // Chiusa la foto la pagina scorre di nuovo
    await page.evaluate(() => window.scrollTo(0, 300));
    await page.waitForTimeout(100);
    out.push({width, before, opened, moved, closed, again: await page.evaluate(() => Math.round(scrollY)), errors});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_page_under_the_enlarged_photo_does_not_scroll(plant, ingress_server, tmp_path):  # noqa: F811
    """Revisione: sul telefono un trascinamento sullo sfondo scuro, e sul computer la rotella, facevano scorrere la
    pagina sotto la foto (più di 600 px): chiudendo la foto l'elenco non era più dove era. Ora resta ferma, e la
    pagina non cambia larghezza mentre la foto è aperta."""
    ingress, _, _, app = plant
    app.state.ha.snapshot_dir = tmp_path / "foto"
    app.state.ha.snapshot_dir.mkdir()
    person = ingress.post(
        "/api/persons", json={"first_name": "Marta", "last_name": "Colombo", "card": "04a1b2c3"}, headers=ADMIN
    ).json()
    photo = ingress.put(
        f"/api/persons/{person['id']}/photo",
        content=plain_png(480, 600, 120),
        headers={**ADMIN, "Content-Type": "image/png"},
    )
    assert photo.status_code == 200, photo.text
    for i in range(40):
        body = {"first_name": "Persona", "last_name": f"Prova{i:02d}"}
        assert ingress.post("/api/persons", json=body, headers=ADMIN).status_code == 200
    config = {"base": ingress_server, "headers": ADMIN, "person": person["id"]}
    result = subprocess.run(["node", "-e", STILL, json.dumps(config)], capture_output=True, text=True, timeout=300)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    for row in json.loads(result.stdout):
        assert row["errors"] == [], row
        assert row["opened"]["open"] and row["moved"]["open"], row
        assert row["moved"]["y"] == row["opened"]["y"] == row["before"]["y"], row
        assert "viewer-open" in row["opened"]["lock"] and "viewer-open" not in row["closed"]["lock"], row
        # La pagina sotto non cambia larghezza aprendo e chiudendo la foto
        assert row["opened"]["width"] == row["before"]["width"] == row["closed"]["width"], row
        assert row["again"] == 300, row
