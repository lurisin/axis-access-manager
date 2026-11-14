"""Suggerimento del Codice Stampato (0.28.4): senza gli esempi inventati della 0.28.3 («Ciccio», «1234»,
«Cicciobello») in tutto quello che il titolare può leggere (pagina, messaggi e commenti del server, DOCS, CHANGELOG che
Home Assistant mostra all'aggiornamento, opzioni dell'add-on), e lo stesso testo professionale in Nuova Persona, Tessere
da Registrare, finestra Codice e Aggiungi Tessera. Niente «1234» neanche dentro un numero di esempio («+39 333 1234567»,
«04123456»): in un software professionale gli esempi sono neutri (+39 333 5550190, A-07). Nei test i valori restano:
sono dati scritti dagli utenti."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_direct import ADMIN, clients, fast_hash  # noqa: F401
from test_direct_plain import plant  # noqa: F401
from test_direct_plain_page import ingress_server  # noqa: F401
from test_page_morph import _has_playwright
from test_page_time_zone import needs_node

ROOT = Path(__file__).resolve().parents[1]
ADDON = ROOT / "axis_access_manager"
PAGE = ADDON / "app" / "static" / "index.html"
HINT = "Il codice scritto sulla tessera. Resta solo nell'app."
# Esempi buttati lì: «Ciccio», e «1234» anche dentro le parole e i numeri («+39 333 1234567», «04123456»,
# «password1234»), o le cifre in fila con il punto delle migliaia («06.123.456»)
EXAMPLES = re.compile(r"ciccio|1234|123\.456", re.I)
# L'elenco delle password deboli di direct.py, che non si vede: le password che l'accesso diretto rifiuta
WEAK_PASSWORDS = re.compile(r"^COMMON_PASSWORDS = \{\n.*?^\}$", re.M | re.S)


def _texts() -> list[Path]:
    return [
        PAGE,
        ADDON / "DOCS.md",
        ADDON / "CHANGELOG.md",
        ROOT / "README.md",
        ADDON / "config.yaml",
        *sorted((ADDON / "translations").glob("*.yaml")),
        *sorted((ADDON / "app").glob("*.py")),
    ]


def _readable(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if path.name == "direct.py":
        # Le righe dell'elenco diventano vuote: i numeri delle righe dopo restano quelli del file
        text = WEAK_PASSWORDS.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    return text


def test_no_made_up_examples_anywhere_the_owner_can_read():
    found = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}"
        for path in _texts()
        for number, line in enumerate(_readable(path).splitlines(), 1)
        if EXAMPLES.search(line)
    ]
    assert found == []


def test_the_check_finds_1234_inside_words_but_not_in_the_weak_passwords():
    """Il controllo di sopra trova «1234» anche dentro un numero o una parola; l'elenco delle password deboli di
    direct.py, che ne ha (password1234, 1234567890), resta fuori, e solo lui."""
    for text in ("+39 333 1234567", 'come `="04123456"`', "password1234", "«06.123.456»", "Codice Ciccio"):
        assert EXAMPLES.search(text), text
    for text in ("+39 333 5550190", 'come `="04705918"`', "A-07", "«06.555.019»", "matricola 000417"):
        assert not EXAMPLES.search(text), text
    direct = (ADDON / "app" / "direct.py").read_text(encoding="utf-8")
    weak = WEAK_PASSWORDS.search(direct)
    assert weak and '"password1234"' in weak.group(0) and '"1234567890"' in weak.group(0)
    readable = _readable(ADDON / "app" / "direct.py")
    assert "1234" not in readable and readable.count("\n") == direct.count("\n")
    assert readable.replace("\n", "") == direct.replace(weak.group(0), "").replace("\n", "")


def test_every_printed_code_field_uses_the_same_hint():
    """Le quattro finestre passano dal suggerimento unico: printedCodeField() o PRINTED_CODE_HINT con un'aggiunta."""
    page = PAGE.read_text(encoding="utf-8")
    calls = re.findall(r"printedCodeField\(([^`]*?)\)(?=[}\s:]|$)", page)
    assert len(calls) == 4, calls
    for args in calls:
        assert args == "" or "PRINTED_CODE_HINT" in args, args
    assert f'const PRINTED_CODE_HINT = "{HINT}";' in page


