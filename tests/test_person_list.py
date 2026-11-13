"""Elenco Persone della pagina (0.28.3, parte C): ordinamenti, lettere di sezione, ricerca su tutti i campi, scelta
ricordata nel browser e tabella del computer. Le funzioni di index.html, prese dalla pagina com'è e fatte girare con
node come le date in test_page_time_zone. Le persone sono di fantasia."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "axis_access_manager" / "app" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="serve node")

# Dichiarazioni che servono, nell'ordine in cui vanno valutate
DATES = (
    "pad",
    "zoneChecked",
    "plantZone",
    "zoned",
    "partsFormat",
    "zoneParts",
    "plantDay",
    "shiftDay",
    "dateFormats",
    "fmtWith",
    "fmtFull",
    "fmtNumDay",
    "fmtTime",
    "dayTime",
    "relTime",
)
SORT = (
    "PERSON_SORTS",
    "PERSON_SORT_DEFAULT",
    "PERSON_SORT_DATES",
    "savedPersonSort",
    "personFilters",
    "categoryOf",
    "personSortKey",
    "nameCollator",
    "codeCollator",
    "comparePersons",
    "personSortValue",
    "sortPersons",
    "foldText",
    "personLetter",
    "personSortNote",
    "PHONE_MARKS",
    "searchFold",
    "personTexts",
    "personHaystack",
    "personMatches",
    "searchedCards",
)
TABLE = (
    "esc",
    "initials",
    "photoUrl",
    "personAvatar",
    "personAccess",
    "validityState",
    "VALIDITY_PILL",
    "customAccess",
    "personFlag",
    "ICONS",
    "icon",
    "cardTag",
    "cardTitle",
    "personRows",
    "personTable",
)
NOW = "2026-11-13T12:00:00Z"


def _declarations(names) -> str:
    """Le dichiarazioni names della pagina com'è: funzioni (fino alla graffa in colonna zero), costanti di una riga e
    costanti su più righe che finiscono con ]; o }; in colonna zero."""
    page = PAGE.read_text(encoding="utf-8")
    found = []
    for name in names:
        block = (
            re.search(rf"^(?:async )?function {name}\(.*?^\}}$", page, re.M | re.S)
            or re.search(rf"^(?:const|let) {name} = [\[{{]\n.*?^[\]}}];$", page, re.M | re.S)
            or re.search(rf"^(?:const|let) {name} = .*$", page, re.M)
        )
        assert block, name
        found.append(block.group(0))
    return "\n".join(found)


def run(calls: dict[str, str], prelude: str = "", names=DATES + SORT, storage: str = "") -> dict:
    """Valuta le espressioni calls in una pagina finta (fuso Europe/Rome, adesso NOW); storage è il codice di
    localStorage (vuoto: un localStorage che non ha niente)."""
    lines = [
        f"const fixed = Date.parse({json.dumps(NOW)}); Date.now = () => fixed;",
        "const store = {me: {time_zone: 'Europe/Rome'}, categories: [], persons: []};",
        storage or "const localStorage = {getItem: () => null, setItem() {}};",
        prelude,
        _declarations(names),
        "const out = {};",
    ]
    for key, expression in calls.items():
        name = json.dumps(key)
        lines.append(f"try {{ out[{name}] = {expression}; }} catch (e) {{ out[{name}] = 'ERRORE ' + e; }}")
    script = "\n".join([*lines, "console.log(JSON.stringify(out));"])
    result = subprocess.run(
        ["node", "-e", script], env={**os.environ, "TZ": "Europe/Athens"}, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


CATEGORIES = [{"id": 1, "name": "Dipendente", "color": "blue"}, {"id": 2, "name": "Ospite", "color": "purple"}]
OLD = "2026-10-01T08:00:00+00:00"


def person(pid, first, last, **fields) -> dict:
    name = " ".join(x for x in (first, last) if x)
    base = {
        "id": pid,
        "name": name,
        "first_name": first,
        "last_name": last,
        "company": "",
        "phone": "",
        "email": "",
        "employee_id": "",
        "referent": "",
        "notes": "",
        "category_id": None,
        "cards": [],
        "photo": "",
        "last_access": "",
        "created_at": OLD,
        "updated_at": OLD,
    }
    return {**base, **fields}


PERSONS = [
    person(
        1,
        "Mario",
        "Rossi",
        employee_id="0042",
        company="Teatro",
        category_id=1,
        last_access="2026-11-12T10:00:00+00:00",
        created_at="2026-01-10T09:00:00+00:00",
        updated_at="2026-11-13T10:00:00+00:00",
    ),
    person(
        2,
        "Anna",
        "Bianchi",
        employee_id="7",
        category_id=2,
        created_at="2026-03-01T09:00:00+00:00",
        updated_at="2026-11-13T09:59:59.999999+00:00",
    ),
    person(
        3,
        "Zeno",
        "Abate",
        employee_id="a-12",
        company="Biglietteria",
        last_access="2026-11-13T08:00:00Z",
        created_at="2025-12-31T23:00:00Z",
        updated_at="2026-11-01T08:00:00+00:00",
    ),
    person(4, "Ospite", "", company="Pulizie", category_id=1, created_at="2026-06-15T09:00:00+00:00"),
    person(
        5,
        "",
        "Rossi",
        employee_id="100",
        company="teatro",
        category_id=2,
        last_access="2026-11-13T08:00:00+00:00",
        created_at="2026-06-15T09:00:00+00:00",
    ),
    person(6, "Élodie", "Àlvarez", company="Àlfa", category_id=1, created_at="2026-02-01T09:00:00+00:00"),
    person(7, "Luca", "Bianchi", created_at=""),
]
PRELUDE = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(PERSONS)};"

# Ordine atteso per ogni chiave e direzione: i vuoti sempre in fondo, a parità l'ordine per cognome (comparePersons:
# Abate, Àlvarez, Bianchi Anna, Bianchi Luca, Ospite, Rossi, Rossi Mario)
EXPECTED = {
    "cognome:asc": [3, 6, 2, 7, 4, 5, 1],
    "cognome:desc": [5, 1, 4, 2, 7, 6, 3],
    "nome:asc": [2, 6, 7, 1, 4, 5, 3],
    "nome:desc": [3, 5, 4, 1, 7, 6, 2],
    # Numeri in ordine di valore (7 prima di 0042 e di 100), poi le lettere; senza matricola in fondo
    "matricola:asc": [2, 1, 5, 3, 6, 7, 4],
    "matricola:desc": [3, 5, 1, 2, 6, 7, 4],
    # Teatro e teatro sono uguali: Rossi prima di Rossi Mario anche nell'ordine inverso
    "ditta:asc": [6, 3, 4, 5, 1, 2, 7],
    "ditta:desc": [5, 1, 4, 3, 6, 2, 7],
    "tipologia:asc": [6, 4, 1, 2, 5, 3, 7],
    "tipologia:desc": [2, 5, 6, 4, 1, 3, 7],
    "accesso:desc": [3, 5, 1, 6, 2, 7, 4],
    "accesso:asc": [1, 3, 5, 6, 2, 7, 4],
    "inserimento:desc": [4, 5, 2, 6, 1, 3, 7],
    "inserimento:asc": [3, 1, 6, 2, 4, 5, 7],
    "modifica:desc": [1, 2, 3, 6, 7, 4, 5],
    "modifica:asc": [6, 7, 4, 5, 3, 2, 1],
}


def test_every_order_of_the_dropdown_has_empty_values_last():
    calls = {sort: f"sortPersons(persons, {json.dumps(sort)}).map((p) => p.id)" for sort in EXPECTED}
    calls["options"] = "PERSON_SORTS.map(([value]) => value)"
    calls["labels"] = "PERSON_SORTS.map(([, label]) => label)"
    calls["untouched"] = "(sortPersons(persons, 'nome:desc'), persons.map((p) => p.id))"
    out = run(calls, PRELUDE)
    assert out.pop("options") == list(EXPECTED)
    labels = out.pop("labels")
    assert labels[:2] == ["Cognome A→Z", "Cognome Z→A"]
    assert "Data di Inserimento Più Recente" in labels and "Ultima Modifica Meno Recente" in labels
    # L'elenco della pagina non cambia ordine: si ordina una copia
    assert out.pop("untouched") == [1, 2, 3, 4, 5, 6, 7]
    assert out == EXPECTED


def test_order_by_last_name_is_the_old_one():
    """Cognome A→Z è l'ordine di prima (comparePersons), anche con 2000 persone e i confronti preparati una volta."""
    out = run(
        {
            "same": "JSON.stringify(sortPersons(persons, 'cognome:asc')) "
            "=== JSON.stringify([...persons].sort(comparePersons))",
            "big": "(() => { const many = Array.from({length: 2000}, (_, i) => ({...persons[i % 7], id: 100 + i})); "
            "const a = performance.now(); sortPersons(many, 'tipologia:desc'); "
            "return performance.now() - a; })()",
        },
        PRELUDE,
    )
    assert out["same"] is True
    assert out["big"] < 500


