"""Aggiornamento senza ricostruire la pagina (morph di index.html), preso dalla pagina com'è. Una riga che cambia posto
(l'elenco Persone ordinato per Ultima Modifica o Ultimo Accesso, dopo un passaggio o una modifica) si sposta da sola:
prima morphChildren spostava davanti a lei, una alla volta, tutte le righe che la seguivano (2017 su 2018 con 2000
persone), e chi aveva il fuoco su una di quelle lo perdeva. Il fuoco resta sulla riga scelta anche quando è lei a
spostarsi.

La prima parte gira con node e un albero finto (conta le mosse); la seconda con Chromium vero (Playwright, se c'è),
perché solo il browser toglie il fuoco a un elemento spostato."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "axis_access_manager" / "app" / "static" / "index.html"
NAMES = ("nodeKey", "morph", "morphChildren", "steadyNodes", "morphNode")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="serve node")


def _declarations(names) -> str:
    """Le dichiarazioni names della pagina com'è: funzioni (fino alla graffa in colonna zero) e costanti di una riga.
    Quelle che non ci sono si saltano (steadyNodes prima della correzione)."""
    page = PAGE.read_text(encoding="utf-8")
    found = []
    for name in names:
        block = re.search(rf"^function {name}\(.*?^\}}$", page, re.M | re.S) or re.search(
            rf"^(?:const|let) {name} = .*$", page, re.M
        )
        if block:
            found.append(block.group(0))
    return "\n".join(found)


# Albero finto: elementi con data-key e un testo, un contenitore che conta le righe spostate (insertBefore di una riga
# che c'era già) e che, come il browser, toglie il fuoco a una riga che esce dal suo posto
TREE = r"""
const document = {body: {}, activeElement: null};
document.activeElement = document.body;
let moves = 0;
class Text {
  constructor(value) { this.nodeType = 3; this.nodeName = "#text"; this.nodeValue = value; this.parentNode = null; }
  get nextSibling() { return sibling(this); }
  remove() { detach(this); }
}
class El {
  constructor(key, text) {
    this.nodeType = 1; this.nodeName = this.tagName = "DIV"; this.dataset = key ? {key} : {};
    this.attributes = []; this.childNodes = []; this.parentNode = null;
    if (text !== undefined) this.append(new Text(text));
  }
  get children() { return this.childNodes.filter((c) => c.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() { return sibling(this); }
  get text() { return this.childNodes.map((c) => c.nodeValue).join(""); }
  append(node) { this.insertBefore(node, null); }
  insertBefore(node, ref) {
    if (node.parentNode === this) moves++;
    detach(node);
    const at = ref ? this.childNodes.indexOf(ref) : this.childNodes.length;
    this.childNodes.splice(at, 0, node);
    node.parentNode = this;
    return node;
  }
  remove() { detach(this); }
  replaceWith(node) { this.parentNode.insertBefore(node, this); detach(this); }
  isEqualNode(other) { return other.dataset.key === this.dataset.key && other.text === this.text; }
  hasAttribute() { return false; }
}
function sibling(node) {
  const list = node.parentNode ? node.parentNode.childNodes : [];
  return list[list.indexOf(node) + 1] || null;
}
function detach(node) {
  if (!node.parentNode) return;
  if (document.activeElement === node) document.activeElement = document.body;
  node.parentNode.childNodes.splice(node.parentNode.childNodes.indexOf(node), 1);
  node.parentNode = null;
}
// Elenco di righe: [[chiave, testo], ...]
const list = (rows) => { const box = new El(); rows.forEach(([k, t]) => box.append(new El(k, t))); return box; };
const keys = (box) => box.children.map((c) => c.dataset.key);
const texts = (box) => box.children.map((c) => c.text);
function step(box, rows, focusKey) {
  moves = 0;
  const focused = focusKey ? box.children.find((c) => c.dataset.key === focusKey) : null;
  if (focused) document.activeElement = focused;
  const kept = new Map(box.children.map((c) => [c.dataset.key, c]));
  morphChildren(box, list(rows));
  const same = box.children.filter((c) => kept.get(c.dataset.key) === c).length;
  return {moves, keys: keys(box), texts: texts(box), same,
    focus: document.activeElement === document.body ? "BODY" : document.activeElement.dataset.key};
}
"""


def run_tree(scenarios: dict[str, str]) -> dict:
    lines = [TREE, _declarations(NAMES), "const out = {};"]
    for key, body in scenarios.items():
        name = json.dumps(key)
        lines.append(
            f"try {{ out[{name}] = (() => {{ {body} }})(); }} catch (e) {{ out[{name}] = 'ERRORE ' + e.stack; }}"
        )
    script = "\n".join([*lines, "console.log(JSON.stringify(out));"])
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


ROWS = "const rows = (n) => Array.from({length: n}, (_, i) => ['p' + i, 'Persona ' + i]);"


@needs_node
def test_a_row_that_goes_down_moves_alone_and_the_others_keep_the_focus():
    """Ordine «Meno Recente»: la prima riga, appena modificata, scende in fondo. Si sposta solo lei; il fuoco sulla
    quinta riga resta. Prima si spostavano tutte le altre, compresa quella con il fuoco."""
    out = run_tree(
        {
            "down": ROWS
            + """
                const box = list(rows(10));
                const next = [...rows(10).slice(1), ['p0', 'Persona 0 modificata']];
                return step(box, next, 'p4');
            """,
        }
    )
    assert out["down"] == {
        "moves": 1,
        "keys": [f"p{i}" for i in [*range(1, 10), 0]],
        "texts": [f"Persona {i}" for i in range(1, 10)] + ["Persona 0 modificata"],
        "same": 10,
        "focus": "p4",
    }


@needs_node
def test_a_row_that_goes_up_moves_alone():
    """Ordine «Più Recente»: la riga modificata sale in cima, con una mossa sola; le righe sopra di lei non si
    toccano (chi ha il fuoco su una di loro lo tiene)."""
    out = run_tree(
        {
            "up": ROWS
            + """
                const box = list(rows(10));
                const r = rows(10);
                return step(box, [r[7], ...r.slice(0, 7), ...r.slice(8)], 'p2');
            """,
        }
    )
    assert out["up"]["moves"] == 1 and out["up"]["focus"] == "p2" and out["up"]["same"] == 10
    assert out["up"]["keys"] == ["p7", "p0", "p1", "p2", "p3", "p4", "p5", "p6", "p8", "p9"]


@needs_node
def test_two_thousand_persons_one_change_one_move():
    """2000 persone (più le lettere di sezione), una modificata che va in fondo: una mossa, non 2017. E i casi misti
    (righe nuove, righe tolte, ordine rovesciato, testo cambiato) danno sempre l'ordine giusto con le righe di prima."""
    out = run_tree(
        {
            "big": ROWS
            + """
                const r = rows(2000);
                const box = list(r);
                return step(box, [...r.slice(1), r[0]], 'p500');
            """,
            "mixed": ROWS
            + """
                const r = rows(30);
                const box = list(r);
                const next = [['n1', 'Nuova'], ...r.slice(20).reverse(), ['p3', 'Cambiata'], ...r.slice(5, 15),
                  ['n2', 'Altra nuova'], r[0]];
                const result = step(box, next, 'p10');
                return {...result, expected: next.map(([k]) => k), wanted: next.map(([, t]) => t)};
            """,
            "reverse": ROWS
            + """
                const r = rows(8);
                const box = list(r);
                return step(box, [...r].reverse(), '');
            """,
        }
    )
    big = out["big"]
    assert (big["moves"], big["focus"], big["same"], big["keys"][-1], big["keys"][0]) == (1, "p500", 2000, "p0", "p1")
    mixed = out["mixed"]
    assert mixed["keys"] == mixed["expected"] and mixed["texts"] == mixed["wanted"]
    assert mixed["focus"] == "p10" and mixed["same"] == 22
    # Rovesciato: tutte tranne una devono spostarsi, nessuna di più
    assert out["reverse"]["moves"] == 7 and out["reverse"]["keys"] == [f"p{i}" for i in range(7, -1, -1)]


# Chromium vero: il fuoco su una riga che si sposta, o su una riga davanti alla quale se ne sposta un'altra
BROWSER = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent(`<div id="list"></div><script>
    const paintIcons = () => {};
    __DECLARATIONS__
    const row = (k, t) => '<div tabindex="0" data-key="' + k + '">' + t + '</div>';
    window.draw = (order, changed = {}) => morph(document.getElementById("list"),
      order.map((k) => row(k, changed[k] || "Persona " + k)).join(""));
    window.focused = () => document.activeElement.dataset.key || document.activeElement.tagName;
  </script>`);
  const out = {};
  const ids = Array.from({length: 2000}, (_, i) => "p" + i);
  await page.evaluate((ids) => window.draw(ids), ids);
  // Meno Recente: la quarta riga, modificata, scende in fondo; il fuoco è sull'ottava
  await page.focus('[data-key="p7"]');
  out.down = await page.evaluate((ids) => {
    const moved = [...ids.slice(0, 3), ...ids.slice(4), ids[3]];
    let records = 0;
    const watch = new MutationObserver((m) => { records += m.length; });
    watch.observe(document.getElementById("list"), {childList: true});
    window.draw(moved, {p3: "Persona p3 modificata"});
    records += watch.takeRecords().length;
    watch.disconnect();
    return {focus: window.focused(), records, last: document.getElementById("list").lastElementChild.dataset.key};
  }, ids);
  // Più Recente: la riga con il fuoco, modificata, sale in cima
  await page.evaluate((ids) => window.draw(ids), ids);
  await page.focus('[data-key="p900"]');
  out.up = await page.evaluate((ids) => {
    window.draw(["p900", ...ids.filter((k) => k !== "p900")], {p900: "Persona p900 modificata"});
    return {focus: window.focused(), first: document.getElementById("list").firstElementChild.dataset.key};
  }, ids);
  // Invio sulla riga con il fuoco: è ancora lei
  await page.evaluate(() => document.addEventListener("keydown", (e) => {
    if (e.key === "Enter") window.opened = document.activeElement.dataset.key;
  }));
  await page.keyboard.press("Enter");
  out.enter = await page.evaluate(() => window.opened || "");
  console.log(JSON.stringify(out));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _has_playwright() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True, timeout=30)
    return probe.returncode == 0


@pytest.mark.skipif(not _has_playwright(), reason="serve Playwright per node")
def test_the_focus_stays_on_the_chosen_row_in_a_real_browser():
    script = BROWSER.replace("__DECLARATIONS__", _declarations(NAMES))
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=120)
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    # Una riga spostata: due record (tolta e rimessa) e il fuoco resta sull'ottava riga
    assert out["down"] == {"focus": "p7", "records": 2, "last": "p3"}
    assert out["up"] == {"focus": "p900", "first": "p900"}
    assert out["enter"] == "p900"
