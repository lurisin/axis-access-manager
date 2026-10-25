"""Esportazione CSV di registro e anagrafica, importazione dell'anagrafica da CSV."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

from .rules import RuleError, normalize_validity
from .service import AccessService, ServiceError, normalize_card, normalize_name
from .vapix import VapixError

EVENT_LABELS = {
    "concesso": "Accesso Concesso",
    "negato": "Accesso Negato",
    "lettura": "Tessera Letta",
    "sconosciuta": "Tessera Sconosciuta",
    "apertura_remota": "Apertura Remota",
    "allarme": "Allarme Porta",
    "manomissione": "Manomissione",
    "citofono": "Citofono",
}

IMPORT_COLUMNS = ["nome", "tessera", "gruppo", "valida_dal", "valida_al", "ditta", "note", "tipologia"]
DATE_FORMATS = ["%d/%m/%Y %H:%M", "%d/%m/%Y", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"]


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


def events_csv(events: list[dict], doors: dict[str, str] | None = None) -> str:
    """Registro in CSV; con più porte aggiunge la colonna Porta (nomi dati da doors: token → nome)."""
    doors = doors or {}
    with_door = len(doors) > 1
    rows = [
        ["Data e Ora", "Evento"] + (["Porta"] if with_door else []) + ["Persona", "Tessera", "Dettaglio", "Origine"]
    ]
    for event in events:
        door = [doors.get(event.get("door") or "", event.get("door") or "")] if with_door else []
        rows.append(
            [
                _local(event["ts"]),
                EVENT_LABELS.get(event["kind"], event["kind"]),
                *door,
                event.get("person_name") or "",
                event.get("card") or "",
                event.get("detail") or "",
                event.get("source") or "",
            ]
        )
    return _csv(rows)


def persons_csv(
    persons: list[dict],
    group_names: dict[str, str],
    last_access: dict[int, str],
    categories: dict[int, str] | None = None,
) -> str:
    """Stesse colonne dell'importazione, più stato e ultimo accesso: il file si può reimportare."""
    categories = categories or {}
    rows = [IMPORT_COLUMNS + ["stato", "ultimo_accesso"]]
    for person in persons:
        for card in person["cards"]:
            rows.append(
                [
                    person["name"],
                    card["number"],
                    group_names.get(card.get("access_profile") or "", card.get("access_profile") or ""),
                    _local(card.get("valid_from") or ""),
                    _local(card.get("valid_to") or ""),
                    person.get("company") or "",
                    person.get("notes") or "",
                    categories.get(person.get("category_id") or 0, ""),
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


def read_import(text: str) -> list[dict]:
    """Righe del CSV come dizionari con le colonne di IMPORT_COLUMNS."""
    text = text.lstrip("﻿").strip()
    if not text:
        raise ServiceError("il file è vuoto")
    first_line = text.splitlines()[0]
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    header = [re.sub(r"\s+", "_", cell.strip().lower()) for cell in rows[0]]
    if "nome" in header and "tessera" in header:
        columns, rows = header, rows[1:]
    else:
        columns = IMPORT_COLUMNS
    if len(rows) > 2000:
        raise ServiceError("massimo 2000 righe per importazione")
    result = []
    for index, row in enumerate(rows, start=2 if columns is header else 1):
        item = {column: (row[i].strip() if i < len(row) else "") for i, column in enumerate(columns)}
        item["riga"] = index
        result.append(item)
    return result


def _validity(value: str) -> str:
    try:
        return normalize_validity(value)
    except RuleError as err:
        raise ServiceError(str(err)) from err


def _changes(item: dict, person: dict, card: dict, group: str, category: dict | None, names: dict) -> dict:
    """Cosa cambia per una persona già registrata, solo per le colonne presenti nel file.

    Gruppo e tipologia vuoti lasciano quelli attuali; date, ditta e note vuote li svuotano (come nel file esportato).
    """
    changes: dict = {}
    if group and group != card.get("access_profile"):
        changes["gruppo"] = group
    for column, field in (("valida_dal", "valid_from"), ("valida_al", "valid_to")):
        if column in item:
            value = _validity(parse_date(item[column], end_of_day=column == "valida_al"))
            if value != (card.get(field) or ""):
                changes[column] = value
    for column, field in (("ditta", "company"), ("note", "notes")):
        if column in item and item[column] != (person.get(field) or ""):
            changes[column] = item[column]
    if category and category["id"] != person.get("category_id"):
        changes["tipologia"] = category
    labels = {
        "gruppo": lambda v: f"gruppo {names.get(card.get('access_profile'), card.get('access_profile') or '—')} → "
        f"{names.get(v, v)}",
        "valida_dal": lambda v: f"valida dal {_local(card.get('valid_from') or '') or '—'} → {_local(v) or '—'}",
        "valida_al": lambda v: f"valida al {_local(card.get('valid_to') or '') or '—'} → {_local(v) or '—'}",
        "ditta": lambda v: f"ditta {person.get('company') or '—'} → {v or '—'}",
        "note": lambda v: "note modificate",
        "tipologia": lambda v: f"tipologia → {v['name']}",
    }
    return {key: (value, labels[key](value)) for key, value in changes.items()}


async def _update(service: AccessService, person: dict, card: dict, changes: dict, user: str) -> None:
    """Applica le modifiche a una persona: ditta e note solo nell'add-on, gruppo e validità anche sul controller."""
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


async def import_persons(service: AccessService, text: str, groups: list[dict], user: str, apply: bool) -> dict:
    """Controlla (apply=False) o esegue l'importazione.

    Le tessere nuove si aggiungono; per quelle già registrate si aggiornano gruppo, validità, ditta, note e
    tipologia della persona se nel file sono diversi, altrimenti la riga è saltata.
    """
    names = {g["token"]: g["name"] for g in groups}
    # Persone già viste in una riga precedente (una persona ha una riga per tessera): id → valori chiesti
    planned: dict[int, tuple] = {}
    by_name = {g["name"].lower(): g["token"] for g in groups}
    tokens = {g["token"] for g in groups}
    categories = {c["name"].lower(): c for c in service.db.list_categories()}
    seen_cards: set[str] = set()
    results = []
    for item in read_import(text):
        outcome = {"riga": item["riga"], "nome": item.get("nome", ""), "tessera": item.get("tessera", "")}
        try:
            name = normalize_name(item.get("nome", ""))
            number = normalize_card(item.get("tessera", ""))
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
            valid_from = parse_date(item.get("valida_dal", ""))
            valid_to = parse_date(item.get("valida_al", ""), end_of_day=True)
            if number.lower() in seen_cards:
                raise ServiceError("tessera ripetuta nel file")
            seen_cards.add(number.lower())
            existing = service.db.card_by_number(number)
            if existing:
                person = service.db.get_person(existing["person_id"])
                # Valori chiesti dalla riga: le righe della stessa persona devono essere d'accordo
                wanted = (
                    group,
                    category["id"] if category else 0,
                    *(item.get(c) for c in ("ditta", "note")),
                    *(
                        _validity(parse_date(item[c], end_of_day=c == "valida_al")) if c in item else None
                        for c in ("valida_dal", "valida_al")
                    ),
                )
                if person["id"] in planned:
                    if wanted != planned[person["id"]]:
                        raise ServiceError("valori diversi da un'altra riga della stessa persona")
                    changes = {}
                else:
                    planned[person["id"]] = wanted
                    changes = _changes(item, person, existing, group, category, names)
                if not changes:
                    outcome.update(esito="saltata", dettaglio=f"già registrata a {existing['person_name']}")
                    results.append(outcome)
                    continue
                if apply:
                    await _update(service, person, existing, changes, user)
                detail = "; ".join(label for _, label in changes.values())
                outcome.update(esito="aggiornata" if apply else "da aggiornare", dettaglio=detail)
                results.append(outcome)
                continue
            matches = [p for p in service.db.all_persons() if p["name"].lower() == name.lower()]
            if len(matches) > 1:
                raise ServiceError("più persone con questo nome: aggiungi la tessera dall'elenco")
            action = "tessera aggiunta" if matches else "nuova persona"
            if apply:
                if matches:
                    await service.add_card(matches[0]["id"], number, user)
                else:
                    await service.create_person(
                        name,
                        number,
                        user,
                        group,
                        valid_from,
                        valid_to,
                        item.get("ditta", ""),
                        item.get("note", ""),
                        category["id"] if category else 0,
                    )
            outcome.update(esito="ok" if apply else "da importare", dettaglio=action)
        except ServiceError as err:
            outcome.update(esito="errore", dettaglio=str(err))
        except VapixError as err:
            outcome.update(esito="errore", dettaglio=f"errore del controller: {err}")
        results.append(outcome)
    summary = {
        "righe": len(results),
        "ok": sum(1 for r in results if r["esito"] in ("ok", "da importare")),
        "aggiornate": sum(1 for r in results if r["esito"] in ("aggiornata", "da aggiornare")),
        "saltate": sum(1 for r in results if r["esito"] == "saltata"),
        "errori": sum(1 for r in results if r["esito"] == "errore"),
    }
    return {"applicato": apply, "riepilogo": summary, "righe": results}