def test_section_letters_only_for_last_and_first_name():
    calls = {}
    for key in ("cognome", "nome", "matricola", "ditta", "tipologia", "accesso", "inserimento", "modifica"):
        calls[key] = f"persons.map((p) => personLetter(p, {json.dumps(key)}))"
    out = run(calls, PRELUDE)
    # L'iniziale del campo usato, senza accenti; chi non ha il cognome sta al suo nome, e il contrario
    assert out.pop("cognome") == ["R", "B", "A", "O", "R", "A", "B"]
    assert out.pop("nome") == ["M", "A", "Z", "O", "R", "E", "L"]
    assert all(letters == [""] * 7 for letters in out.values())


def test_line_under_the_name_on_the_phone_says_the_value_used():
    calls = {
        key: f"personSortNote(persons[{index}], {json.dumps(key)})"
        for key, index in (
            ("cognome", 0),
            ("nome", 0),
            ("matricola", 0),
            ("ditta", 0),
            ("tipologia", 0),
            ("accesso", 0),
            ("inserimento", 0),
            ("modifica", 0),
        )
    }
    calls.update(
        {
            "no_emp": "personSortNote(persons[3], 'matricola')",
            "no_company": "personSortNote(persons[1], 'ditta')",
            "no_category": "personSortNote(persons[2], 'tipologia')",
            "no_access": "personSortNote(persons[1], 'accesso')",
            "recent": "personSortNote(persons[2], 'accesso')",
        }
    )
    out = run(calls, PRELUDE)
    assert out == {
        "cognome": "",
        "nome": "",
        "matricola": "Matricola 0042",
        "ditta": "Teatro",
        "tipologia": "Dipendente",
        "accesso": "Ultimo accesso ieri 11:00",
        "inserimento": "Inserita il 10/01/2026",
        # Il giorno dell'impianto (Roma), non quello del browser (Atene)
        "modifica": "Modificata il 13/11/2026",
        "no_emp": "Senza matricola",
        "no_company": "Senza ditta",
        "no_category": "Senza tipologia",
        "no_access": "Nessun accesso registrato",
        "recent": "Ultimo accesso oggi 09:00",
    }


