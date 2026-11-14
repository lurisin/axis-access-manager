"""Colonne dell'elenco Persone scelte e ordinate da chi guarda (0.28.4): il modello delle colonne di index.html, preso
dalla pagina com'è e fatto girare con node come in test_person_list. Predefinite, scelte non valide pulite come fa il
server, colonne in più per l'ordine e la ricerca, quali spariscono con poco spazio, dati sotto il nome sul telefono e
celle della tabella. Le persone sono di fantasia."""

import json
import re

import pytest
from test_person_list import CATEGORIES, DATES, SORT, TABLE, person, run, table_attrs

from app.preferences import (
    PERSON_COLUMNS,
    PERSON_COLUMNS_DEFAULT,
    PERSON_COLUMNS_REQUIRED,
    clean_person_columns,
)

DEFAULT = list(PERSON_COLUMNS_DEFAULT)


def test_the_page_knows_the_same_columns_as_the_server():
    out = run(
        {
            "keys": "PERSON_COLUMN_KEYS",
            "default": "PERSON_COLUMNS_DEFAULT",
            "required": "PERSON_COLUMNS_REQUIRED",
            "labels": "PERSON_COLUMNS.map((c) => c.label)",
            "sortable": "PERSON_COLUMNS.filter((c) => c.sort).map((c) => c.key)",
            "sorts": "PERSON_SORTS.map(([value]) => value)",
            "classes": "PERSON_COLUMNS.map((c) => c.cls)",
        }
    )
    assert out["keys"] == list(PERSON_COLUMNS)
    assert out["default"] == DEFAULT and out["required"] == list(PERSON_COLUMNS_REQUIRED)
    assert out["labels"] == [
        "Foto",
        "Cognome",
        "Nome",
        "Matricola",
        "Ditta",
        "Tipologia",
        "Tessere",
        "Telefono",
        "Email",
        "Referente Interno",
        "Note",
        "Ultimo Accesso",
        "Data di Inserimento",
        "Ultima Modifica",
        "Stato",
    ]
    # Ogni colonna che ordina c'è nella tendina nelle due direzioni, e la tendina non ha chiavi senza colonna; Note no
    keys = {value.split(":")[0] for value in out["sorts"]}
    assert keys == set(out["sortable"])
    assert all(f"{key}:asc" in out["sorts"] and f"{key}:desc" in out["sorts"] for key in keys)
    assert not {"foto", "tessere", "note", "stato"} & keys
    assert len(set(out["classes"])) == len(out["classes"])


CHOICES = [
    None,
    "cognome",
    {"cognome": 1},
    [],
    DEFAULT,
    ["foto", "stato"],
    ["stato", "foto"],
    ["ditta", "nome"],
    ["cognome", "ditta"],
    ["nome", "cognome", "NOME", "nome", 7, None],
    ["foto", "reparto", "nome", "ditta", "ditta", "stato"],
    list(reversed(PERSON_COLUMNS)),
]


def test_saved_choices_are_cleaned_like_the_server_does():
    """Una scelta rovinata o di una versione futura si pulisce senza errori, come in preferences.py: colonne sconosciute
    e doppioni tolti, Cognome e Nome rimessi; senza una lista le predefinite."""
    out = run({str(i): f"cleanPersonColumns({json.dumps(choice)})" for i, choice in enumerate(CHOICES)})
    for i, choice in enumerate(CHOICES):
        assert out[str(i)] == (clean_person_columns(choice) or DEFAULT), choice
    assert out["0"] == DEFAULT
    assert out["10"] == ["foto", "cognome", "nome", "ditta", "stato"]


def test_the_choice_belongs_to_whoever_loaded_it():
    """Le colonne lette per un utente non valgono per un altro nello stesso browser; finché non arrivano, le
    predefinite."""
    chosen = ["nome", "cognome", "telefono"]
    out = run(
        {
            "nothing": "personColumnsChosen()",
            "mine": f"(store.me.username = 'maria', Object.assign(personPrefs, {{user: 'maria', "
            f"columns: {json.dumps(chosen)}}}), personColumnsChosen())",
            "other": "(store.me.username = 'luca', personColumnsChosen())",
            "default": "(store.me.username = 'maria', personPrefs.columns = null, personColumnsChosen())",
        }
    )
    assert out == {"nothing": DEFAULT, "mine": chosen, "other": DEFAULT, "default": DEFAULT}


