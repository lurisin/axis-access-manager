"""Riquadro «Attendere…» delle modifiche (0.28.3, parte E): le funzioni busy di index.html, prese dalla pagina com'è e
fatte girare con node con una pagina finta e un orologio finto, come le date in test_page_time_zone. Il riquadro compare
solo dopo mezzo secondo, dopo 5 secondi dice che il controller risponde lentamente, si chiude sempre (anche dopo un
errore) prima dell'avviso, e con più operazioni resta aperto finché finiscono tutte."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "axis_access_manager" / "app" / "static" / "index.html"
NAMES = (
    "BUSY_DELAY",
    "BUSY_SLOW",
    "busyState",
    "focusLeft",
    "noteFocusLeft",
    "focusOrigin",
    "busy",
    "busyPaint",
    "busyDone",
    "busyRefocus",
    "busyKeepFocus",
    "sameElement",
    "busyHold",
    "busyBlock",
    "run",
    "runPerson",
)
LABEL = "Eliminazione della tipologia sul controller"
SLOW = "Il controller risponde lentamente: l'operazione continua"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="serve node")


def _declarations(names) -> str:
    """Le dichiarazioni names della pagina com'è: funzioni, anche async (fino alla graffa in colonna zero), e costanti
    di una riga."""
    page = PAGE.read_text(encoding="utf-8")
    found = []
    for name in names:
        block = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", page, re.M | re.S) or re.search(
            rf"^(?:const|let) {name} = .*$", page, re.M
        )
        assert block, name
        found.append(block.group(0))
    return "\n".join(found)


# Pagina finta: il riquadro (#busy) con le sue due righe, il contenitore principale, la vista; un orologio finto che
# avanza solo con advance(ms); toast che annota l'avviso e se in quel momento il riquadro era aperto
PRELUDE = r"""
let log = [];
const node = (id) => ({id, hidden: true, textContent: "", disabled: false, isConnected: true, attrs: {},
  setAttribute(k, v) { this.attrs[k] = String(v); }, removeAttribute(k) { delete this.attrs[k]; },
  hasAttribute(k) { return k in this.attrs; }, matches(sel) { return sel === ":disabled" && this.disabled; },
  set tabIndex(v) { this.attrs.tabindex = String(v); },
  focus() { if (this.isConnected && !this.disabled) document.activeElement = this; }});
const document = {body: {dataset: {}}, activeElement: null, addEventListener() {}, removeEventListener() {}};
document.activeElement = document.body;
const dlg = Object.assign(node("busy"), {open: false,
  showModal() { this.open = true; log.push("apre"); document.activeElement = this; },
  // Come il browser quando l'elemento di prima non si può più mettere a fuoco: il fuoco resta sulla pagina
  close() { this.open = false; log.push("chiude"); document.activeElement = document.body; }});
const nodes = {"#busy": dlg, "#busy-what": node("busy-what"), "#busy-slow": node("busy-slow"),
  ".main": node("main"), "#view": node("view")};
const $ = (sel) => nodes[sel] || null;
// $$: gli elementi della pagina ridisegnata (vedi busyKeepFocus); MutationObserver scatta a mano con mutate()
let $$ = () => [];
let watchers = [];
class MutationObserver {
  constructor(fn) { this.fn = fn; }
  observe() { watchers.push(this); }
  disconnect() { watchers = watchers.filter((w) => w !== this); }
}
const mutate = () => watchers.forEach((w) => w.fn());
const CSS = {escape: (text) => text};
const toast = (message, error = false) =>
  log.push(`${error ? "errore" : "avviso"}: ${message} (riquadro ${dlg.open ? "aperto" : "chiuso"})`);
let clock = 0, timers = [];
globalThis.setTimeout = (fn, ms = 0) => { const t = {at: clock + ms, fn}; timers.push(t); return t; };
globalThis.clearTimeout = (t) => { timers = timers.filter((x) => x !== t); };
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function advance(ms) {
  const end = clock + ms;
  for (;;) {
    await flush();
    timers.sort((a, b) => a.at - b.at);
    if (!timers.length || timers[0].at > end) break;
    const t = timers.shift();
    clock = t.at;
    t.fn();
  }
  clock = end;
  await flush();
}
// Un'operazione che finisce quando lo dice la prova
function later() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
// Com'è il riquadro adesso
const box = () => ({open: dlg.open, what: nodes["#busy-what"].hidden ? "" : nodes["#busy-what"].textContent,
  slow: !nodes["#busy-slow"].hidden, ariaBusy: nodes[".main"].attrs["aria-busy"] || ""});