SEARCH_PERSON = person(
    10,
    "Giulia",
    "Neri",
    employee_id="M-0042",
    company="Sartoria",
    referent="Direzione di Sala",
    phone="+39 06 123-4567",
    email="giulia.neri@example.org",
    notes="Chiavi del magazzino\nscenografie",
    cards=[{"number": "04AB12CD"}],
)
OTHER = person(11, "Paolo", "Verdi", company="Neri e Figli", notes="giulia")


def test_search_finds_every_field():
    searches = {
        "name": "giulia neri",
        "first": "GIULIA",
        "last": "neri",
        "reversed": "neri giulia",
        "employee": "m-0042",
        "company": "sartoria",
        "referent": "direzione di",
        "phone": "061234567",
        "phone_spaced": "06 123",
        "email": "neri@example",
        "card": "04ab12",
        "notes": "magazzino scenografie",
        "spaces": "  giulia   neri ",
        "nothing": "violino",
        "across_fields": "neri sartoria",
    }
    calls = {
        key: f"persons.filter((p) => personMatches(p, {json.dumps(text)})).map((p) => p.id)"
        for key, text in searches.items()
    }
    out = run(calls, f"const persons = {json.dumps([SEARCH_PERSON, OTHER])};")
    assert out == {
        "name": [10],
        "first": [10, 11],
        "last": [10, 11],
        "reversed": [10],
        "employee": [10],
        "company": [10],
        "referent": [10],
        "phone": [10],
        "phone_spaced": [10],
        "email": [10],
        "card": [10],
        # Le note su più righe si cercano come una riga sola
        "notes": [10],
        "spaces": [10],
        "nothing": [],
        # La fine di un campo non si unisce all'inizio del successivo
        "across_fields": [],
    }


