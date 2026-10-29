"""Utenti dell'app e permessi: chi può vedere e chi può modificare."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

from .db import Database

# Permesso → descrizione mostrata nella pagina Utenti
PERMISSIONS = {
    "stato": "Vedere cruscotto, porte e notifiche",
    "persone": "Vedere persone, tessere e tipologie",
    "persone_modifica": "Registrare, modificare, sospendere ed eliminare persone e tessere",
    "registro": "Vedere il registro accessi e le foto",
    "regole": "Vedere fasce orarie e festività",
    "regole_modifica": "Modificare tipologie, fasce orarie e festività",
    "import_export": "Esportare e importare file CSV",
    "impianti": "Configurare controller, porte, videocitofoni e telecamere e programmare il controller",
    "backup": "Vedere, creare, bloccare e confrontare i backup (sbloccare, ripristinare ed eliminare restano agli "
    "amministratori)",
    "utenti": "Gestire gli utenti dell'app e vedere le attività (amministratore)",
}
# Nome breve di ogni permesso, per Registro Attività e confronto dei backup
PERMISSION_NAMES = {
    "stato": "Stato",
    "persone": "Persone",
    "persone_modifica": "Modifica Persone",
    "registro": "Registro",
    "regole": "Regole",
    "regole_modifica": "Modifica Regole",
    "import_export": "Importa ed Esporta",
    "impianti": "Impianti",
    "backup": "Backup",
    "utenti": "Utenti",
}

# Permessi che ne richiedono altri per avere senso
IMPLIED = {
    "persone_modifica": ["persone"],
    "regole_modifica": ["regole"],
}

# Ogni permesso mostra le sue pagine nel menu: senza "stato" spariscono cruscotto, porte e notifiche
ROLES = {
    "amministratore": ("Amministratore", list(PERMISSIONS)),
    "operatore": ("Operatore", ["stato", "persone", "persone_modifica", "registro", "regole", "import_export"]),
    "tessere": ("Tessere", ["persone", "persone_modifica", "regole"]),
    "consultazione": ("Consultazione", ["stato", "persone", "registro", "regole"]),
    "solo_registro": ("Solo Registro", ["registro"]),
    "solo_stato": ("Solo Stato", ["stato"]),
    "nessun_accesso": ("Nessun Accesso", []),
}

ADMIN = "utenti"
LOCAL_USER = "locale"
SETTING_STATUS_ADDED = "perm_stato_added"
# Id dell'utente di Home Assistant → nome utente nell'app: i permessi seguono la persona, non il nome utente
SETTING_USER_IDS = "app_user_ids"
# Nome utente nell'app di chi entra senza nome utente di Home Assistant (login LDAP, OIDC...)
ID_PREFIX = "id:"


class UserError(Exception):
    """Operazione sugli utenti non consentita."""


@dataclass
class AppUser:
    username: str
    name: str
    perms: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return self.name or self.username

    def can(self, *perms: str) -> bool:
        return ADMIN in self.perms or all(p in self.perms for p in perms)


def normalize_perms(perms: list[str]) -> list[str]:
    result = {p for p in perms if p in PERMISSIONS}
    if ADMIN in result:
        return list(PERMISSIONS)
    for perm in list(result):
        result.update(IMPLIED.get(perm, []))
    return [p for p in PERMISSIONS if p in result]


def role_of(perms: list[str]) -> str:
    current = set(normalize_perms(perms))
    for key, (_, role_perms) in ROLES.items():
        if set(role_perms) == current:
            return key
    return "personalizzato"


class UserDirectory:
    """Riconosce l'utente di Home Assistant che apre la pagina e ne restituisce i permessi."""

    def __init__(self, db: Database, readonly_users: tuple[str, ...] = ()) -> None:
        self.db = db
        self.readonly_users = readonly_users
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def resolve(self, username: str, display_name: str, user_id: str = "") -> AppUser:
        """Utente registrato; chi entra per la prima volta viene aggiunto.

        Il primo utente in assoluto diventa amministratore, gli altri partono con "Solo Stato"
        (o "Consultazione" se elencati nell'opzione readonly_users). Con l'id di Home Assistant (user_id) l'utente
        si riconosce anche se cambia nome utente, o se Home Assistant non lo manda.
        """
        username = username.strip().lower()
        user_id = user_id.strip()
        if not username and not user_id:
            # Senza intestazioni di Ingress (sviluppo, vecchio Supervisor): accesso completo come prima della 0.4
            return AppUser(LOCAL_USER, "Accesso locale", set(PERMISSIONS))
        with self._lock:
            if user_id:
                username = self._follow_id(user_id, username)
            row = self.db.get_app_user(username)
            if row is None:
                if not self._has_admin():
                    perms = ROLES["amministratore"][1]
                elif username in self.readonly_users:
                    perms = ROLES["consultazione"][1]
                else:
                    perms = ROLES["solo_stato"][1]
                self.db.save_app_user(username, display_name, perms)
                self.db.add_audit(display_name or username, "utente aggiunto al primo accesso", role_label(perms))
                row = self.db.get_app_user(username)
            self._touch(username, display_name, row)
        return AppUser(row["username"], row["name"] or display_name, set(row["perms"]))

    def add_status_permission(self) -> int:
        """Una volta sola: "stato" agli utenti creati prima della 0.11, che vedevano sempre cruscotto e porte."""
        if self.db.get_setting(SETTING_STATUS_ADDED):
            return 0
        changed = 0
        with self._lock:
            for row in self.db.list_app_users():
                if "stato" not in row["perms"]:
                    self.db.save_app_user(row["username"], row["name"], normalize_perms(row["perms"] + ["stato"]))
                    changed += 1
            self.db.set_setting(SETTING_STATUS_ADDED, "1")
        if changed:
            self.db.add_audit(
                "sistema",
                "permesso Stato aggiunto agli utenti esistenti",
                f"{changed} utent{'e' if changed == 1 else 'i'}",
            )
        return changed

    def _follow_id(self, user_id: str, username: str) -> str:
        """Nome utente nell'app della persona di Home Assistant con questo id; tiene aggiornato il legame."""
        saved = self._ids()
        ids = dict(saved)
        bound = ids.get(user_id, "")
        if bound and self.db.get_app_user(bound) is None:
            bound = ""  # eliminato dall'app: chi torna riparte da capo
        if not username:
            # Home Assistant manda il nome utente solo per i suoi account, non per LDAP o OIDC: resta l'id
            username = bound or f"{ID_PREFIX}{user_id}"
        elif bound != username:
            owner = next((i for i, name in ids.items() if name == username and i != user_id), "")
            row = self.db.get_app_user(username)
            if owner and row is not None and f"{ID_PREFIX}{owner}" != username:
                if ADMIN in row["perms"] and not self._reachable_admins(username):
                    # Unico amministratore (account di Home Assistant ricreato, Home Assistant reinstallato): messo da
                    # parte sotto il vecchio id nessuno potrebbe più ridare i permessi, quindi resta al nome utente.
                    # Una copia resta anche al vecchio id: se l'account era stato solo rinominato e il nome riusato
                    # da un altro, chi era amministratore lo ritrova al prossimo accesso
                    self.db.save_app_user(f"{ID_PREFIX}{owner}", row["name"], row["perms"])
                    ids[owner] = f"{ID_PREFIX}{owner}"
                    self.db.add_audit(
                        "sistema",
                        "unico amministratore passato a un nuovo account di Home Assistant",
                        f"{row['name'] or username}: i permessi restano al nome utente {username} e anche a "
                        f"{ID_PREFIX}{owner}, per l'account di prima",
                    )
                else:
                    # Il nome utente era di un'altra persona di Home Assistant: i permessi restano suoi
                    self._move(
                        username, f"{ID_PREFIX}{owner}", "nome utente passato a un altro utente di Home Assistant"
                    )
                    ids[owner] = f"{ID_PREFIX}{owner}"
            if bound and self.db.get_app_user(username) is None:
                # Nome utente cambiato in Home Assistant: i permessi seguono la persona
                self._move(bound, username, "nome utente cambiato in Home Assistant")
        ids = {i: name for i, name in ids.items() if name != username}
        ids[user_id] = username
        if ids != saved:
            self.db.set_setting(SETTING_USER_IDS, json.dumps(ids))
        return username

    def _ids(self) -> dict[str, str]:
        try:
            ids = json.loads(self.db.get_setting(SETTING_USER_IDS) or "{}")
        except ValueError:
            return {}
        return ids if isinstance(ids, dict) else {}

    def _unbind(self, username: str) -> None:
        """Il nome utente non è più legato a nessuna persona di Home Assistant: il prossimo che entra lo prende."""
        ids = self._ids()
        if username in ids.values():
            self.db.set_setting(SETTING_USER_IDS, json.dumps({i: n for i, n in ids.items() if n != username}))

    def _move(self, old: str, new: str, reason: str) -> None:
        row = self.db.get_app_user(old)
        self.db.save_app_user(new, row["name"], row["perms"])
        self.db.delete_app_user(old)
        self.db.add_audit("sistema", reason, f"{row['name'] or old}: {old} → {new}")

    def _touch(self, username: str, display_name: str, row: dict) -> None:
        # Al massimo una scrittura al minuto per utente
        now = time.monotonic()
        if now - self._seen.get(username, -120) < 60 and row["name"]:
            return
        self._seen[username] = now
        # Il nome scelto nella pagina Utenti resta: quello di Home Assistant serve solo se manca
        self.db.touch_app_user(username, "" if row["name"] else display_name)

    def _has_admin(self) -> bool:
        return any(ADMIN in u["perms"] for u in self.db.list_app_users())

    def list(self) -> list[dict]:
        result = []
        for row in self.db.list_app_users():
            perms = normalize_perms(row["perms"])
            result.append({**row, "perms": perms, "role": role_of(perms), "admin": ADMIN in perms})
        return result

    def save(self, username: str, name: str, perms: list[str], by: AppUser, create: bool) -> dict:
        username = username.strip().lower()
        if not username or len(username) > 100 or any(c.isspace() for c in username):
            raise UserError("nome utente non valido: usa il nome utente di Home Assistant, senza spazi")
        name = " ".join(name.split())[:100]
        perms = normalize_perms(perms)
        with self._lock:
            existing = self.db.get_app_user(username)
            if create and existing:
                raise UserError(f"l'utente {username} esiste già")
            if not create and not existing:
                raise UserError("utente non trovato")
            if existing and ADMIN in existing["perms"] and ADMIN not in perms and not self._reachable_admins(username):
                raise UserError("serve almeno un amministratore: dai prima i permessi completi a un altro utente")
            if create:
                # Registrato prima del suo primo accesso: lo prende chi entra con questo nome utente
                self._unbind(username)
            self.db.save_app_user(username, name or (existing or {}).get("name", ""), perms)
        action = "utente creato" if create else "utente modificato"
        saved = self.db.get_app_user(username)
        self.db.add_audit(by.label, action, f"{saved['name'] or username}: {role_label(perms)}")
        return saved

    def delete(self, username: str, by: AppUser) -> None:
        username = username.strip().lower()
        with self._lock:
            existing = self.db.get_app_user(username)
            if not existing:
                raise UserError("utente non trovato")
            if username == by.username:
                raise UserError("non puoi eliminare il tuo utente")
            if ADMIN in existing["perms"] and not self._reachable_admins(username):
                raise UserError("è l'unico amministratore")
            self.db.delete_app_user(username)
            self._unbind(username)
        self.db.add_audit(by.label, "utente eliminato", existing["name"] or username)

    def _reachable_admins(self, excluding: str) -> int:
        """Amministratori diversi da excluding che qualcuno può ancora usare: non conta chi è stato messo da parte
        sotto l'id di una persona di Home Assistant (id:…) e da allora non è più entrato."""
        return sum(
            1
            for u in self.db.list_app_users()
            if ADMIN in u["perms"]
            and u["username"] != excluding
            and not (u["username"].startswith(ID_PREFIX) and not u["last_seen"])
        )


def role_label(perms: list[str]) -> str:
    role = role_of(perms)
    if role in ROLES:
        return ROLES[role][0]
    return "Personalizzato: " + ", ".join(PERMISSION_NAMES[p] for p in normalize_perms(perms))
