"""Testi senza Home Assistant per chi entra dall'accesso diretto senza i permessi tecnici.

Il personale che usa l'accesso diretto (reception, pulizie, manutenzione) non deve nemmeno sapere che dietro l'app c'è
Home Assistant: senza i permessi Impianti o Backup non legge mai «Home Assistant», «MQTT», «broker», «add-on» e simili,
né nelle pagine né nelle notifiche né negli errori. Resta solo il registro, dove Home Assistant è chi ha aperto la porta
a distanza. Lo decide il server per ogni richiesta (main.hides_home_assistant), mai il browser; da Home Assistant e per
chi ha i permessi tecnici i testi restano quelli di sempre.
"""

from __future__ import annotations

import re

# Permessi tecnici: chi ne ha uno configura l'impianto o i backup, e legge i testi di sempre
TECHNICAL = ("impianti", "backup")
# Parole che chi entra così non deve leggere. «HA» solo maiuscolo («ha» è il verbo), «ingress» non «ingresso»
FORBIDDEN = re.compile(
    r"home[ -]?assistant|(?-i:\bHA\b)|mqtt|broker|mosquitto|\badd-?on\b|supervisor|\bingress\b|\bintegrazion[ei]\b",
    re.IGNORECASE,
)
# Soluzione tecnica: per loro c'è l'amministratore
ASK_ADMIN = "avvisa l'amministratore dell'impianto"
# Errore che anche così parlerebbe di Home Assistant (un testo nuovo, non previsto qui sotto)
NEUTRAL_ERROR = f"operazione non riuscita: {ASK_ADMIN}"
# Frasi dei messaggi del server che possono arrivare anche a loro, riscritte: errori imprevisti, telecamere (le foto
# passano da Home Assistant)
PHRASES = (
    (re.compile(r"\b([Ee])rrore imprevisto[,:] dettagli nel log dell'add-on"), rf"\1rrore imprevisto: {ASK_ADMIN}"),
    (re.compile(r"Home Assistant non risponde"), "telecamera non raggiungibile"),
    (re.compile(r" \(risposta (\d+) di Home Assistant\)"), r" (risposta \1)"),
)


def technical(user) -> bool:
    """L'utente ha un permesso tecnico (l'amministratore li ha tutti)."""
    return any(user.can(perm) for perm in TECHNICAL)


def forbidden(text: str) -> bool:
    return bool(FORBIDDEN.search(text or ""))


def plain_text(text: str, fallback: str = NEUTRAL_ERROR) -> str:
    """Il testo senza Home Assistant: le frasi note riscritte; se ne resta una parola, fallback al posto di tutto."""
    for pattern, replacement in PHRASES:
        text = pattern.sub(replacement, text)
    return fallback if forbidden(text) else text


def plain_source(source: str) -> str:
    """Chi ha provato ad aprire a distanza («Luca (Home Assistant)»), per le notifiche: nel registro resta com'è."""
    rest = source.replace("(Home Assistant)", "(apertura remota)")
    return "Apertura Remota" if forbidden(rest) else rest