def test_the_sort_column_and_the_cards_join_the_table_where_they_belong():
    out = run(
        {
            "default": f"personColumnsFor({json.dumps(DEFAULT)}, 'cognome', false)",
            "created": f"personColumnsFor({json.dumps(DEFAULT)}, 'inserimento', false)",
            "phone": f"personColumnsFor({json.dumps(DEFAULT)}, 'telefono', true)",
            "cards": "personColumnsFor(['foto', 'cognome', 'nome', 'stato'], 'cognome', true)",
            "both": "personColumnsFor(['stato', 'nome', 'cognome'], 'matricola', true)",
            "front": "personColumnsFor(['nome', 'cognome'], 'tessere', false)",
            "unknown": "personColumnsFor(['nome', 'cognome'], 'colore', false)",
            "chosen": "personColumnsFor(['cognome', 'nome', 'modifica', 'tessere'], 'modifica', true)",
        }
    )
    assert out["default"] == DEFAULT
    # Con le predefinite la data dell'ordine va prima di Stato, come nella 0.28.3
    assert out["created"] == ["foto", "cognome", "nome", "matricola", "ditta", "tipologia", "tessere", "accesso",
                              "inserimento", "stato"]  # fmt: skip
    assert out["phone"] == ["foto", "cognome", "nome", "matricola", "ditta", "tipologia", "tessere", "telefono",
                            "accesso", "stato"]  # fmt: skip
    assert out["cards"] == ["foto", "cognome", "nome", "tessere", "stato"]
    assert out["both"] == ["stato", "nome", "cognome", "matricola", "tessere"]
    # Tessere non ordina, e una chiave sconosciuta non aggiunge niente
    assert out["front"] == out["unknown"] == ["nome", "cognome"]
    # Già scelte: restano dove le ha messe chi guarda
    assert out["chosen"] == ["cognome", "nome", "modifica", "tessere"]


def fit(columns, width, keep=("cognome", "nome", "cognome")) -> str:
    return f"personColumnsFit({json.dumps(columns)}, {width}, {json.dumps(list(keep))})"


def test_with_little_room_the_last_columns_of_the_order_go_first():
    """Le predefinite stanno tutte in 980 px (1280 col menu aperto, anche con la barra di scorrimento di Windows); poi
    spariscono dalla fine: Stato, Ultimo Accesso, Tessere, Tipologia, Ditta, Matricola, la Foto. Cognome e Nome mai;
    la colonna dell'ordine e Tessere con la ricerca che trova delle tessere solo se non c'è altro da togliere."""
    every = list(PERSON_COLUMNS)
    out = run(
        {
            "need": f"personColumnsNeed({json.dumps(DEFAULT)})",
            "wide": fit(DEFAULT, 2000),
            "980": fit(DEFAULT, 980),
            "979": fit(DEFAULT, 979),
            "700": fit(DEFAULT, 700),
            "600": fit(DEFAULT, 600),
            "300": fit(DEFAULT, 300),
            "100": fit(DEFAULT, 100),
            "by_access": fit(DEFAULT, 700, ["cognome", "nome", "accesso"]),
            "by_state_cards": fit(DEFAULT, 600, ["cognome", "nome", "cognome", "tessere"]),
            "kept_last": fit(DEFAULT, 260, ["cognome", "nome", "accesso", "tessere"]),
            "reordered": fit(["stato", "cognome", "foto", "nome", "ditta"], 400),
            "every": fit(every, 1000),
        }
    )
    assert out["need"] == 980
    assert out["wide"] == DEFAULT and out["980"] == DEFAULT
    assert out["979"] == DEFAULT[:-1]
    assert out["700"] == ["foto", "cognome", "nome", "matricola", "ditta", "tipologia", "tessere"]
    assert out["600"] == ["foto", "cognome", "nome", "matricola", "ditta", "tipologia"]
    assert out["300"] == ["foto", "cognome", "nome"]
    # Anche oltre il possibile Cognome e Nome restano (la foto no)
    assert out["100"] == ["cognome", "nome"]
    # La colonna dell'ordine resta: va via prima quello che c'è prima di lei
    assert out["by_access"] == ["foto", "cognome", "nome", "matricola", "ditta", "tipologia", "accesso"]
    assert out["by_state_cards"] == ["foto", "cognome", "nome", "matricola", "ditta", "tessere"]
    # Senza altro da togliere vanno anche quelle, dalla fine
    assert out["kept_last"] == ["cognome", "nome"]
    # Con un altro ordine va via prima la Foto, che viene dopo Stato
    assert out["reordered"] == ["stato", "cognome", "nome"]
    # Con tutte le colonne in 1000 px restano le prime dell'ordine
    shown = out["every"]
    assert shown == every[: len(shown)] and 6 <= len(shown) < len(every)