def test_text_to_search_is_prepared_once_per_person():
    out = run(
        {
            "same": "(() => { const p = persons[0]; "
            "return personHaystack(p) === personHaystack(p) && personTexts.has(p); })()",
            "fresh": "(() => { const copy = {...persons[0], company: 'Altra'}; "
            "return personMatches(copy, 'altra'); })()",
        },
        f"const persons = {json.dumps([SEARCH_PERSON])};",
    )
    assert out == {"same": True, "fresh": True}


# Codici Stampati di fantasia: una persona con due tessere con il codice, una con una tessera senza codice, una con un
# codice che contiene l'altro, una senza tessere
CODED = [
    person(
        20,
        "Marta",
        "Gialli",
        cards=[
            {"id": 1, "number": "04ab12cd", "printed_code": "Ciccio"},
            {"id": 2, "number": "04ff0011", "printed_code": "Ávila  7"},
        ],
    ),
    person(21, "Paolo", "Verdi", cards=[{"id": 3, "number": "04cc0099", "printed_code": ""}]),
    person(22, "Ugo", "Neri", cards=[{"id": 4, "number": "04dd2200", "printed_code": "cICCIObello"}]),
    person(23, "Rita", "Bruni", notes="tessera da fare"),
]
CODED_PRELUDE = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(CODED)};"


def test_search_finds_the_printed_code_of_any_card():
    """«cicc» trova chi ha la tessera «Ciccio», come gli altri campi: parte del testo, maiuscole e accenti indifferenti,
    anche sulla seconda tessera della persona; un codice e il numero accanto non si uniscono."""
    searches = {
        "part": "cicc",
        "upper": "CICCIO",
        "longer": "ciccioB",
        "second_card": "vila",
        "accent": "AVILA 7",
        "spaces": "  ávila   7 ",
        "number": "04CC",
        "both_cards": "04ff",
        "name": "marta",
        "code_and_number": "ciccio 04ff",
        "nothing": "zeta",
    }
    calls = {
        key: f"persons.filter((p) => personMatches(p, {json.dumps(text)})).map((p) => p.id)"
        for key, text in searches.items()
    }
    out = run(calls, CODED_PRELUDE)
    assert out == {
        "part": [20, 22],
        "upper": [20, 22],
        "longer": [22],
        "second_card": [20],
        "accent": [20],
        "spaces": [20],
        "number": [21],
        "both_cards": [20],
        "name": [20],
        "code_and_number": [],
        "nothing": [],
    }


def test_cards_found_by_the_search_are_the_ones_shown():
    """Le tessere che la ricerca trova, per codice o per numero: quelle che il telefono mostra sotto il nome."""
    found = "(i, q) => searchedCards(persons[i], q).map((c) => c.id)"
    out = run(
        {
            "code": f"({found})(0, 'CICC')",
            "second": f"({found})(0, 'avila 7')",
            "number": f"({found})(0, '04FF00')",
            "every": f"({found})(0, '04')",
            "no_code": f"({found})(1, 'cc00')",
            "by_name": f"({found})(0, 'gialli')",
            "empty": f"({found})(0, '   ')",
            "no_cards": f"({found})(3, 'tessera')",
        },
        CODED_PRELUDE,
    )
    assert out == {
        "code": [1],
        "second": [2],
        "number": [2],
        "every": [1, 2],
        "no_code": [3],
        "by_name": [],
        "empty": [],
        "no_cards": [],
    }


