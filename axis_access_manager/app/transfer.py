"""Esportazione CSV di registro e anagrafica, importazione dell'anagrafica da CSV."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .db import SOURCE_LABELS
from .rules import RuleError, normalize_validity
from .service import AccessService, ServiceError, normalize_card, normalize_name
from .vapix import VapixError

EVENT_LABELS = {
    "concesso": "Accesso Concesso",
    "negato": "Accesso Negato",
    "lettura": "Tessera Letta",
    "sconosciuta": "Tessera Sconosciuta",
    "apertura_remota": "Apertura Remota",
    "uscita": "Uscita",
    "allarme": "Allarme Porta",
    "manomissione": "Manomissione",
    "citofono": "Chiamata Citofono",
}

IMPORT_COLUMNS = ["nome", "tessera", "gruppo", "valida_dal", "valida_al", "ditta", "note", "tipologia", "stato"]
# Con i secondi come nel file esportato, o senza come lo risalva spesso un foglio di calcolo
DATE_FORMATS = [
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
]
DATE_COLUMNS = (("valida_dal", "valid_from"), ("valida_al", "valid_to"))
CARD_STATES = ("attiva", "sospesa")
# Esito della riga (l'interfaccia lo usa per i colori) → etichetta da mostrare
OUTCOME_LABELS = {
    "da importare": "Da Importare",
    "ok": "Importata",
    "da aggiornare": "Da Aggiornare",
    "aggiornata": "Aggiornata",
    "saltata": "Saltata",
    "errore": "Errore",
}
# Testi che un foglio di calcolo leggerebbe come formule
FORMULA_START = ("=", "+", "-", "@")
# Tessere che un foglio di calcolo convertirebbe in numero: perdono gli zeri iniziali o le ultime cifre
NUMERIC_CARD = re.compile(r"\d+(?:[eE]\d+)?")
# Carattere che sostituisce i byte non leggibili: file in un'altra codifica (per esempio Excel in ANSI) letto come UTF-8
UNREADABLE = "\ufffd"


def _local(ts: str) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return ts


def _csv(rows: list[list]) -> str:
    buffer = io.StringIO()
    # Punto e virgola e BOM: Excel in italiano apre il file senza passaggi
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerows(rows)
    return "﻿" + buffer.getvalue()


def _text(value: str) -> str:
    """Testo che il foglio di calcolo non prende per una formula: apostrofo davanti, tolto all'importazione."""
    return "'" + value if value.startswith(FORMULA_START) else value


def _card_text(number: str) -> str:
    """Tessera come formula di testo (="04123456") se il foglio di calcolo la convertirebbe in numero."""
    return f'="{number}"' if NUMERIC_CARD.fullmatch(number) else number


def events_csv(events: list[dict], doors: dict[str, str] | None = None) -> str:
    """Registro in CSV; con più porte aggiunge la colonna Porta (nomi dati da doors: token → nome)."""
    doors = doors or {}
    with_door = len(doors) > 1
    rows = [
        ["Data e Ora", "Evento"]
        + (["Porta"] if with_door else [])
        + ["Persona", "Tessera", "Dettaglio", "Origine", "Recuperato"]
    ]
    for event in events:
        door = [_text(doors.get(event.get("door") or "", event.get("door") or ""))] if with_door else []
        rows.append(
            [
                _local(event["ts"]),
                EVENT_LABELS.get(event["kind"], event["kind"]),
                *door,
                _text(event.get("person_name") or ""),
                _card_text(event.get("card") or ""),
                _text(event.get("detail") or ""),
                # Lettore, controller o porta come nel registro; altrimenti il nome di chi ha aperto
                _text(SOURCE_LABELS.get(event.get("source") or "", event.get("source") or "")),
                # Arrivato dopo, dal registro interno del controller (MQTT o add-on fermi in quel momento)
                "sì" if event.get("recovered") else "",
            ]
        )
    return _csv(rows)


def persons_csv(
    persons: list[dict],
    group_names: dict[str, str],
    last_access: dict[int, str],
    categories: dict[int, str] | None = None,
) -> str:
    """Stesse colonne dell'importazione, più l'ultimo accesso: il file si può reimportare."""
    categories = categories or {}
    rows = [IMPORT_COLUMNS + ["ultimo_accesso"]]
    for person in persons:
        for card in person["cards"]:
            rows.append(
                [
                    _text(person["name"]),
                    _card_text(card["number"]),
                    _text(group_names.get(card.get("access_profile") or "", card.get("access_profile") or "")),
                    _local(card.get("valid_from") or ""),
                    _local(card.get("valid_to") or ""),
                    _text(person.get("company") or ""),
                    _text(person.get("notes") or ""),
                    _text(categories.get(person.get("category_id") or 0, "")),
                    "attiva" if card["enabled"] else "sospesa",
                    _local(last_access.get(person["id"], "")),
                ]
            )
    return _csv(rows)


