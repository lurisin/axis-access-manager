"""Esportazione CSV di registro e anagrafica, importazione dell'anagrafica da CSV."""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime

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


def parse_date(value: str) -> str:
    """Data del CSV (gg/mm/aaaa, con ora facoltativa) in formato ISO locale; vuoto se assente."""
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).isoformat()
        except ValueError:
            continue
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


async def import_persons(service: AccessService, text: str, groups: list[dict], user: str, apply: bool) -> dict:
    """Controlla (apply=False) o esegue l'importazione. Le tessere già registrate vengono saltate."""
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
            valid_from, valid_to = parse_date(item.get("valida_dal", "")), parse_date(item.get("valida_al", ""))
            if number.lower() in seen_cards:
                raise ServiceError("tessera ripetuta nel file")
            seen_cards.add(number.lower())
            existing = service.db.card_by_number(number)
            if existing:
                outcome.update(esito="saltata", dettaglio=f"già registrata a {existing['person_name']}")
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
        "saltate": sum(1 for r in results if r["esito"] == "saltata"),
        "errori": sum(1 for r in results if r["esito"] == "errore"),
    }
    return {"applicato": apply, "riepilogo": summary, "righe": results}