def test_desktop_table_shows_every_card_like_the_sheet():
    """Colonna Tessere: ogni tessera come nella scheda, il codice sopra e il numero sotto più piccolo; senza codice il
    numero; tutto anche nel title. Con la ricerca che trova delle tessere la tabella ha data-cards (la colonna resta
    in vista), e le righe sono le stesse con e senza ricerca."""
    persons = [*CODED, person(24, "Lia", "Rame", cards=[{"id": 5, "number": "04ee", "printed_code": "A&B <7>"}])]
    prelude = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(persons)};"
    table = (
        "(q) => { personFilters.q = q; personFilters.sort = 'cognome:asc'; "
        "return personTable(sortPersons(persons.filter((p) => personMatches(p, q)), 'cognome:asc')); }"
    )
    out = run(
        {
            "plain": f"({table})('')",
            "code": f"({table})('cicc')",
            "name": f"({table})('marta')",
            "number": f"({table})('04cc')",
        },
        prelude,
        names=DATES + SORT + TABLE,
    )
    html = out["plain"]
    cells = dict(re.findall(r'<tr data-person="(\d+)".*?<td class="c-cards"(.*?)</td>', html, re.S))
    assert cells["20"] == (
        ' title="Ciccio · 04ab12cd\nÁvila  7 · 04ff0011">'
        '<div class="p-card"><div class="p-code">Ciccio</div><div class="card-no mono">04ab12cd</div></div>'
        '<div class="p-card"><div class="p-code">Ávila  7</div><div class="card-no mono">04ff0011</div></div>'
    )
    assert cells["21"] == ' title="04cc0099"><div class="p-card"><div class="mono">04cc0099</div></div>'
    assert cells["23"] == '><span class="muted">—</span>'
    # Il codice si scrive com'è, senza diventare HTML
    assert cells["24"] == (
        ' title="A&amp;B &lt;7&gt; · 04ee"><div class="p-card"><div class="p-code">A&amp;B &lt;7&gt;</div>'
        '<div class="card-no mono">04ee</div></div>'
    )
    assert '<table class="table p-table" data-sorted="cognome">' in html
    assert '<table class="table p-table" data-sorted="cognome" data-cards>' in out["code"]
    assert '<table class="table p-table" data-sorted="cognome" data-cards>' in out["number"]
    # Trovata per nome: niente data-cards, la colonna segue lo spazio come sempre
    assert '<table class="table p-table" data-sorted="cognome">' in out["name"]
    rows = {pid: row for row, pid in re.findall(r'(<tr data-person="(\d+)".*?</tr>)', html, re.S)}
    found = {pid: row for row, pid in re.findall(r'(<tr data-person="(\d+)".*?</tr>)', out["code"], re.S)}
    assert found["20"] == rows["20"] and found["22"] == rows["22"]


def test_phone_rows_show_the_cards_the_search_found():
    """Telefono: la riga non mostra le tessere, solo quelle che la ricerca ha trovato, sotto il nome, con il codice in
    evidenza e il numero più piccolo (senza codice il numero); niente per una persona trovata per nome."""
    rows = (
        "(q) => { personFilters.q = q; personFilters.sort = 'cognome:asc'; "
        "return personRows(sortPersons(persons.filter((p) => personMatches(p, q)), 'cognome:asc')); }"
    )
    out = run(
        {
            "code": f"({rows})('Cicc')",
            "number": f"({rows})('04cc')",
            "name": f"({rows})('marta')",
            "all": f"({rows})('')",
        },
        CODED_PRELUDE,
        names=DATES + SORT + TABLE,
    )

    def found(html):
        return {
            pid: re.findall(
                r'<span class="p-sub p-found" title="([^"]*)"><svg class="icon".*?</svg>'
                r'(.*?<span class="mono">[^<]*</span>)</span>',
                row,
                re.S,
            )
            for row, pid in re.findall(r'(<div class="person-row" data-person="(\d+)".*?</div>)', html, re.S)
        }

    code = found(out["code"])
    assert code["20"] == [("Ciccio · 04ab12cd", '<span class="p-code">Ciccio</span><span class="mono">04ab12cd</span>')]
    assert code["22"] == [
        ("cICCIObello · 04dd2200", '<span class="p-code">cICCIObello</span><span class="mono">04dd2200</span>')
    ]
    assert found(out["number"]) == {"21": [("04cc0099", '<span class="mono">04cc0099</span>')]}
    assert found(out["name"]) == {"20": []}
    assert "p-found" not in out["all"]