def parse_date(value: str, end_of_day: bool = False) -> str:
    """Data del CSV (gg/mm/aaaa, con ora facoltativa) in formato ISO locale; vuoto se assente.

    Con end_of_day una data senza ora vale fino a fine giornata: "valida al 31/12" comprende il 31.
    """
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if end_of_day and "%H" not in fmt:
            parsed = parsed.replace(hour=23, minute=59, second=59)
        return parsed.isoformat()
    raise ServiceError(f"data non riconosciuta: {value} (usa gg/mm/aaaa)")


def _cell(value: str) -> str:
    """Cella del CSV senza le protezioni dell'esportazione: ="…" attorno alla tessera, apostrofo davanti al testo."""
    value = value.strip()
    if match := re.fullmatch(r'="(.*)"', value, re.DOTALL):
        value = match[1].strip()
    if value.startswith("'") and value[1:].startswith(FORMULA_START):
        value = value[1:]
    return value


def read_import(text: str) -> list[dict]:
    """Righe del CSV come dizionari con le colonne dell'intestazione, o di IMPORT_COLUMNS se manca.

    Senza intestazione una colonna c'è solo dove la cella ha un valore: le celle vuote lasciano i valori attuali.
    """
    text = text.lstrip("﻿").strip()
    if not text:
        raise ServiceError("il file è vuoto")
    first_line = text.splitlines()[0]
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [cells for cells in ([_cell(cell) for cell in row] for row in reader) if any(cells)]
    if not rows:
        # Solo separatori: un foglio svuotato e risalvato
        raise ServiceError("il file è vuoto")
    header = [re.sub(r"\s+", "_", cell.lower()) for cell in rows[0]]
    with_header = "nome" in header and "tessera" in header
    if with_header:
        columns, rows = header, rows[1:]
    else:
        columns = IMPORT_COLUMNS
    if len(rows) > 2000:
        raise ServiceError("massimo 2000 righe per importazione")
    result = []
    for index, row in enumerate(rows, start=2 if with_header else 1):
        if with_header:
            item = {column: (row[i] if i < len(row) else "") for i, column in enumerate(columns)}
        else:
            item = {column: row[i] for i, column in enumerate(columns) if i < len(row) and row[i]}
        item["riga"] = index
        result.append(item)
    return result


def _validity(value: str) -> str:
    try:
        return normalize_validity(value)
    except RuleError as err:
        raise ServiceError(str(err)) from err


def _requested_date(item: dict, column: str, current: str) -> str | None:
    """Data chiesta dalla riga nel formato del controller; None se la colonna non c'è.

    Un foglio di calcolo può risalvare le date senza secondi: una cella senza secondi che coincide al minuto con
    la data attuale non è una modifica.
    """
    if column not in item:
        return None
    value = _validity(parse_date(item[column], end_of_day=column == "valida_al"))
    if value and value[:16] == current[:16] and not re.search(r"\d:\d\d:\d\d", item[column]):
        return current
    return value


def _wanted(item: dict, card: dict, group: str, category: dict | None) -> tuple:
    """Valori della persona chiesti dalla riga: le righe della stessa persona devono essere d'accordo."""
    return (
        group,
        category["id"] if category else 0,
        item.get("ditta"),
        item.get("note"),
        *(_requested_date(item, column, card.get(field) or "") for column, field in DATE_COLUMNS),
    )


def _changes(item: dict, person: dict, card: dict, group: str, category: dict | None, names: dict) -> dict:
    """Cosa cambia per una persona già registrata, solo per le colonne presenti nel file.

    Gruppo e tipologia vuoti lasciano quelli attuali; date, ditta e note vuote li svuotano (come nel file esportato).
    Gruppo e validità attuali sono quelli di card, una tessera della persona.
    """
    changes: dict = {}
    if group and group != card.get("access_profile"):
        changes["gruppo"] = group
    for column, field in DATE_COLUMNS:
        value = _requested_date(item, column, card.get(field) or "")
        if value is not None and value != (card.get(field) or ""):
            changes[column] = value
    for column, field in (("ditta", "company"), ("note", "notes")):
        if column in item and item[column] != (person.get(field) or ""):
            changes[column] = item[column]
    if category and category["id"] != person.get("category_id"):
        changes["tipologia"] = category
    labels = {
        "gruppo": lambda v: (
            f"gruppo {names.get(card.get('access_profile'), card.get('access_profile') or '—')} → {names.get(v, v)}"
        ),
        "valida_dal": lambda v: f"valida dal {_local(card.get('valid_from') or '') or '—'} → {_local(v) or '—'}",
        "valida_al": lambda v: f"valida al {_local(card.get('valid_to') or '') or '—'} → {_local(v) or '—'}",
        "ditta": lambda v: f"ditta {person.get('company') or '—'} → {v or '—'}",
        "note": lambda v: "note modificate",
        "tipologia": lambda v: f"tipologia → {v['name']}",
    }
    return {key: (value, labels[key](value)) for key, value in changes.items()}


