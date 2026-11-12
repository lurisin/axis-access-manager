"""Confronto in ombra: lo stato voluto dall'app confrontato con il controller mentre l'app gira, senza scrivere niente.

Utenti e tessere si confrontano dopo ogni sincronizzazione, con la sua stessa lettura; la programmazione ogni ora e a
richiesta (scheda Confronto del controller). Per ogni tipo restano i contatori dell'ultimo confronto, e ogni
differenza va nel log con un WARNING, una riga per oggetto, una volta sola finché resta la stessa. Serve a vedere,
prima che l'app scriva da sola (passo 3), se quello che scriverebbe corrisponde al controller.
"""

from __future__ import annotations

import logging

from .compare import KIND_TITLES, KINDS

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
# Tipo letto ma non confrontato: lo stato voluto non è calcolato (presa in carico che non si è potuta fare)
NOT_COMPARED = "non confrontato: presa in carico non calcolata"


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


class Shadow:
    """Risultati del confronto in ombra per il controller in uso (state.shadow)."""

    def __init__(self) -> None:
        # Ora (orologio del ciclo) del prossimo confronto della programmazione
        self.next_full = 0.0
        self._reset(None)

    def _reset(self, controller_id: int | None) -> None:
        self.controller_id = controller_id
        # Tipo → contatori dell'ultimo confronto: uguali, prese, differenze, sorprese, non letto (il motivo) e ora
        self.kinds: dict[str, dict] = {}
        # Giro → ora ed esito dell'ultimo confronto
        self.rounds: dict[str, dict] = {}
        # Chiave della riga → (tipo, impronta) delle differenze già scritte nel log
        self._logged: dict[str, tuple[str, tuple]] = {}

    def record(self, controller_id: int, kinds, result: dict, at: str, round_name: str) -> None:
        """Registra il confronto dei tipi kinds (compare.compare) e scrive nel log le differenze nuove. Un tipo non
        letto, o letto ma non confrontato (compare: pending, la presa in carico non si è potuta calcolare), tiene le
        differenze già scritte: non vuol dire risolto, e se al giro dopo torna la stessa differenza non si riscrive."""
        if controller_id != self.controller_id:
            self._reset(controller_id)
        kinds = list(kinds)
        unread = {row["kind"]: row["blocked"] or "non letto" for row in result.get("unread") or []}
        why = ": ".join([NOT_COMPARED, *(["; ".join(dict.fromkeys(unread.values()))] if unread else [])])
        skipped = {**{kind: why for kind in result.get("pending") or ()}, **unread}
        rows: dict[str, list[dict]] = {}
        for row in result.get("rows") or []:
            rows.setdefault(group_of(row), []).append(row)
        for kind in kinds:
            mine = [] if kind in skipped else rows.get(kind) or []
            self.kinds[kind] = {
                "uguali": sum(1 for r in mine if r["action"] == "uguale"),
                "prese": sum(1 for r in mine if r["action"] == "prende"),
                "differenze": sum(1 for r in mine if r["action"] in DIFFERENCES),
                "sorprese": sum(1 for r in mine if r["surprises"]),
                "non_letto": skipped.get(kind, ""),
                "at": at,
            }
            if kind in skipped:
                continue
            seen = set()
            for row in filter(flagged, mine):
                seen.add(row["key"])
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
        self.rounds[round_name] = {"at": at, "kinds": kinds, "error": skipped[kinds[0]] if failed else ""}

    def as_dict(self) -> dict:
        return {
            "rounds": self.rounds,
            "kinds": [{"kind": k, "title": KIND_TITLES[k], **self.kinds[k]} for k in KINDS if k in self.kinds],
        }