@pytest.mark.parametrize(
    ("storage", "expected"),
    [
        ("const localStorage = {getItem: () => 'matricola:desc', setItem() {}};", "matricola:desc"),
        ("const localStorage = {getItem: () => 'colore:asc', setItem() {}};", "cognome:asc"),
        ("const localStorage = {getItem: () => null, setItem() {}};", "cognome:asc"),
        ("const localStorage = {getItem() { throw new Error('bloccato'); }, setItem() {}};", "cognome:asc"),
        ("", "cognome:asc"),
    ],
)
def test_chosen_order_is_remembered_if_the_browser_can(storage, expected):
    out = run({"sort": "personFilters.sort", "saved": "savedPersonSort()"}, storage=storage)
    assert out == {"sort": expected, "saved": expected}


def _rows(body: str) -> list[str]:
    return sorted(re.findall(r"<tr data-person=.*?</tr>", body, re.S))


def test_desktop_table_headers_sort_and_rows_do_not_depend_on_the_order():
    version = "2026-11-13T10:00:00.123456+00:00"
    persons = [{**p, "photo": version if p["id"] == 1 else ""} for p in PERSONS]
    prelude = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(persons)};"
    table = "(s) => { personFilters.sort = s; return personTable(sortPersons(persons, s)); }"
    out = run(
        {
            "matricola": f"({table})('matricola:asc')",
            "desc": f"({table})('accesso:desc')",
            "created": f"({table})('inserimento:desc')",
            "rows_a": f"({table})('cognome:asc').split('<tbody>')[1]",
            "rows_b": f"({table})('ditta:asc').split('<tbody>')[1]",
        },
        prelude,
        names=DATES + SORT + TABLE,
    )
    html = out["matricola"]
    assert '<table class="table p-table" data-sorted="matricola">' in html
    heads = re.findall(r'<th class="(c-[a-z]+)"[^>]*>', html)
    assert heads == ["c-photo", "c-last", "c-first", "c-emp", "c-company", "c-cat", "c-cards", "c-access", "c-state"]
    # Un pulsante in ogni intestazione che ordina, aria-sort solo su quella scelta
    assert re.findall(r'aria-sort="(\w+)"', html) == ["ascending"]
    assert re.search(
        r'<th class="c-emp" scope="col" aria-sort="ascending"><button type="button" class="th-sort" '
        r'data-sort="matricola"',
        html,
    )
    assert re.findall(r'data-sort="(\w+)"', html) == ["cognome", "nome", "matricola", "ditta", "tipologia", "accesso"]
    assert re.findall(r'aria-sort="(\w+)"', out["desc"]) == ["descending"]
    assert '<span class="arrow" aria-hidden="true">↓</span>' in out["desc"]
    # Con l'ordine per data di inserimento la sua colonna, con la data in cifre e quella completa nel title
    assert '<th class="c-extra" scope="col" aria-sort="descending">' in out["created"]
    assert ">10/01/2026</td>" in out["created"]
    # Riga: apre la scheda da tastiera, foto con la versione codificata, caricata solo quando serve
    assert '<tr data-person="1" data-key="p1" tabindex="0">' in html
    assert 'src="api/persons/1/photo?v=2026-11-13T10%3A00%3A00.123456%2B00%3A00"' in html
    assert 'loading="lazy" decoding="async"' in html
    assert '<span class="avatar sm">AB</span>' in html
    # Le righe sono le stesse con un altro ordine (cambiano solo posto): il ridisegno le sposta e basta
    assert _rows(out["rows_a"]) == _rows(out["rows_b"])


def test_phone_rows_and_section_letters_beyond_twelve_persons():
    many = [person(100 + i, f"Nome{i:02d}", ["Abete", "Betulla", "Cedro"][i % 3]) for i in range(13)]
    prelude = f"store.categories = {json.dumps(CATEGORIES)}; const persons = {json.dumps(many)};"
    rows = "(s, list) => { personFilters.sort = s; return personRows(sortPersons(list, s)); }"
    out = run(
        {
            "names": f"({rows})('cognome:asc', persons)",
            "first": f"({rows})('nome:desc', persons)",
            "emp": f"({rows})('matricola:asc', persons)",
            "few": f"({rows})('cognome:asc', persons.slice(0, 12))",
        },
        prelude,
        names=DATES + SORT + TABLE,
    )
    assert re.findall(r'<div class="day" data-key="h(\w)">', out["names"]) == ["A", "B", "C"]
    assert re.findall(r'<div class="day" data-key="h(\w)">', out["first"]) == ["N"]
    assert '<div class="day"' not in out["emp"] and '<div class="day"' not in out["few"]
    assert out["emp"].count('<span class="p-sub">Senza matricola</span>') == 13
    assert '<span class="p-sub">' not in out["names"]
    assert 'role="button" tabindex="0"' in out["names"]