async def _update(service: AccessService, person: dict, card: dict, changes: dict, user: str) -> None:
    """Applica le modifiche a una persona: prima la sospensione della tessera card (se chiesta), poi ditta e note
    solo nell'add-on, gruppo e validità anche sul controller."""
    if "stato" in changes:
        await service.set_card_enabled(card["id"], False, user)
    if "ditta" in changes or "note" in changes:
        await service.update_person(
            person["id"],
            user,
            company=changes["ditta"][0] if "ditta" in changes else None,
            notes=changes["note"][0] if "note" in changes else None,
        )
    if {"gruppo", "valida_dal", "valida_al", "tipologia"} & changes.keys():
        await service.set_person_access(
            person["id"],
            # Una nuova tipologia porta con sé il suo accesso, salvo un gruppo chiesto nella stessa riga
            changes["gruppo"][0]
            if "gruppo" in changes
            else ""
            if "tipologia" in changes
            else card.get("access_profile") or "",
            changes["valida_dal"][0] if "valida_dal" in changes else card.get("valid_from") or "",
            changes["valida_al"][0] if "valida_al" in changes else card.get("valid_to") or "",
            user,
            changes["tipologia"][0]["id"] if "tipologia" in changes else None,
        )


def _card_number(value: str) -> str:
    """Numero della tessera; quello in notazione scientifica viene da un foglio di calcolo che l'ha convertito."""
    value = value.removeprefix("'")
    if re.fullmatch(r"\d+(?:[.,]\d+)?[eE][+-]\d+", value):
        raise ServiceError(
            f"numero tessera in notazione scientifica ({value}): il foglio di calcolo l'ha letto come un numero, "
            "riesporta le persone o scrivi la tessera come testo"
        )
    return normalize_card(value)