# Pagina finta per personListWidth: finestra, pagina ed elenco con le larghezze di prova, e il riquadro di prova della
# barra di scorrimento larga BAR (quante volte si misura in measured)
WINDOW = """
let BAR = 17, measured = 0;
const window = {innerWidth: 1280, devicePixelRatio: 1};
const document = {
  documentElement: {clientWidth: 1263},
  body: {appendChild() { measured++; }},
  createElement: () => ({style: {}, offsetWidth: 100, get clientWidth() { return 100 - BAR; }, remove() {}}),
};
const list = {clientWidth: 981};
const at = (inner, page, width) => { window.innerWidth = inner; document.documentElement.clientWidth = page;
  list.clientWidth = width; return personListWidth(list); };
"""


def test_the_list_is_measured_as_with_the_scrollbar_always_there():
    """Verifica della 0.28.4: la larghezza per le colonne non dipende dalla barra di scorrimento verticale, che compare
    e sparisce con l'altezza dell'elenco. Con la barra di Windows (17 px) a 1280 col menu aperto l'elenco è di 981 px
    con la barra e di 998 senza: tutte e due le volte conta 981. Con le barre sovrapposte del telefono e del Mac (0 px)
    conta l'elenco com'è. La barra si misura una volta per ogni zoom, e la larghezza non supera mai quella vera."""
    out = run(
        {
            "windows": "[at(1280, 1263, 981), at(1280, 1280, 998), at(1280, 1263, 981)]",
            "measured": "measured",
            "overlay": "(BAR = 0, window.devicePixelRatio = 2, [at(1280, 1280, 998), at(1280, 1280, 998), measured])",
            # Zoom al 125%: la barra si misura di nuovo, in px della pagina
            "zoom": "(BAR = 14, window.devicePixelRatio = 1.25, [at(1024, 1010, 803), at(1024, 1024, 817), measured])",
            # Una barra della pagina più larga di quella del riquadro di prova, o lo zoom col dito: mai più dell'elenco
            "wider": "at(1024, 1004, 797)",
            "pinch": "at(600, 1024, 817)",
        },
        WINDOW,
    )
    assert out["windows"] == [981, 981, 981] and out["measured"] == 1
    assert out["overlay"] == [998, 998, 2]
    assert out["zoom"] == [803, 803, 3]
    assert out["wider"] == 797 and out["pinch"] == 803


def test_same_window_width_same_columns_whatever_happened_before():
    """Verifica della 0.28.4: a 1280 col menu aperto le predefinite hanno 998 px (senza barra) e ne servono 980;
    stringendo di poco la finestra (976 px a 1258) Stato andava via e riallargandola non tornava più fino al
    ricaricamento, perché la colonna tolta tornava solo con 24 px in più. Ora le colonne dipendono solo dalla
    larghezza: qualunque strada, alla stessa larghezza le stesse colonne, con la barra di Windows e senza."""
    keep = '["cognome", "nome", "cognome"]'
    shown = f"(w) => personColumnsFit({json.dumps(DEFAULT)}, w, {keep}).length"
    windows = f"(inner) => personColumnsFit({json.dumps(DEFAULT)}, at(inner, inner - 17, inner - 299), {keep})"
    out = run(
        {
            "overlay": f"[998, 993, 980, 979, 976, 980, 993, 998, 818, 998].map({shown})",
            "windows": f"[1280, 1258, 1280, 1100, 1280].map({windows}).map((s) => s.length)",
            "bar": f"[1280, 1275, 1263].map((inner) => [({windows})(inner), (BAR = 17, "
            f"personColumnsFit({json.dumps(DEFAULT)}, at(inner, inner, inner - 282), {keep}))])",
        },
        WINDOW,
    )
    assert out["overlay"] == [9, 9, 9, 8, 8, 9, 9, 9, out["overlay"][8], 9] and out["overlay"][8] < 8
    assert out["windows"] == [9, 8, 9, out["windows"][3], 9] and out["windows"][3] < 8
    # La barra che va e viene (998 px senza, 981 con, a 1280) non cambia le colonne
    for with_bar, without in out["bar"]:
        assert with_bar == without
    assert [len(with_bar) for with_bar, _ in out["bar"]] == [9, 8, 8]