// Fatti di busy prima di ogni scenario: niente orologio, avvisi e fuoco degli scenari prima
function reset() {
  log = []; timers = []; clock = 0; $$ = () => [];
  document.activeElement = document.body;
  busyState.keep?.();
}
// Evento finto per busyBlock: fermato se chiama preventDefault
const event = (type, extra = {}) => ({type, key: "", ctrlKey: false, metaKey: false, altKey: false, ...extra,
  target: {closest: (sel) => (extra.retry && sel === "#retry" ? {} : null)},
  preventDefault() { this.stopped = true; }, stopImmediatePropagation() { this.stopped = true; }});
const blocked = (type, extra) => { const e = event(type, extra); busyBlock(e); return Boolean(e.stopped); };
"""


def run_busy(scenarios: dict[str, str]) -> dict:
    """Fa girare ogni scenario (corpo di una funzione async che restituisce le osservazioni) uno dopo l'altro."""
    lines = [PRELUDE, _declarations(NAMES), "const out = {};", "(async () => {"]
    for key, body in scenarios.items():
        name = json.dumps(key)
        lines.append(
            f"try {{ reset(); out[{name}] = await (async () => {{ {body} }})(); }} "
            f"catch (e) {{ out[{name}] = 'ERRORE ' + (e.stack || e); }}"
        )
    lines += ["console.log(JSON.stringify(out));", "})();"]
    result = subprocess.run(["node", "-e", "\n".join(lines)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_a_quick_change_never_shows_the_box():
    """Sotto il mezzo secondo il riquadro non compare mai (niente lampi); la pagina è comunque ferma e occupata."""
    out = run_busy(
        {
            "quick": f"""
                const d = later();
                const done = busy({json.dumps(LABEL)}, () => d.promise);
                await advance(499);
                const during = box();
                d.resolve("fatto");
                const result = await done;
                await advance(10000);
                const ops = busyState.ops.length, own = busyState.timers.length;
                return {{during, result, after: box(), log, ops, timers: own}};
            """
        }
    )
    assert out["quick"] == {
        "during": {"open": False, "what": LABEL, "slow": False, "ariaBusy": "true"},
        "result": "fatto",
        "after": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "log": [],
        "ops": 0,
        "timers": 0,
    }


@needs_node
def test_after_half_a_second_the_box_and_after_five_the_slow_controller():
    """Dopo 500 ms il riquadro con cosa sta facendo; dopo 5 secondi la riga del controller lento; poi resta aperto
    quanto serve (nessun tempo massimo suo) e si chiude alla fine."""
    out = run_busy(
        {
            "slow": f"""
                const d = later();
                const done = busy({json.dumps(LABEL)}, () => d.promise);
                const seen = {{}};
                await advance(499); seen.at499 = box();
                await advance(1); seen.at500 = box();
                await advance(4499); seen.at4999 = box();
                await advance(1); seen.at5000 = box();
                await advance(120000); seen.at125s = box();
                d.resolve(true);
                await done;
                seen.end = box();
                seen.log = log;
                return seen;
            """
        }
    )
    shown = {"open": True, "what": LABEL, "slow": False, "ariaBusy": "true"}
    assert out["slow"] == {
        "at499": {**shown, "open": False},
        "at500": shown,
        "at4999": shown,
        "at5000": {**shown, "slow": True},
        "at125s": {**shown, "slow": True},
        "end": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "log": ["apre", "chiude"],
    }
    # Il testo della riga lenta è quello promesso
    assert SLOW in PAGE.read_text(encoding="utf-8")


@needs_node
def test_the_box_closes_before_the_usual_message_also_after_an_error():
    """run: il riquadro si chiude (finally) e poi arriva l'avviso di sempre, di conferma o di errore; anche con
    un'eccezione subito, prima di ogni attesa."""
    out = run_busy(
        {
            "error": f"""
                const d = later();
                const done = run(() => d.promise, "Tipologia eliminata", {json.dumps(LABEL)});
                await advance(700);
                d.reject(new Error("il controller non risponde"));
                const result = await done;
                const ops = busyState.ops.length, own = busyState.timers.length;
                return {{result: result ?? null, log, box: box(), ops, timers: own}};
            """,
            "ok": f"""
                const d = later();
                const done = run(() => d.promise, "Tipologia eliminata", {json.dumps(LABEL)});
                await advance(3000);
                d.resolve({{ok: true}});
                return {{result: await done, log}};
            """,
            "thrown": """
                let caught = "";
                const fail = () => { throw new Error("subito"); };
                try { await busy("Salvataggio della persona", fail); } catch (e) { caught = e.message; }
                await advance(1000);
                return {caught, log, box: box(), ops: busyState.ops.length};
            """,
            "person": """
                const d = later();
                const done = runPerson(() => d.promise, "Persona eliminata", "Eliminazione della persona");
                await advance(600);
                const during = box().what;
                d.reject(Object.assign(new Error("cambiata"), {status: 409}));
                return {during, result: await done, log};
            """,
        }
    )
    assert out["error"] == {
        "result": None,
        "log": ["apre", "chiude", "errore: il controller non risponde (riquadro chiuso)"],
        "box": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "ops": 0,
        "timers": 0,
    }
    confirmed = ["apre", "chiude", "avviso: Tipologia eliminata (riquadro chiuso)"]
    assert out["ok"] == {"result": {"ok": True}, "log": confirmed}
    assert out["thrown"] == {
        "caught": "subito",
        "log": [],
        "box": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "ops": 0,
    }
    assert out["person"] == {
        "during": "Eliminazione della persona",
        "result": {"stale": "cambiata"},
        "log": ["apre", "chiude"],
    }


@needs_node
def test_nested_and_concurrent_operations_keep_one_box_with_the_latest_text():
    """Un contatore: il riquadro resta finché finiscono tutte, senza chiudersi e riaprirsi in mezzo, e mostra il testo
    dell'ultima avviata (un'operazione senza testo lascia quello di prima); finita l'ultima, torna quello di prima."""
    out = run_busy(
        {
            "nested": f"""
                const outer = later(), inner = later(), quiet = later();
                const seen = {{}};
                const done = busy("Salvataggio della telecamera", async () => {{
                    await outer.promise;
                    await run(() => inner.promise, "", {json.dumps(LABEL)});
                    await busy("", () => quiet.promise);
                    return "fine";
                }});
                await advance(600); seen.outer = box().what;
                outer.resolve();
                await advance(100); seen.inner = box().what;
                inner.resolve();
                await advance(100); seen.quiet = box();
                quiet.resolve();
                seen.result = await done;
                seen.end = box();
                seen.log = log;
                return seen;
            """,
            "together": """
                const a = later(), b = later();
                const first = busy("Scrittura della tessera sul controller", () => a.promise);
                await advance(200);
                const second = busy("Rilettura dal controller", () => b.promise);
                await advance(400);
                const both = box().what;
                b.resolve();
                await second;
                const left = box();
                await advance(5000);
                const slow = box().slow;
                a.resolve();
                await first;
                return {both, left, slow, end: box().open, log};
            """,
            "plain": """
                const d = later();
                const done = busy("", () => d.promise);
                await advance(600);
                const seen = box();
                d.resolve();
                await done;
                return seen;
            """,
            "skipped": """
                const d = later();
                const done = busy(false, () => d.promise);
                await advance(7000);
                const seen = {box: box(), ops: busyState.ops.length};
                d.resolve(5);
                return {...seen, result: await done, log};
            """,
        }
    )
    assert out["nested"] == {
        "outer": "Salvataggio della telecamera",
        "inner": LABEL,
        "quiet": {"open": True, "what": "Salvataggio della telecamera", "slow": False, "ariaBusy": "true"},
        "result": "fine",
        "end": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "log": ["apre", "chiude"],
    }
    assert out["together"] == {
        "both": "Rilettura dal controller",
        "left": {"open": True, "what": "Scrittura della tessera sul controller", "slow": False, "ariaBusy": "true"},
        "slow": True,
        "end": False,
        "log": ["apre", "chiude"],
    }
    # Senza un testo suo: solo «Attendere…»
    assert out["plain"] == {"open": True, "what": "", "slow": False, "ariaBusy": "true"}
    # false: nessun riquadro e nessun blocco (le letture e le operazioni con un loro segno di avanzamento)
    assert out["skipped"] == {
        "box": {"open": False, "what": "", "slow": False, "ariaBusy": ""},
        "ops": 0,
        "result": 5,
        "log": [],
    }


@needs_node
def test_clicks_and_keys_are_stopped_for_the_whole_operation_except_the_retry_window():
    """Dal primo istante (anche prima del riquadro) clic e tasti non arrivano alla pagina: niente doppio clic né
    seconda modifica; passano la finestra Riprova e i tasti del browser. Finita l'operazione, tutto come prima."""
    out = run_busy(
        {
            "block": """
                const before = blocked("click");
                const d = later();
                const done = busy("Salvataggio della persona", () => d.promise);
                await advance(100);
                const early = {click: blocked("click"), dblclick: blocked("dblclick"),
                    enter: blocked("keydown", {key: "Enter"}), escape: blocked("keydown", {key: "Escape"}),
                    submit: blocked("submit")};
                await advance(5000);
                const late = {click: blocked("click"), retry: blocked("click", {retry: true}),
                    retryKey: blocked("keydown", {key: "Enter", retry: true}), reload: blocked("keydown", {key: "F5"}),
                    copy: blocked("keydown", {key: "c", ctrlKey: true})};
                d.resolve();
                await done;
                return {before, early, late, after: blocked("click")};
            """,
        }
    )
    assert out["block"] == {
        "before": False,
        "early": {"click": True, "dblclick": True, "enter": True, "escape": True, "submit": True},
        "late": {"click": True, "retry": False, "retryKey": False, "reload": False, "copy": False},
        "after": False,
    }


@needs_node
def test_the_retry_window_hides_the_box_until_it_is_answered():
    """La finestra Riprova (controller che non ha risposto) si apre a metà operazione: il riquadro si toglie finché non
    si risponde e poi torna, con lo stesso testo, fino alla fine."""
    out = run_busy(
        {
            "hold": f"""
                const d = later();
                const done = busy({json.dumps(LABEL)}, () => d.promise);
                await advance(800);
                busyHold(true);
                const held = box().open;
                await advance(6000);
                busyHold(false);
                const back = box();
                d.resolve();
                await done;
                return {{held, back, log}};
            """
        }
    )
    assert out["hold"] == {
        "held": False,
        "back": {"open": True, "what": LABEL, "slow": True, "ariaBusy": "true"},
        "log": ["apre", "chiude", "apre", "chiude"],
    }


@needs_node
def test_focus_goes_back_where_it_was_or_to_the_page():
    """Alla chiusura il fuoco torna sull'elemento di prima; se non c'è più va sulla pagina; un pulsante disattivato
    da chi l'ha premuto fino alla fine si riprova appena è di nuovo attivo."""
    out = run_busy(
        {
            "back": """
                const button = node("salva");
                button.focus();
                const d = later();
                const done = busy("Salvataggio della persona", () => d.promise);
                await advance(600);
                const during = document.activeElement.id;
                d.resolve();
                await done;
                return {during, after: document.activeElement.id};
            """,
            "gone": """
                const button = node("elimina");
                button.focus();
                const d = later();
                const done = busy("Eliminazione della tipologia sul controller", () => d.promise);
                await advance(600);
                button.isConnected = false;
                d.resolve();
                await done;
                return {after: document.activeElement.id, tabindex: nodes["#view"].attrs.tabindex};
            """,
            "disabled": """
                const button = node("rileggi");
                button.focus();
                button.disabled = true;
                const d = later();
                const done = busy("Rilettura dal controller", () => d.promise);
                await advance(600);
                d.resolve();
                await done;
                const right = document.activeElement.id || "pagina";
                button.disabled = false;
                await advance(0);
                return {right, after: document.activeElement.id};
            """,
            "quick": """
                const button = node("sospendi");
                button.focus();
                await busy("Sospensione della tessera sul controller", () => Promise.resolve());
                return document.activeElement.id;
            """,
            # Chi ha avviato l'operazione ridisegna la scheda: il fuoco va sul pulsante uguale appena compare, non se
            # intanto è andato altrove, e dopo 10 secondi non si segue più
            "redrawn": """
                const attributes = [{name: "class", value: "small"}, {name: "data-toggle", value: "12"}];
                const old = Object.assign(node("vecchio"), {localName: "button", attributes});
                old.focus();
                const d = later();
                const done = busy("Sospensione della tessera sul controller", () => d.promise);
                await advance(600);
                d.resolve();
                await done;
                const fresh = Object.assign(node("nuovo"), {getClientRects: () => [1], closest: () => null});
                old.isConnected = false;
                document.activeElement = document.body;
                mutate();
                const empty = document.activeElement === document.body;
                $$ = (sel) => sel === 'button[data-toggle="12"]' ? [fresh] : [];
                mutate();
                const found = document.activeElement.id;
                const other = node("altro");
                other.focus();
                mutate();
                const kept = document.activeElement.id;
                await advance(10000);
                return {empty, found, kept, watching: watchers.length};
            """,
        }
    )
    assert out == {
        "back": {"during": "busy", "after": "salva"},
        "gone": {"after": "view", "tabindex": "-1"},
        "disabled": {"right": "pagina", "after": "rileggi"},
        "quick": "sospendi",
        "redrawn": {"empty": True, "found": "nuovo", "kept": "altro", "watching": 0},
    }


@needs_node
def test_focus_comes_back_to_the_sheet_buttons_and_to_a_button_disabled_before_run():
    """Rilievi della verifica: i pulsanti della scheda hanno attributi data- senza valore (data-add-card,
    data-edit-data, data-photo-pick…) e dopo il ridisegno il fuoco finiva sulla pagina; quelli che spariscono (Togli
    Foto) lo passano al pulsante indicato da data-refocus; il pulsante uguale si cerca prima nella finestra aperta. Un
    pulsante disattivato prima di run() (Rileggi dal Controller), che il browser lascia senza fuoco, lo riavuto alla
    fine, anche senza riquadro; un campo lasciato da tempo e non disattivato no."""
    redraw = """
        const d = later();
        const done = busy("Scrittura della tessera sul controller", () => d.promise);
        await advance(600);
        d.resolve();
        await done;
        old.isConnected = false;
        document.activeElement = document.body;
    """
    fresh = "(id) => Object.assign(node(id), {getClientRects: () => [1], closest: () => null})"
    out = run_busy(
        {
            "empty": f"""
                const attributes = [{{name: "type", value: "button"}}, {{name: "data-add-card", value: ""}}];
                const old = Object.assign(node("aggiungi"), {{localName: "button", attributes}});
                old.focus();
                {redraw}
                const make = {fresh};
                const again = make("aggiungi nuovo");
                $$ = (sel) => sel === "button[data-add-card]" ? [again] : [];
                mutate();
                return document.activeElement.id;
            """,
            "refocus": f"""
                const attributes = [{{name: "data-photo-del", value: ""}},
                  {{name: "data-refocus", value: "[data-photo-pick]"}}];
                const old = Object.assign(node("togli foto"), {{localName: "button", attributes,
                  dataset: {{refocus: "[data-photo-pick]"}}}});
                old.focus();
                {redraw}
                const make = {fresh};
                const pick = make("aggiungi foto");
                $$ = (sel) => sel === "[data-photo-pick]" ? [pick] : [];
                mutate();
                return document.activeElement.id;
            """,
            "dialog": f"""
                const attributes = [{{name: "data-close", value: ""}}];
                const old = Object.assign(node("chiudi"), {{localName: "button", attributes}});
                old.focus();
                {redraw}
                const make = {fresh};
                const top = make("scheda"), inside = make("chiudi della scheda"), page = make("chiudi della pagina");
                $$ = (sel, root) => sel === "dialog[open]:not(#busy)" ? [top]
                  : sel === "button[data-close]" ? (root === top ? [inside] : [page, inside]) : [];
                mutate();
                return document.activeElement.id;
            """,
            "disabled": """
                const button = node("rileggi");
                button.focus();
                // Come Chrome: il pulsante disattivato perde il fuoco, che va sulla pagina
                button.disabled = true;
                noteFocusLeft({target: button, relatedTarget: null});
                document.activeElement = document.body;
                const d = later();
                const done = busy("Rilettura dal controller", () => d.promise);
                await advance(1500);
                d.resolve();
                await done;
                button.disabled = false;
                await advance(0);
                return document.activeElement.id || "pagina";
            """,
            "quick": """
                const button = node("usa il fuso");
                button.focus();
                button.disabled = true;
                noteFocusLeft({target: button, relatedTarget: null});
                document.activeElement = document.body;
                const done = busy("Scrittura del fuso orario sul controller", () => Promise.resolve());
                await done;
                button.disabled = false;
                await advance(0);
                return {focus: document.activeElement.id || "pagina", log};
            """,
            "stale": """
                const search = node("cerca");
                noteFocusLeft({target: search, relatedTarget: null});
                document.activeElement = document.body;
                await busy("Salvataggio", () => Promise.resolve());
                await advance(0);
                return document.activeElement.id || "pagina";
            """,
        }
    )
    assert out == {
        "empty": "aggiungi nuovo",
        "refocus": "aggiungi foto",
        "dialog": "chiudi della scheda",
        "disabled": "rileggi",
        "quick": {"focus": "usa il fuso", "log": []},
        "stale": "pagina",
    }


def test_every_change_of_the_page_goes_through_the_box():
    """Ogni POST, PUT, PATCH e DELETE della pagina passa da run, runPerson o busy (o da un'azione di una finestra, che
    usa busy da sola). Fuori resta solo Prova Collegamento, che ha già la sua rotellina accanto al pulsante."""
    page = PAGE.read_text(encoding="utf-8").splitlines()
    outside = []
    for number, line in enumerate(page):
        if not re.search(r'method: (?:[^,{}]*\? )?"(?:POST|PUT|PATCH|DELETE)"', line):
            continue
        # La chiamata può andare a capo dopo run(; dentro un onClick di una finestra è già nel riquadro
        around = "\n".join(page[max(0, number - 2) : number + 1])
        action = "\n".join(page[max(0, number - 8) : number + 1])
        if re.search(r"\b(?:run|runPerson|busy)\(", around) or "const saveProgramming = " in line:
            continue
        if re.search(r'busy: "[^"]+", onClick: async \(dlg\) => \{', action):
            continue
        outside.append(line.strip()[:60])
    assert outside == ['const r = await api("api/controllers/test", {method: "POST",']


def test_the_box_is_never_used_for_reading_pages_and_searches():
    """Il registro (pagina, ricerca, filtri, Carica Altri) passa da run per gli errori, ma senza riquadro; gli
    aggiornamenti automatici non passano da run né da busy."""
    page = PAGE.read_text(encoding="utf-8")
    assert 'const data = await run(() => api("api/events?" + params), "", false);' in page
    automatic = ("loadSummary", "refreshView", "refreshPersons", "refreshUnknown", "refreshLog", "refreshNotifications")
    for name in automatic:
        body = re.search(rf"^async function {name}\(.*?^\}}$", page, re.M | re.S).group(0)
        assert not re.search(r"\b(?:run|runPerson|busy)\(", body), name
    # Il riquadro è nella pagina com'è promesso: testo che si legge ad alta voce, aria-busy, nessun pulsante
    markup = re.search(r'<dialog id="busy".*?</dialog>', page, re.S).group(0)
    assert 'role="status"' in markup and 'aria-live="polite"' in markup and "Attendere…" in markup
    assert "<button" not in markup
    assert '$(".main").setAttribute("aria-busy", "true")' in page
    assert "@media (prefers-reduced-motion: reduce)" in page