async def import_persons(service: AccessService, text: str, groups: list[dict], user: str, apply: bool) -> dict:
    """Controlla (apply=False) o esegue l'importazione.

    Le tessere nuove si aggiungono; per le persone già registrate si aggiornano gruppo, validità, ditta, note e
    tipologia se nel file sono diversi, altrimenti la riga è saltata. Lo stato "sospesa" sospende la tessera,
    "attiva" non riattiva una tessera sospesa.
    """
    names = {g["token"]: g["name"] for g in groups}
    # Persone già viste in una riga precedente (una persona ha una riga per tessera) → valori chiesti.
    # Chiave: id della persona, o il nome di una persona nuova finché non è creata (nell'anteprima non lo è mai)
    planned: dict[int | str, tuple] = {}
    by_name = {g["name"].lower(): g["token"] for g in groups}
    tokens = {g["token"] for g in groups}
    categories = {c["name"].lower(): c for c in service.db.list_categories()}
    # Tessere registrate di sole cifre con zeri iniziali, per il numero senza gli zeri: un foglio di calcolo le
    # riduce così
    zeroless = {
        c["number"].lstrip("0"): c["number"]
        for c in service.db.all_cards()
        if c["number"].isdigit() and c["number"].startswith("0")
    }
    seen_cards: set[str] = set()
    results = []
    for item in read_import(text):
        outcome = {"riga": item["riga"], "nome": item.get("nome", ""), "tessera": item.get("tessera", "")}
        try:
            # Lettere accentate perse: nomi, ditta e note andrebbero storpiati nell'app e sul controller
            if any(UNREADABLE in value for value in item.values() if isinstance(value, str)):
                raise ServiceError("caratteri non leggibili: salva il file come CSV UTF-8 e riprova")
            name = normalize_name(item.get("nome", ""))
            number = _card_number(item.get("tessera", ""))
            group_text = item.get("gruppo", "")
            group = ""
            if group_text:
                group = by_name.get(group_text.lower()) or (group_text if group_text in tokens else "")
                if not group:
                    raise ServiceError(f"gruppo sconosciuto: {group_text}")
            category_text = item.get("tipologia", "")
            category = categories.get(category_text.lower()) if category_text else None
            if category_text and category is None:
                raise ServiceError(f"tipologia sconosciuta: {category_text}")
            state = item.get("stato", "").lower()
            if state and state not in CARD_STATES:
                raise ServiceError(f"stato sconosciuto: {item['stato']} (usa attiva o sospesa)")
            valid_from = parse_date(item.get("valida_dal", ""))
            valid_to = parse_date(item.get("valida_al", ""), end_of_day=True)
            if number.lower() in seen_cards:
                raise ServiceError("tessera ripetuta nel file")
            seen_cards.add(number.lower())
            existing = service.db.card_by_number(number)
            if existing:
                person = service.db.get_person(existing["person_id"])
                card = existing
            else:
                if number in zeroless:
                    raise ServiceError(
                        f"la tessera {number} è la {zeroless[number]} senza gli zeri iniziali: il foglio di calcolo "
                        "l'ha letta come un numero (se è un'altra tessera aggiungila dalla scheda della persona)"
                    )
                matches = [p for p in service.db.all_persons() if p["name"].lower() == name.lower()]
                if len(matches) > 1:
                    raise ServiceError("più persone con questo nome: aggiungi la tessera dall'elenco")
                person = service.db.get_person(matches[0]["id"]) if matches else None
                # Gruppo e validità attuali della persona: quelli della sua prima tessera
                card = person["cards"][0] if person and person["cards"] else {}
            key = person["id"] if person else name.lower()
            wanted = _wanted(item, card, group, category)
            repeated = key in planned
            if repeated:
                if wanted != planned[key]:
                    raise ServiceError("valori diversi da un'altra riga della stessa persona")
                changes = {}
            else:
                planned[key] = wanted
                changes = _changes(item, person, card, group, category, names) if person else {}
            if existing:
                if state == "sospesa" and existing["enabled"]:
                    changes = {"stato": (False, "tessera sospesa"), **changes}
                # Un file esportato prima non riapre una tessera sospesa nel frattempo
                kept = state == "attiva" and not existing["enabled"]
                note = "; la tessera resta sospesa: riattivala dalla scheda della persona" if kept else ""
                if not changes:
                    outcome.update(esito="saltata", dettaglio=f"già registrata a {existing['person_name']}{note}")
                    results.append(outcome)
                    continue
                if apply:
                    await _update(service, person, existing, changes, user)
                detail = "; ".join(label for _, label in changes.values()) + note
                outcome.update(esito="aggiornata" if apply else "da aggiornare", dettaglio=detail)
                results.append(outcome)
                continue
            # Tessera nuova: va alla persona con lo stesso nome, anche se la crea una riga precedente del file
            adds = person is not None or (repeated and not apply)
            action = "tessera aggiunta" if adds else "nuova persona"
            if state == "sospesa":
                action += ", sospesa" if adds else ", tessera sospesa"
            if apply:
                # Una tessera «sospesa» si scrive già sospesa: non resta mai attiva sul controller
                if person:
                    await service.add_card(person["id"], number, user, enabled=state != "sospesa")
                else:
                    saved = await service.create_person(
                        name,
                        number,
                        user,
                        group,
                        valid_from,
                        valid_to,
                        item.get("ditta", ""),
                        item.get("note", ""),
                        category["id"] if category else 0,
                        enabled=state != "sospesa",
                    )
                    planned[saved["id"]] = wanted
                if changes:
                    await _update(service, person, card, changes, user)
            if changes:
                # La riga cambia anche la persona: si mostra come un aggiornamento
                detail = "; ".join([action, *(label for _, label in changes.values())])
                outcome.update(esito="aggiornata" if apply else "da aggiornare", dettaglio=detail)
            else:
                outcome.update(esito="ok" if apply else "da importare", dettaglio=action)
        except ServiceError as err:
            outcome.update(esito="errore", dettaglio=str(err))
        except VapixError as err:
            # Come il gestore degli errori del controller in main: niente prefisso se il messaggio lo nomina già
            text = str(err)
            outcome.update(esito="errore", dettaglio=text if "controller" in text else f"errore del controller: {text}")
        results.append(outcome)
    for outcome in results:
        # Etichetta da mostrare; esito resta il valore su cui ragiona l'interfaccia
        outcome["etichetta"] = OUTCOME_LABELS[outcome["esito"]]
    summary = {
        "righe": len(results),
        "ok": sum(1 for r in results if r["esito"] in ("ok", "da importare")),
        "aggiornate": sum(1 for r in results if r["esito"] in ("aggiornata", "da aggiornare")),
        "saltate": sum(1 for r in results if r["esito"] == "saltata"),
        "errori": sum(1 for r in results if r["esito"] == "errore"),
    }
    return {"applicato": apply, "riepilogo": summary, "righe": results}
