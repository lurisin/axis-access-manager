"""La rotellina di caricamento gira sempre (0.28.6): anche con «Riduci movimento» attivo sul telefono, dove prima
pulsava soltanto e l'archetto azzurro sembrava fermo. In Chromium vero, con lo stile della pagina."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_page_morph import _has_playwright

PAGE = Path(__file__).resolve().parent.parent / "axis_access_manager" / "app" / "static" / "index.html"

SCRIPT = r"""
const { chromium } = require("playwright");
const css = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const out = {};
  for (const motion of ["no-preference", "reduce"]) {
    const page = await browser.newPage({reducedMotion: motion});
    await page.setContent(`<style>${css}</style><span class="spinner"></span>`);
    const angle = () => page.evaluate(() => {
      const m = new DOMMatrix(getComputedStyle(document.querySelector(".spinner")).transform);
      return Math.round(Math.atan2(m.b, m.a) * 180 / Math.PI);
    });
    const name = await page.evaluate(() => getComputedStyle(document.querySelector(".spinner")).animationName);
    const angles = [await angle()];
    for (let i = 0; i < 3; i++) { await page.waitForTimeout(150); angles.push(await angle()); }
    out[motion] = {name, angles};
    await page.close();
  }
  await browser.close();
  console.log(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(not _has_playwright(), reason="Playwright non disponibile")
def test_the_spinner_turns_even_with_reduced_motion():
    css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", PAGE.read_text(encoding="utf-8"), re.S))
    done = subprocess.run(["node", "-e", SCRIPT, json.dumps(css)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    for motion, seen in out.items():
        assert seen["name"] == "spin", motion
        # L'angolo cambia a ogni lettura: la rotellina gira davvero
        assert len(set(seen["angles"])) == len(seen["angles"]), (motion, seen["angles"])