PEOPLE = [
    person(
        1,
        "Mario",
        "Rossi",
        employee_id="0042",
        company="Teatro",
        phone="+39 06 5550 1234",
        email="mario.rossi+sala@esempio.it",
        referent="Direzione di Sala",
        notes="Chiavi del magazzino\n  scenografie <B>",
        category_id=1,
        cards=[{"id": 1, "number": "04ab12cd", "printed_code": "Ciccio"}, {"id": 2, "number": "04ff0011"}],
        last_access="2026-11-12T10:00:00+00:00",
        created_at="2026-01-10T09:00:00+00:00",
        updated_at="2026-11-13T11:55:00+00:00",
    ),
    person(2, "Anna", "Bianchi", category_id=2),
    person(3, "Zeno", "Abate"),
]
PRELUDE = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(PEOPLE)};"


@pytest.mark.parametrize(
    ("chosen", "sort", "q", "expected"),
    [
        # Predefinite e ordine per cognome: i primi due dati che ci sono, nell'ordine delle colonne
        (DEFAULT, "cognome", "", ["Matricola 0042 · Teatro", "Ospite", ""]),
        (
            ["telefono", "cognome", "nome", "email", "note"],
            "nome",
            "",
            ["+39 06 5550 1234 · mario.rossi+sala@esempio.it", "", ""],
        ),  # fmt: skip
        # Foto, Cognome, Nome e Stato la riga li ha già; le note su una riga
        (
            ["foto", "stato", "cognome", "nome", "note", "referente"],
            "cognome",
            "",
            ["Chiavi del magazzino scenografie <B> · Referente Direzione di Sala", "", ""],
        ),  # fmt: skip
        (
            ["cognome", "nome", "tessere", "accesso"],
            "cognome",
            "",
            ["Ciccio, 04ff0011 · Ultimo accesso ieri 11:00", "", ""],
        ),  # fmt: skip
        # Le tessere trovate dalla ricerca sono già sotto il nome
        (
            ["cognome", "nome", "tessere", "inserimento"],
            "cognome",
            "cicc",
            ["Inserita il 10/01/2026", *["Inserita il 01/10/2026"] * 2],
        ),
        # Con un altro ordine il valore usato per ordinare, come prima
        (["cognome", "nome", "telefono"], "matricola", "", ["Matricola 0042", "Senza matricola", "Senza matricola"]),
        (DEFAULT, "telefono", "", ["+39 06 5550 1234", "Senza telefono", "Senza telefono"]),
        (DEFAULT, "referente", "", ["Referente Direzione di Sala", "Senza referente", "Senza referente"]),
        (DEFAULT, "email", "", ["mario.rossi+sala@esempio.it", "Senza email", "Senza email"]),
    ],
)
def test_phone_rows_show_the_first_chosen_data_under_the_name(chosen, sort, q, expected):
    calls = {
        str(i): f"personRowNote(persons[{i}], {json.dumps(sort)}, {json.dumps(chosen)}, "
        f"searchedCards(persons[{i}], {json.dumps(q)}).length > 0)"
        for i in range(3)
    }
    out = run(calls, PRELUDE, names=DATES + SORT + TABLE)
    assert [out[str(i)] for i in range(3)] == expected