def test_photo_is_prepared_in_the_browser_and_sent_inside_the_waiting_box():
    """Foto della persona dalla scheda: campo file per immagini senza capture (sul telefono fotocamera o galleria),
    letta con l'orientamento della foto, ridotta a 600 px in JPEG 0,85, mandata e tolta dentro il riquadro di
    attesa. Nel Dettaglio Evento la Foto in Anagrafica solo per chi vede le persone."""
    page = PAGE.read_text(encoding="utf-8")
    [photo] = [i for i in re.findall(r'<input type="file"[^>]*>', page) if "data-photo-file" in i]
    assert 'accept="image/*"' in photo and "capture" not in photo
    assert 'createImageBitmap(file, {imageOrientation: "from-image"})' in page
    assert "const PHOTO_SIDE = 600;" in page and "for (const quality of [0.85," in page
    assert 'const PHOTO_UNREADABLE = "questa immagine non si può leggere: usa una foto JPEG o PNG";' in page
    assert re.search(
        r'run\(async \(\) => api\(`api/persons/\$\{id\}/photo`, \{method: "PUT", raw: await photoBlob\(file\)\}\),'
        r'[^\n]*"Caricamento della foto"\)',
        page,
    )
    assert re.search(r'\{method: "DELETE"\}\), "Foto tolta", "Rimozione della foto"\)', page)
    assert 'e.person_id && can("persone") ? `<div class="ev-face"' in page