@needs_node
def test_printed_code_field_has_a_neutral_placeholder_and_the_hint():
    page = PAGE.read_text(encoding="utf-8")
    parts = [
        re.search(r"^const esc = .*$", page, re.M).group(0),
        re.search(r"^const field = .*$", page, re.M).group(0),
        re.search(r"^const PRINTED_CODE_HINT = .*$", page, re.M).group(0),
        re.search(r"^const printedCodeField = .*?;$", page, re.M | re.S).group(0),
        "console.log(JSON.stringify([printedCodeField(), printedCodeField('A-07', PRINTED_CODE_HINT + ' Vuoto per "
        "toglierlo.')]));",
    ]
    result = subprocess.run(["node", "-e", "\n".join(parts)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    empty, edit = json.loads(result.stdout)
    assert 'placeholder="facoltativo"' in empty
    assert f'<p class="hint">{HINT}</p>' in empty
    assert 'value="A-07"' in edit and f"{HINT} Vuoto per toglierlo." in edit


DIALOGS = r"""
const { chromium } = require("playwright");
const config = JSON.parse(process.argv[1]);
(async () => {
  const browser = await chromium.launch();
  const out = [];
  for (const width of [390, 1280]) {
    const context = await browser.newContext({viewport: {width, height: 800}, extraHTTPHeaders: config.headers});
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    const read = async (name) => {
      const dlg = page.locator("#modal");
      await dlg.locator('[name="printed_code"]').waitFor();
      const box = await dlg.evaluate((d) => {
        const input = d.querySelector('[name="printed_code"]');
        const field = input.closest(".field");
        return {
          title: d.querySelector(".dlg-head h2").textContent,
          placeholder: input.placeholder,
          hint: field.querySelector(".hint")?.textContent || "",
          all: d.textContent,
          scroll: d.scrollWidth > d.clientWidth,
        };
      });
      out.push({width, name, ...box});
      await page.keyboard.press("Escape");
      await dlg.waitFor({state: "hidden"});
    };
    await page.goto(`${config.base}/#da-registrare`);
    await page.locator("[data-new]").first().click();
    await read("Nuova Persona");
    await page.locator("[data-assign]").first().click();
    await read("A Persona Esistente");
    await page.goto(`${config.base}/#persone/${config.person}`);
    await page.locator("#sheet [data-add-card]").click();
    await read("Aggiungi Tessera");
    await page.locator("#sheet [data-card-code]").first().click();
    await read("Codice");
    out.push({width, errors});
    await context.close();
  }
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_the_four_dialogs_show_the_same_professional_hint(plant, ingress_server):  # noqa: F811
    """Nella pagina vera, a 390 e a 1280 px: Nuova Persona e A Persona Esistente da Tessere da Registrare, Aggiungi
    Tessera e Codice dalla scheda. Stesso suggerimento, segnaposto «facoltativo» e nessun esempio inventato."""
    ingress, _, _, app = plant
    person = ingress.post(
        "/api/persons",
        json={"first_name": "Marta", "last_name": "Colombo", "card": "04a1b2c3", "printed_code": "A-07"},
        headers=ADMIN,
    ).json()
    app.state.db.note_unknown_card("04ddeeff", "2026-11-14T08:00:00+00:00")
    config = {"base": ingress_server, "headers": ADMIN, "person": person["id"]}
    result = subprocess.run(["node", "-e", DIALOGS, json.dumps(config)], capture_output=True, text=True, timeout=180)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    dialogs = [d for d in out if "name" in d]
    assert [d["errors"] for d in out if "errors" in d] == [[], []]
    assert [(d["width"], d["name"]) for d in dialogs] == [
        (width, name)
        for width in (390, 1280)
        for name in ("Nuova Persona", "A Persona Esistente", "Aggiungi Tessera", "Codice")
    ]
    for d in dialogs:
        assert d["placeholder"] == "facoltativo", d
        expected = HINT + (" Vuoto per toglierlo." if d["name"] == "Codice" else "")
        assert d["hint"] == expected, d
        assert not EXAMPLES.search(d["all"]), d
        assert not d["scroll"], d
