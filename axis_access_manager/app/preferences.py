"""Preferenze di ogni utente dell'app, salvate sul server per nome utente: le ritrova da qualsiasi computer o telefono,
da Home Assistant e dall'accesso diretto. Per ora le colonne dell'elenco Persone (Anagrafica), scelte e in ordine.

Stanno nel database dell'app (app_settings, vedi db.preferences_key) ma non nei backup dell'app, come le password:
sono un modo di guardare i dati, non dati dell'impianto. Non cambiano niente sul controller."""

from __future__ import annotations

import json
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# Colonne dell'elenco Persone, nell'ordine della finestra Colonne dell'Elenco: le stesse chiavi di PERSON_COLUMNS nella
# pagina (le chiavi dell'ordinamento, dove ce n'è uno)
PERSON_COLUMNS = (
    "foto",
    "cognome",
    "nome",
    "matricola",
    "ditta",
    "tipologia",
    "tessere",
    "telefono",
    "email",
    "referente",
    "note",
    "accesso",
    "inserimento",
    "modifica",
    "stato",
)
# Sempre in vista: si spostano ma non si tolgono
PERSON_COLUMNS_REQUIRED = ("cognome", "nome")
# Le predefinite, come l'elenco della 0.28.3
PERSON_COLUMNS_DEFAULT = ("foto", "cognome", "nome", "matricola", "ditta", "tipologia", "tessere", "accesso", "stato")
# Dati mandati e salvati per utente, al massimo (JSON): la scelta più lunga possibile ne usa circa 150 byte
PREFERENCES_MAX = 4096
# Permesso che serve per leggere e cambiare ogni preferenza
PREFERENCE_PERMISSIONS = {"person_columns": "persone"}

ColumnKey = Annotated[str, StringConstraints(max_length=30)]


class PreferencesIn(BaseModel):
    """Preferenze da cambiare: solo quelle mandate; null torna alla scelta predefinita."""

    model_config = ConfigDict(extra="forbid")
    person_columns: list[ColumnKey] | None = Field(default=None, max_length=len(PERSON_COLUMNS))


class PreferenceError(ValueError):
    """Scelta non accettata, con il motivo per l'utente."""


def check_person_columns(columns: list[str]) -> list[str]:
    """Scelta mandata dalla pagina: solo colonne conosciute, ognuna una volta, con Cognome e Nome."""
    for key in columns:
        if key not in PERSON_COLUMNS:
            raise PreferenceError(f"colonna sconosciuta: {key}")
    repeated = next((key for i, key in enumerate(columns) if key in columns[:i]), None)
    if repeated:
        raise PreferenceError(f"colonna ripetuta: {repeated}")
    if not all(key in columns for key in PERSON_COLUMNS_REQUIRED):
        raise PreferenceError("Cognome e Nome restano sempre nell'elenco")
    return list(columns)


def clean_person_columns(value: Any) -> list[str] | None:
    """Scelta salvata, pulita senza errori: le colonne che questa versione non conosce (salvate da una versione futura)
    e i doppioni si tolgono, Cognome e Nome si rimettono se mancano. None (le predefinite) se non è una lista."""
    if not isinstance(value, list):
        return None
    columns: list[str] = []
    for key in value:
        if isinstance(key, str) and key in PERSON_COLUMNS and key not in columns:
            columns.append(key)
    # Cognome e Nome sempre, uno accanto all'altro se ne manca uno; tutti e due all'inizio, dopo la foto
    if "cognome" not in columns and "nome" not in columns:
        at = 1 if columns[:1] == ["foto"] else 0
        columns[at:at] = list(PERSON_COLUMNS_REQUIRED)
    elif "cognome" not in columns:
        columns.insert(columns.index("nome"), "cognome")
    elif "nome" not in columns:
        columns.insert(columns.index("cognome") + 1, "nome")
    return columns


def read_saved(text: str) -> dict:
    """Preferenze salvate di un utente; {} se non ce ne sono o se non si leggono."""
    try:
        saved = json.loads(text) if text else {}
    except ValueError:
        return {}
    return saved if isinstance(saved, dict) else {}


def visible(saved: dict, can: Any) -> dict:
    """Preferenze da mandare alla pagina, pulite, solo quelle che l'utente può usare (can: AppUser.can)."""
    result: dict[str, Any] = {}
    if can(PREFERENCE_PERMISSIONS["person_columns"]):
        result["person_columns"] = clean_person_columns(saved.get("person_columns"))
    return result


def updated(saved: dict, body: PreferencesIn) -> dict:
    """Preferenze salvate con le modifiche mandate. Quelle che questa versione non conosce (di una versione futura)
    restano, se ci stanno."""
    result = dict(saved)
    if "person_columns" in body.model_fields_set:
        if body.person_columns is None or list(body.person_columns) == list(PERSON_COLUMNS_DEFAULT):
            # Le predefinite: se cambiano in una versione futura, chi non ha scelto niente segue le nuove
            result.pop("person_columns", None)
        else:
            result["person_columns"] = check_person_columns(body.person_columns)
    if len(json.dumps(result)) > PREFERENCES_MAX:
        result = {key: value for key, value in result.items() if key in PREFERENCE_PERMISSIONS}
    return result
