"""Confronto in ombra: lo stato voluto dall'app confrontato con il controller mentre l'app gira, senza scrivere niente
sul controller.

Utenti e tessere si confrontano dopo ogni sincronizzazione, con la sua stessa lettura; la programmazione ogni ora e a
richiesta (scheda Confronto del controller). Per ogni tipo restano i contatori dell'ultimo confronto, e ogni
differenza va nel log con un WARNING, una riga per oggetto, una volta sola finché resta la stessa. Serve a vedere,
prima che l'app scriva da sola (passo 3), se quello che scriverebbe corrisponde al controller.

Ogni giro scrive anche una riga INFO nel log (giro, esito e conteggi per tipo) e si somma nello storico del database
(tabella shadow_days, una scrittura per giro): per ogni giorno dell'impianto e giro quanti giri, quanti senza
differenze, con differenze, sorprese, elenchi non letti o non riusciti, e l'ultima differenza. I contatori dell'ultimo
giro restano in memoria e si perdono a ogni riavvio; lo storico no (collaudo 0.28.1: alla fine della settimana di
osservazione non restava prova dei giri fatti).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta

from .compare import KIND_TITLES, KINDS
from .db import Database
from .health import _zone, ha_time_zone
from .service import plural

_LOGGER = logging.getLogger(__name__)

# Azioni del confronto che scriverebbero o toglierebbero qualcosa sul controller
DIFFERENCES = frozenset({"riporta", "ricrea", "togli"})
# Righe che non sono di un tipo del confronto: gli sblocchi programmati vengono dalla configurazione delle porte
GROUPS = {"sblocco": "config_porta"}
# Le azioni al condizionale: nella 0.28 l'app non le fa, dice soltanto cosa farebbe
WOULD = (
    ("Prende in carico", "Prenderebbe in carico"),
    ("Riporta", "Riporterebbe"),
    ("Ricrea", "Ricreerebbe"),
    ("Togli", "Toglierebbe"),
)
# Giri del confronto in ombra: utenti e tessere dopo ogni sincronizzazione, la programmazione ogni ora e a richiesta
# (Confronta Ora nella scheda Confronto), con l'ora di ognuno per conto suo
CARDS_ROUND = "utenti_tessere"
PROGRAMMING_ROUND = "programmazione"
MANUAL_ROUND = "confronta_ora"
# Il giro nella riga del log
ROUND_TITLES = {
    CARDS_ROUND: "di utenti e tessere dopo la sincronizzazione",
    PROGRAMMING_ROUND: "della programmazione di ogni ora",
    MANUAL_ROUND: "della programmazione con Confronta Ora",
}
# Tipi di utenti e tessere: nello storico la loro differenza (nomi, numeri di tessera) solo con il permesso Persone
CARD_KINDS = ("utente", "tessera")
# Tipo letto ma non confrontato: lo stato voluto non è calcolato (presa in carico che non si è potuta fare)
NOT_COMPARED = "non confrontato: presa in carico non calcolata"
# Storico dei giri nel database: gli ultimi HISTORY_DAYS giorni dell'impianto
HISTORY_DAYS = 30
# Lunghezza massima nello storico dell'ultima differenza e dell'ultimo errore
TEXT_MAX = 300
# Contatori dello storico (vedi db.SHADOW_FIELDS): un giro ne aggiunge 1 a rounds e a quelli che lo riguardano
COUNTERS = ("rounds", "clean", "differences", "surprises", "unread", "errors")


def group_of(row: dict) -> str:
    """Tipo sotto cui sta una riga del confronto."""
    return GROUPS.get(row["kind"], row["kind"])


def would(text: str) -> str:
    """ "Riporta com'è nell'app" → "Riporterebbe com'è nell'app"."""
    for now, then in WOULD:
        if text.startswith(now):
            return then + text[len(now) :]
    return text


def flagged(row: dict) -> bool:
    """Riga da guardare: una differenza, o un oggetto con una sorpresa."""
    return row["action"] in DIFFERENCES or bool(row["surprises"])


def log_line(row: dict) -> str:
    parts = [f"{row['label']}: {would(row['text'])}"]
    parts += [f"{c['label']} {c['old']} → {c['new']}" for c in row["changes"][:8]]
    if len(row["changes"]) > 8:
        parts.append(f"e altri {len(row['changes']) - 8} campi")
    parts += [f"da guardare: {text}" for text in row["surprises"]]
    return " · ".join(parts)


def counts_text(counts: dict) -> str:
    """Conteggi di un tipo in breve: "3 uguali, 1 da prendere in carico", "non letto", "nessun oggetto"."""
    if counts["non_letto"]:
        return "non letto"
    parts = [
        counts["uguali"] and plural(counts["uguali"], "uguale", "uguali"),
        counts["prese"] and f"{counts['prese']} da prendere in carico",
        counts["differenze"] and plural(counts["differenze"], "differenza", "differenze"),
        counts["sorprese"] and plural(counts["sorprese"], "sorpresa", "sorprese"),
    ]
    return ", ".join(part for part in parts if part) or "nessun oggetto"


def round_line(round_name: str, counts: dict[str, dict], error: str = "") -> str:
    """Riga INFO di un giro: il giro, l'esito e i conteggi di ogni tipo in breve. error: il giro non ha letto o
    confrontato niente, e i conteggi non dicono niente."""
    head = f"Confronto in ombra, giro {ROUND_TITLES[round_name]}"
    if error:
        return f"{head}: non riuscito, {error}"
    unread = [c["non_letto"] for c in counts.values() if c["non_letto"]]
    differences = sum(c["differenze"] for c in counts.values())
    surprises = sum(c["sorprese"] for c in counts.values())
    outcome = [
        differences and plural(differences, "differenza", "differenze"),
        surprises and plural(surprises, "sorpresa", "sorprese"),
        unread and f"{plural(len(unread), 'tipo non letto', 'tipi non letti')} ({'; '.join(dict.fromkeys(unread))})",
    ]
    kinds = " · ".join(f"{KIND_TITLES[kind]}: {counts_text(c)}" for kind, c in counts.items())
    return f"{head}: {', '.join(part for part in outcome if part) or 'senza differenze'}. {kinds}"


def short(text: str) -> str:
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def plant_day(moment: str | datetime | None = None) -> str:
    """Giorno dell'impianto (aaaa-mm-gg) di un'ora ISO con il fuso (adesso senza argomenti): nel fuso di Home
    Assistant, come i giorni della pagina; se non lo si conosce, in quello del sistema."""
    if moment is None:
        moment = datetime.now(tz=UTC)
    elif isinstance(moment, str):
        moment = datetime.fromisoformat(moment.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.astimezone()
    zone = _zone(ha_time_zone())
    return (moment.astimezone(zone) if zone else moment.astimezone()).date().isoformat()


def merged(old: dict | None, new: dict) -> dict:
    """Due pezzi dello stesso giorno e giro: contatori sommati, ora, errore e differenza più recenti (come
    db.SHADOW_ROUND)."""
    if old is None:
        return dict(new)
    result = {name: old[name] + new[name] for name in COUNTERS}
    result["last_at"] = new["last_at"]
    result["last_error"] = new["last_error"] or old["last_error"]
    last = new if new["difference"] else old
    result.update({name: last[name] for name in ("difference", "difference_kind", "difference_at")})
    return result


class Shadow:
    """Risultati del confronto in ombra per il controller in uso (state.shadow), con lo storico nel database db."""

    def __init__(self, db: Database | None = None) -> None:
        # Ora (orologio del ciclo) del prossimo confronto della programmazione
        self.next_full = 0.0
        self.db = db
        # Giri non ancora nello storico (disco pieno): (giorno, giro) → contatori, si salvano con un giro dopo
        self._pending: dict[tuple[str, str], dict] = {}
        # Ultimo salvataggio dello storico non riuscito: nel log una volta, non a ogni giro
        self._save_failed = False
        # Giorno dell'ultima pulizia dello storico: una al giorno basta
        self._pruned = ""
        self._reset(None)

    def _reset(self, controller_id: int | None) -> None:
        self.controller_id = controller_id
        # Tipo → contatori dell'ultimo confronto: uguali, prese, differenze, sorprese, non letto (il motivo), ora e giro
        self.kinds: dict[str, dict] = {}
        # Giro → ora ed esito dell'ultimo confronto
        self.rounds: dict[str, dict] = {}
        # Chiave della riga → (tipo, impronta) delle differenze già scritte nel log
        self._logged: dict[str, tuple[str, tuple]] = {}

    def record(self, controller_id: int, kinds, result: dict, at: str, round_name: str) -> None:
        """Registra il confronto dei tipi kinds (compare.compare) e scrive nel log le differenze nuove. Un tipo non
        letto, o letto ma non confrontato (compare: pending, la presa in carico non si è potuta calcolare), tiene le
        differenze già scritte: non vuol dire risolto, e se al giro dopo torna la stessa differenza non si riscrive.
        Il giro va nel log (una riga INFO) e nello storico."""
        if controller_id != self.controller_id:
            self._reset(controller_id)
        kinds = list(kinds)
        unread = {row["kind"]: row["blocked"] or "non letto" for row in result.get("unread") or []}
        why = ": ".join([NOT_COMPARED, *(["; ".join(dict.fromkeys(unread.values()))] if unread else [])])
        skipped = {**{kind: why for kind in result.get("pending") or ()}, **unread}
        rows: dict[str, list[dict]] = {}
        for row in result.get("rows") or []:
            rows.setdefault(group_of(row), []).append(row)
        found: list[tuple[str, dict]] = []
        for kind in kinds:
            mine = [] if kind in skipped else rows.get(kind) or []
            self.kinds[kind] = {
                "uguali": sum(1 for r in mine if r["action"] == "uguale"),
                "prese": sum(1 for r in mine if r["action"] == "prende"),
                "differenze": sum(1 for r in mine if r["action"] in DIFFERENCES),
                "sorprese": sum(1 for r in mine if r["surprises"]),
                "non_letto": skipped.get(kind, ""),
                "at": at,
                "round": round_name,
            }
            if kind in skipped:
                continue
            seen = set()
            for row in filter(flagged, mine):
                seen.add(row["key"])
                found.append((kind, row))
                mark = (
                    row["action"],
                    tuple((c["label"], c["old"], c["new"]) for c in row["changes"]),
                    tuple(row["surprises"]),
                )
                if self._logged.get(row["key"]) != (kind, mark):
                    self._logged[row["key"]] = (kind, mark)
                    _LOGGER.warning("Confronto in ombra: %s", log_line(row))
            for key in [k for k, (owner, _) in self._logged.items() if owner == kind and k not in seen]:
                del self._logged[key]
        # Niente letto o niente confrontato: il motivo vale per tutto il giro
        failed = bool(kinds) and all(kind in skipped for kind in kinds)
        error = skipped[kinds[0]] if failed else ""
        self.rounds[round_name] = {"at": at, "kinds": kinds, "error": error}
        counts = {kind: self.kinds[kind] for kind in kinds}
        _LOGGER.info("%s", round_line(round_name, counts, error))
        differences = any(c["differenze"] for c in counts.values())
        surprises = any(c["sorprese"] for c in counts.values())
        partial = not failed and any(kind in skipped for kind in kinds)
        first = found[0] if found else None
        difference = ""
        if first:
            more = f" (e altre {len(found) - 1} righe)" if len(found) > 2 else " (e un'altra riga)" if found[1:] else ""
            difference = short(log_line(first[1]) + more)
        self._save(
            round_name,
            at,
            {
                "rounds": 1,
                "clean": int(not (differences or surprises or partial or failed)),
                "differences": int(differences),
                "surprises": int(surprises),
                "unread": int(partial),
                "errors": int(failed),
                "last_at": at,
                "last_error": short(error),
                "difference": difference,
                "difference_kind": first[0] if first else "",
                "difference_at": at if first else "",
            },
        )

    def fail(self, controller_id: int, round_name: str, at: str, err: BaseException) -> None:
        """Giro non riuscito per un errore (database, un profilo che l'app non accetta): resta nella scheda e nello
        storico, e nel log ha la sua riga come gli altri giri (i dettagli dell'errore li scrive chi ha chiamato)."""
        if controller_id != self.controller_id:
            self._reset(controller_id)
        error = short(str(err) or type(err).__name__)
        self.rounds[round_name] = {"at": at, "kinds": [], "error": error}
        _LOGGER.info("%s", round_line(round_name, {}, error))
        self._save(
            round_name,
            at,
            {
                **dict.fromkeys(COUNTERS, 0),
                "rounds": 1,
                "errors": 1,
                "last_at": at,
                "last_error": error,
                "difference": "",
                "difference_kind": "",
                "difference_at": "",
            },
        )

    def _save(self, round_name: str, at: str, values: dict) -> None:
        """Il giro nello storico del database, con i giri rimasti indietro: una scrittura sola. Con il disco pieno
        restano in memoria e si salvano con un giro dopo; il confronto va avanti lo stesso."""
        if self.db is None:
            return
        day = plant_day(at)
        keep_from = (date.fromisoformat(day) - timedelta(days=HISTORY_DAYS - 1)).isoformat()
        self._pending[day, round_name] = merged(self._pending.get((day, round_name)), values)
        # Giorni già fuori dallo storico (disco pieno per settimane): non servono più
        self._pending = {key: v for key, v in self._pending.items() if key[0] >= keep_from}
        try:
            self.db.add_shadow_rounds(
                [(d, r, v) for (d, r), v in self._pending.items()], keep_from if self._pruned != day else ""
            )
        except sqlite3.Error as err:
            if not self._save_failed:
                _LOGGER.warning("Storico del confronto in ombra non salvato, si riprova al giro dopo: %s", err)
            self._save_failed = True
            return
        if self._save_failed:
            _LOGGER.info("Storico del confronto in ombra di nuovo salvato")
            self._save_failed = False
        self._pending.clear()
        self._pruned = day

    def history(self, days: int = HISTORY_DAYS, hidden: str = "") -> dict:
        """Storico degli ultimi days giorni dell'impianto, dal più recente, con i giri non ancora salvati. Per ogni
        giorno e giro i contatori (None se nessun giro). hidden: il motivo per cui l'ultima differenza di utenti e
        tessere non si mostra (manca il permesso di vedere le persone), vuoto se si mostra."""
        today = date.fromisoformat(plant_day())
        wanted = [(today - timedelta(days=n)).isoformat() for n in range(days)]
        found = {(row["day"], row["round"]): row for row in self.db.shadow_days(wanted[-1])} if self.db else {}
        for key, values in self._pending.items():
            found[key] = merged(found.get(key), values)
        return {
            "today": wanted[0],
            "time_zone": ha_time_zone(),
            "days": [
                {
                    "day": day,
                    "giri": {
                        name: self._day_view(found.get((day, name)), hidden)
                        for name in (PROGRAMMING_ROUND, CARDS_ROUND, MANUAL_ROUND)
                    },
                }
                for day in wanted
            ],
            # Giri che il disco pieno non lascia salvare: si perdono con un riavvio
            "unsaved": sum(values["rounds"] for values in self._pending.values()),
        }

    @staticmethod
    def _day_view(row: dict | None, hidden: str) -> dict | None:
        if row is None:
            return None
        secret = bool(hidden) and row["difference_kind"] in CARD_KINDS
        return {
            "giri": row["rounds"],
            "senza_differenze": row["clean"],
            "con_differenze": row["differences"],
            "con_sorprese": row["surprises"],
            "con_non_letti": row["unread"],
            "non_riusciti": row["errors"],
            "ultimo": row["last_at"],
            "ultimo_errore": row["last_error"],
            "ultima_differenza": "" if secret else row["difference"],
            "ultima_differenza_at": row["difference_at"],
            "ultima_differenza_nascosta": hidden if secret else "",
        }

    def as_dict(self) -> dict:
        return {
            "rounds": self.rounds,
            "kinds": [{"kind": k, "title": KIND_TITLES[k], **self.kinds[k]} for k in KINDS if k in self.kinds],
        }