def test_table_with_the_chosen_columns_in_their_order():
    chosen = ["nome", "cognome", "telefono", "email", "note", "modifica", "inserimento", "referente", "stato"]
    table = (
        "(sort, q = '', width) => { personFilters.sort = sort; personFilters.q = q; "
        "store.me.username = 'maria'; Object.assign(personPrefs, {user: 'maria', columns: "
        f"{json.dumps(chosen)}}}); "
        "return personTable(sortPersons(persons.filter((p) => personMatches(p, q)), sort), width); }"
    )
    many = [*PEOPLE, *(person(10 + i, f"Nome{i:02d}", ["Abete", "Betulla", "Cedro"][i % 3]) for i in range(12))]
    out = run(
        {
            "html": f"({table})('nome:asc')",
            "narrow": f"({table})('cognome:asc', '', 600)",
            "cards": f"({table})('telefono:desc', '04ab', 600)",
            "letters": f"(persons.push(...{json.dumps(many[3:])}), ({table})('nome:asc'))",
        },
        PRELUDE,
        names=DATES + SORT + TABLE,
    )
    html = out["html"]
    assert table_attrs(html)["data-columns"] == " ".join(chosen)
    assert table_attrs(html)["data-hide"] == ""
    assert (table_attrs(html)["data-first"], table_attrs(html)["data-last"]) == ("nome", "stato")
    heads = re.findall(r'<th class="(c-[a-z]+)"', html)
    assert heads == ["c-first", "c-last", "c-phone", "c-email", "c-notes", "c-updated", "c-created", "c-ref", "c-state"]
    # Intestazioni che ordinano: tutte tranne Note e Stato
    assert re.findall(r'data-sort="(\w+)"', html) == ["nome", "cognome", "telefono", "email", "modifica",
                                                      "inserimento", "referente"]  # fmt: skip
    assert 'title="Ordina per Referente Interno"' in html
    row = re.search(r'<tr data-person="1".*?</tr>', html, re.S).group(0)
    cells = re.findall(r"<td class=\"(c-[a-z]+)\"", row)
    assert cells == [c.replace("c-", "c-") for c in heads]
    # Telefono ed email come nella scheda, per chiamare e scrivere
    assert '<td class="c-phone" title="+39 06 5550 1234"><a href="tel:+390655501234">+39 06 5550 1234</a></td>' in row
    assert (
        '<td class="c-email" title="mario.rossi+sala@esempio.it"><a href="mailto:mario.rossi%2Bsala@esempio.it">'
        "mario.rossi+sala@esempio.it</a></td>"
    ) in row
    # Note su una riga, il testo intero nel title, senza diventare HTML
    assert (
        '<td class="c-notes" title="Chiavi del magazzino\n  scenografie &lt;B&gt;">'
        "Chiavi del magazzino scenografie &lt;B&gt;</td>"
    ) in row
    # Date come l'Ultimo Accesso, con l'ora completa nel title
    assert '<td class="c-updated" title="13 nov 2026, 12:55:00">5 min fa</td>' in row
    assert '<td class="c-created" title="10 gen 2026, 10:00:00">10/01/2026 10:00</td>' in row
    assert '<td class="c-ref" title="Direzione di Sala">Direzione di Sala</td>' in row
    empty = re.search(r'<tr data-person="3".*?</tr>', html, re.S).group(0)
    assert '<td class="c-phone"><span class="muted">—</span></td>' in empty
    assert '<td class="c-notes"><span class="muted">—</span></td>' in empty
    # 600 px: restano le prime dell'ordine, mai Cognome e Nome
    narrow = table_attrs(out["narrow"])
    assert narrow["data-hide"] == "modifica inserimento referente stato"
    assert (narrow["data-first"], narrow["data-last"]) == ("nome", "note")
    # Ordine per telefono e ricerca che trova una tessera: Tessere in più (dopo Cognome, l'ultima scelta che nell'elenco
    # completo viene prima di lei), e restano tutte e due
    cards = table_attrs(out["cards"])
    assert cards["data-columns"] == "nome cognome tessere telefono email note modifica inserimento referente stato"
    assert cards["data-hide"] == "note modifica inserimento referente stato"
    assert "data-cards" in cards
    # Lettere di sezione nella colonna dell'ordine, che c'è sempre. Intestazioni e celle delle sezioni con la chiave
    # della colonna: il morph le abbina per colonna, non per posizione
    section = re.search(r'<tr class="sec" data-key="hA">(.*?)</tr>', out["letters"]).group(1)
    assert section.startswith('<td class="c-first" data-key="nome">A</td><td class="c-last" data-key="cognome"></td>')
    assert re.findall(r'<th class="c-[a-z]+" scope="col" data-key="th-([a-z]+)"', out["letters"])[:3] == [
        "nome",
        "cognome",
        "telefono",
    ]


def test_rules_of_the_columns_come_from_the_model():
    out = run({"css": "personColumnCss()"})
    css = out["css"]
    assert ".p-table .c-photo { width: 64px; }" in css and ".p-table .c-state { width: 168px; }" in css
    assert ".p-table .c-last { width" not in css
    assert '.p-table[data-hide~="telefono"] .c-phone { display: none; }' in css
    # Bordo più largo per la prima e l'ultima in vista; le fisse prendono i suoi 8 px, tranne Foto e Stato
    assert 'table.table.p-table[data-last="tessere"] .c-cards { padding-right: 18px; width: 132px; }' in css
    assert 'table.table.p-table[data-first="foto"] .c-photo { padding-left: 18px; }' in css
    assert 'table.table.p-table[data-last="stato"] .c-state { padding-right: 18px; }' in css
    assert 'table.table.p-table[data-first="cognome"] .c-last { padding-left: 18px; }' in css