# Chromium vero (Playwright, se c'è): l'elenco con i Codici Stampati disegnato con le regole CSS della pagina com'è, a
# 1280 px (tabella) e a 390 px (righe del telefono), in tema chiaro e scuro
LAYOUT = r"""
const { chromium } = require("playwright");
(async () => {
  const browser = await chromium.launch();
  const out = {};
  const cases = [
    ["wide", 1280, false, "", "light"],
    ["wide_dark", 1280, false, "cicc", "dark"],
    ["narrow", 1000, false, "", "light"],
    ["narrow_search", 1000, false, "cicc", "light"],
    ["narrow_by_date", 1280, false, "04cc", "light", "inserimento:desc"],
    ["phone", 390, true, "", "light"],
    ["phone_code", 390, true, "CICC", "light"],
    ["phone_long", 390, true, "04AAB", "dark"],
  ];
  for (const [name, width, phone, q, scheme, sort = "cognome:asc"] of cases) {
    const page = await browser.newPage({viewport: {width, height: 800}, colorScheme: scheme});
    await page.setContent(__PAGE__);
    out[name] = await page.evaluate(([phone, q, sort]) => {
      personFilters.q = q;
      personFilters.sort = sort;
      const shown = sortPersons(persons.filter((p) => personMatches(p, q)), sort);
      document.getElementById("p-list").innerHTML = phone ? personRows(shown) : personTable(shown);
      const de = document.documentElement;
      const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).display !== "none";
      // Testo intero: niente puntini (scrollWidth oltre la larghezza vera, con il mezzo pixel del browser)
      const whole = (el) => el.scrollWidth <= el.getBoundingClientRect().width + 0.5;
      const style = (el) => el && {weight: getComputedStyle(el).fontWeight, size: getComputedStyle(el).fontSize,
        color: getComputedStyle(el).color};
      const head = document.querySelector("th.c-cards");
      const codes = [...document.querySelectorAll(".p-code")];
      const numbers = [...document.querySelectorAll(".p-card .card-no, .p-card > .mono, .p-found .mono")];
      return {
        scroll: de.scrollWidth <= de.clientWidth,
        column: head ? visible(head) : null,
        codes: codes.filter(visible).map((el) => el.textContent),
        numbers: numbers.filter(visible).map((el) => [el.textContent, whole(el)]),
        code: style(codes.find(visible)),
        number: style(document.querySelector(".p-card .card-no, .p-found .mono")),
        found: [...document.querySelectorAll(".p-found")].map((el) => el.closest("[data-person]").dataset.person),
        rows: document.querySelectorAll("[data-person]").length,
      };
    }, [phone, q, sort]);
    await page.close();
  }
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
def test_list_with_printed_codes_in_a_real_browser_at_1280_and_390():
    """Tabella: la colonna Tessere con il codice in evidenza (grassetto) e il numero intero, più piccolo e più chiaro;
    con il riquadro stretto la colonna sparisce, tranne quando la ricerca trova delle tessere (anche con l'ordine per
    data). Telefono: senza ricerca nessuna tessera; con la ricerca per codice o per numero la tessera sotto il nome,
    con il numero intero anche dopo un codice lungo. Mai scorrimento orizzontale."""
    page = PAGE.read_text(encoding="utf-8")
    css = re.search(r"<style>(.*?)</style>", page, re.S).group(1)
    persons = [
        *CODED,
        person(
            24,
            "Elena",
            "Bruno",
            cards=[{"id": 6, "number": "04aab2c200993f", "printed_code": "Riserva Magazzino Lunga"}],
        ),
    ]
    script = "\n".join(
        [
            "const store = {me: {time_zone: 'Europe/Rome'}, categories: " + json.dumps(CATEGORIES) + ", persons: []};",
            "const persons = " + json.dumps(persons) + ";",
            _declarations(DATES + SORT + TABLE),
        ]
    )
    html = (
        f'<!doctype html><html lang="it"><head><meta charset="utf-8"><style>{css}</style></head><body>'
        '<div class="main"><div style="padding:16px"><div class="card flush p-table-wrap">'
        f'<div class="list" id="p-list"></div></div></div></div><script>{script}</script></body></html>'
    )
    result = subprocess.run(
        ["node", "-e", LAYOUT.replace("__PAGE__", json.dumps(html))], capture_output=True, text=True, timeout=120
    )
    if result.returncode and "Executable doesn't exist" in result.stderr:
        pytest.skip("Chromium di Playwright non installato")
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert all(case["scroll"] for case in out.values()), out
    wide = out["wide"]
    assert wide["column"] is True and wide["rows"] == 5
    assert wide["codes"] == ["Riserva Magazzino Lunga", "Ciccio", "Ávila  7", "cICCIObello"]
    # Ogni numero intero, anche quello di una tessera senza codice (Verdi); nessuno per chi non ha tessere (Bruni)
    assert wide["numbers"] == [
        ["04aab2c200993f", True],
        ["04ab12cd", True],
        ["04ff0011", True],
        ["04dd2200", True],
        ["04cc0099", True],
    ]
    # Il codice in evidenza, il numero più piccolo e più chiaro, in tutti e due i temi
    for case in (wide, out["wide_dark"]):
        assert int(case["code"]["weight"]) >= 600
        assert float(case["number"]["size"].removesuffix("px")) < float(case["code"]["size"].removesuffix("px"))
        assert case["number"]["color"] != case["code"]["color"]
    assert out["wide_dark"]["codes"] == ["Ciccio", "Ávila  7", "cICCIObello"]
    # Riquadro stretto: la colonna lascia il posto, tranne quando la ricerca trova delle tessere
    assert out["narrow"]["column"] is False and out["narrow"]["numbers"] == []
    assert out["narrow_search"]["column"] is True
    assert out["narrow_search"]["numbers"] == [["04ab12cd", True], ["04ff0011", True], ["04dd2200", True]]
    assert out["narrow_by_date"]["column"] is True and out["narrow_by_date"]["numbers"] == [["04cc0099", True]]
    # Telefono
    assert out["phone"]["column"] is None and out["phone"]["rows"] == 5 and out["phone"]["found"] == []
    assert out["phone"]["numbers"] == []
    assert out["phone_code"]["found"] == ["20", "22"]
    assert out["phone_code"]["codes"] == ["Ciccio", "cICCIObello"]
    assert out["phone_code"]["numbers"] == [["04ab12cd", True], ["04dd2200", True]]
    assert int(out["phone_code"]["code"]["weight"]) >= 600
    # Codice lungo: si accorcia il codice, il numero cercato resta intero
    assert out["phone_long"]["found"] == ["24"]
    assert out["phone_long"]["numbers"] == [["04aab2c200993f", True]]
