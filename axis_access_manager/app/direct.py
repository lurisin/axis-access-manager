"""Accesso diretto all'app, fuori da Home Assistant: per il personale (reception, pulizie, manutenzione) da un PC della
rete interna, con nome utente e password dell'app.

Un secondo server nello stesso processo, sulla porta DIRECT_PORT del container (in Home Assistant la si assegna nella
scheda Rete dell'add-on), serve la stessa app. Le sue richieste sono segnate (scope DIRECT_SCOPE): per loro non vale
l'utente di Home Assistant (le intestazioni di Ingress si tolgono) ma solo la sessione aperta con il login.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from .db import Database

_LOGGER = logging.getLogger(__name__)

DIRECT_PORT = 8100
DIRECT_SCOPE = "aam_direct"
SETTING = "direct_access"
DEFAULTS = {"enabled": False, "https": False, "certfile": "fullchain.pem", "keyfile": "privkey.pem"}
# Certificati di Home Assistant (map: ssl:ro)
SSL_DIR = Path("/ssl")
# Intestazione che la pagina manda con ogni modifica: un altro sito non può aggiungerla senza un preflight CORS, a cui
# l'app non risponde
REQUEST_HEADER = "X-AAM-Request"
# Intestazioni di Ingress: sull'accesso diretto non devono mai arrivare all'app
INGRESS_HEADERS = (b"x-remote-user-name", b"x-remote-user-display-name", b"x-remote-user-id", b"x-ingress-path")
# Rete interna di Home Assistant: un client con questo indirizzo è il gateway Docker, non il PC vero
HASSIO_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# Sessione: resta aperta su quel computer finché non si esce o la chiude un amministratore. Si chiude da sola solo se
# non viene usata per SESSION_UNUSED. Il cookie scade dopo COOKIE_MAX_AGE (Chrome non accetta di più) e si rinnova
SESSION_UNUSED = timedelta(days=90)
COOKIE_MAX_AGE = 400 * 24 * 3600
TOUCH_EVERY = timedelta(hours=1)
# Con la password provvisoria si può solo cambiarla, entro un'ora dal login
MUST_CHANGE_SESSION = timedelta(hours=1)
# La password provvisoria vale una settimana: poi l'amministratore ne crea un'altra
TEMPORARY_VALID = timedelta(days=7)
COOKIE = "aam_session"
COOKIE_HTTPS = "__Host-aam_session"

# Password: scrypt con N=2^14, r=8, p=5 (16 MiB, costo pari ai parametri minimi OWASP), sale di 16 byte
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 5
SCRYPT_MAXMEM = 64 * 1024 * 1024
PASSWORD_MIN = 10
PASSWORD_MAX = 128
COMMON_PASSWORDS = {
    "1234567890",
    "0123456789",
    "1234512345",
    "1111111111",
    "0000000000",
    "qwertyuiop",
    "password12",
    "password123",
    "password1234",
    "passwordpassword",
    "abcdefghij",
    "abcd123456",
    "abc1234567",
    "iloveyou12",
    "qwerty1234",
    "1q2w3e4r5t",
    "q1w2e3r4t5",
    "asdfghjkl1",
    "hotel12345",
    "reception1",
    "reception123",
    "benvenuto1",
    "benvenuto123",
    "cambiami123",
}
# Password provvisorie: 12 caratteri senza quelli che si confondono (0/o, 1/l/i), a gruppi di 4
TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# Tentativi sbagliati: (indirizzo, utente) 5 in 15 minuti; indirizzo 20 in 15 minuti; utente da qualunque indirizzo
# 30 in un'ora. Poi 15 minuti di blocco. Il tentativo conta prima del controllo della password: anche quelli in
# parallelo
LIMITS = (("pair", 5, 15 * 60), ("ip", 20, 15 * 60), ("user", 30, 3600))
BLOCK_SECONDS = 15 * 60
# Calcoli della password in parallelo e in coda: oltre, la richiesta si rifiuta subito
HASH_WORKERS = 2
HASH_QUEUE = 8


class DirectError(Exception):
    """Richiesta dell'accesso diretto non valida: messaggio per l'utente e codice HTTP."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------- Password ----------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_problem(password: str, username: str) -> str:
    """Perché la nuova password non va bene; vuoto se va bene."""
    if len(password) < PASSWORD_MIN:
        return f"la password deve avere almeno {PASSWORD_MIN} caratteri"
    if len(password) > PASSWORD_MAX:
        return f"la password può avere al massimo {PASSWORD_MAX} caratteri"
    simple = password.lower()
    if username and username.lower() in simple:
        return "la password non può contenere il nome utente"
    if simple in COMMON_PASSWORDS or len(set(simple)) < 4:
        return "password troppo facile da indovinare"
    return ""


def temporary_password() -> str:
    chars = "".join(secrets.choice(TEMP_ALPHABET) for _ in range(12))
    return "-".join(chars[i : i + 4] for i in range(0, 12, 4))


def normalize_username(value: str) -> str:
    """Come users.py: senza spazi ai lati e in minuscolo. Vuoto se non è un nome utente valido."""
    username = value.strip().lower()
    if not username or len(username) > 100 or any(c.isspace() or ord(c) < 32 for c in username):
        return ""
    return username


class PasswordHasher:
    """Calcoli scrypt su thread propri, al massimo HASH_WORKERS insieme e HASH_QUEUE in attesa: i tentativi in massa non
    occupano i thread dell'app (database, controller) e oltre la coda si rifiutano subito."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=HASH_WORKERS, thread_name_prefix="password")
        self._waiting = 0
        # Hash di una password a caso: chi non esiste costa quanto chi sbaglia la password
        self._dummy = ""

    async def _run(self, func, *args):
        if self._waiting >= HASH_WORKERS + HASH_QUEUE:
            raise DirectError("troppe richieste di accesso in questo momento: riprova tra poco", 429)
        self._waiting += 1
        try:
            return await asyncio.get_running_loop().run_in_executor(self._pool, func, *args)
        finally:
            self._waiting -= 1

    async def verify(self, password: str, stored: str | None) -> bool:
        if stored is None:
            if not self._dummy:
                self._dummy = await self._run(hash_password, secrets.token_hex(16))
            await self._run(verify_password, password, self._dummy)
            return False
        return await self._run(verify_password, password, stored)

    async def hash(self, password: str) -> str:
        return await self._run(hash_password, password)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# ---------- Tentativi sbagliati ----------


@dataclass
class LoginLimiter:
    """Tentativi sbagliati in memoria (all'avvio dell'add-on si riparte da zero)."""

    failures: dict[tuple, deque] = field(default_factory=dict)
    blocked_until: dict[tuple, float] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic

    @staticmethod
    def _keys(ip: str, username: str) -> list[tuple[tuple, int, int]]:
        keys = []
        for kind, limit, window in LIMITS:
            if kind == "pair":
                keys.append(((kind, ip, username), limit, window))
            elif kind == "ip" and ip:
                keys.append(((kind, ip), limit, window))
            elif kind == "user":
                keys.append(((kind, username), limit, window))
        return keys

    def blocked(self, ip: str, username: str) -> int:
        """Secondi di blocco che restano, 0 se si può provare."""
        now = self.clock()
        left = 0
        for key, _, _ in self._keys(ip, username):
            until = self.blocked_until.get(key, 0)
            if until > now:
                left = max(left, int(until - now) + 1)
            elif key in self.blocked_until:
                del self.blocked_until[key]
        return left

    def attempt(self, ip: str, username: str) -> list[tuple]:
        """Un tentativo, contato prima del controllo della password. Restituisce i blocchi appena scattati."""
        now = self.clock()
        started = []
        for key, limit, window in self._keys(ip, username):
            times = self.failures.setdefault(key, deque())
            while times and times[0] <= now - window:
                times.popleft()
            times.append(now)
            if len(times) >= limit and self.blocked_until.get(key, 0) <= now:
                self.blocked_until[key] = now + BLOCK_SECONDS
                times.clear()
                started.append(key)
        self._prune(now)
        return started

    def success(self, ip: str, username: str) -> None:
        """Password giusta: il tentativo non conta, né quelli sbagliati prima da questo indirizzo."""
        for key, _, _ in self._keys(ip, username):
            times = self.failures.get(key)
            if times:
                if key[0] == "pair":
                    times.clear()
                else:
                    times.pop()

    def unblock(self, username: str) -> None:
        """L'amministratore ha dato una password nuova: l'utente può riprovare subito."""
        for store in (self.failures, self.blocked_until):
            for key in [
                k for k in store if (k[0] == "user" and k[1] == username) or (k[0] == "pair" and k[2] == username)
            ]:
                del store[key]

    def _prune(self, now: float) -> None:
        if len(self.failures) < 5000:
            return
        for key in [k for k, times in self.failures.items() if not times or times[-1] <= now - 3600]:
            del self.failures[key]


def client_ip(request) -> str:
    """Indirizzo del PC; vuoto se è il gateway Docker di Home Assistant (tutti i PC sembrerebbero lo stesso)."""
    host = request.client.host if request.client else ""
    try:
        if ipaddress.ip_address(host) in HASSIO_NETWORK:
            return ""
    except ValueError:
        return ""
    return host


# ---------- Sessioni ----------


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def _parse(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def session_valid(session: dict, password: dict | None, now: datetime) -> bool:
    """Sessione ancora buona: l'utente ha la password con cui è entrato, la sessione è stata usata negli ultimi
    SESSION_UNUSED, e con la password provvisoria (da cambiare) non è più vecchia di MUST_CHANGE_SESSION."""
    if password is None or session.get("password_set_at") != password.get("set_at"):
        return False
    last = _parse(session.get("last_seen") or "")
    created = _parse(session.get("created_at") or "")
    if last is None or created is None or now - last > SESSION_UNUSED:
        return False
    return not (password.get("must_change") and now - created > MUST_CHANGE_SESSION)


def temporary_expired(password: dict, now: datetime) -> bool:
    if not password.get("must_change"):
        return False
    set_at = _parse(password.get("set_at") or "")
    return set_at is None or now - set_at > TEMPORARY_VALID


def short_agent(agent: str) -> str:
    """Browser e sistema in poche parole, per riconoscere il computer nella pagina Utenti."""
    browser = next(
        (
            name
            for key, name in (
                ("Edg/", "Edge"),
                ("OPR/", "Opera"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in agent
        ),
        "",
    )
    system = next(
        (
            name
            for key, name in (
                ("Windows", "Windows"),
                ("Android", "Android"),
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Mac OS", "Mac"),
                ("Linux", "Linux"),
                ("CrOS", "ChromeOS"),
            )
            if key in agent
        ),
        "",
    )
    return " su ".join(x for x in (browser, system) if x) or "Browser"


# ---------- Impostazioni ----------


def load_settings(db: Database) -> dict:
    try:
        saved = json.loads(db.get_setting(SETTING) or "{}")
    except ValueError:
        saved = {}
    settings = dict(DEFAULTS)
    if isinstance(saved, dict):
        settings.update({k: saved[k] for k in DEFAULTS if k in saved and type(saved[k]) is type(DEFAULTS[k])})
    return settings


def cert_name_problem(name: str) -> str:
    """Nome di un file della cartella ssl di Home Assistant: solo il nome, senza percorso."""
    if not name or len(name) > 100 or "/" in name or "\\" in name or name.startswith(".") or "\x00" in name:
        return "nome del file non valido: scrivi solo il nome, come fullchain.pem"
    return ""


# ---------- Server ----------


def mark_direct(app):
    """L'app vista dal server dell'accesso diretto: richieste segnate, senza le intestazioni di Ingress."""

    async def direct_app(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope[DIRECT_SCOPE] = True
            scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k.lower() not in INGRESS_HEADERS]
        await app(scope, receive, send)

    return direct_app


def is_direct(request) -> bool:
    return request.scope.get(DIRECT_SCOPE) is True


class _Server(uvicorn.Server):
    """Secondo server nello stesso processo: i segnali di arresto li gestisce il server principale."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


class DirectServer:
    """Avvia, ferma e riavvia il server dell'accesso diretto secondo le impostazioni."""

    def __init__(self, app, port: int = DIRECT_PORT, ssl_dir: Path = SSL_DIR, log_level: str = "info") -> None:
        self.app = app
        self.port = port
        self.ssl_dir = ssl_dir
        self.log_level = log_level
        self._server: _Server | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self.running = False
        self.https = False
        self.error = ""
        self.since = ""
        # Porta vera (con port=0 nei test la sceglie il sistema)
        self.bound_port = 0

    def status(self) -> dict:
        return {
            "running": self.running,
            "https": self.https,
            "error": self.error,
            "since": self.since or None,
            "port": self.port,
        }

    async def apply(self, settings: dict) -> None:
        """Server acceso con le impostazioni date, o spento. Un errore (porta occupata, certificato) resta in error."""
        async with self._lock:
            await self._stop()
            self.error = ""
            if not settings.get("enabled"):
                return
            try:
                await self._start(settings)
            except Exception as err:  # noqa: BLE001 - l'add-on resta in piedi, l'errore si vede nelle impostazioni
                self.error = _error_text(err)
                _LOGGER.warning("Accesso diretto non avviato: %s", self.error)
                await self._stop()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop()

    async def _start(self, settings: dict) -> None:
        https = bool(settings.get("https"))
        ssl_args = {}
        if https:
            for key in ("certfile", "keyfile"):
                problem = cert_name_problem(settings.get(key) or "")
                if problem:
                    raise DirectError(problem)
            certfile = self.ssl_dir / settings["certfile"]
            keyfile = self.ssl_dir / settings["keyfile"]
            for path in (certfile, keyfile):
                if not path.is_file():
                    raise DirectError(f"{path.name} non trovato nella cartella ssl di Home Assistant")
            ssl_args = {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}
        config = uvicorn.Config(
            mark_direct(self.app),
            lifespan="off",
            proxy_headers=False,
            server_header=False,
            access_log=False,
            log_level=self.log_level,
            timeout_graceful_shutdown=3,
            **ssl_args,
        )
        # Errori del certificato qui, non dentro il server
        config.load()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.port))
        except OSError:
            sock.close()
            raise
        self.bound_port = sock.getsockname()[1]
        server = _Server(config)
        self._server = server
        self._task = asyncio.create_task(self._serve(server, sock), name="accesso-diretto")
        # Avviato davvero (o fallito subito)
        for _ in range(100):
            if server.started or self._task.done():
                break
            await asyncio.sleep(0.02)
        if self._task.done():
            error = self._task.exception() if not self._task.cancelled() else None
            raise error or DirectError("server dell'accesso diretto fermato")
        self.running = True
        self.https = https
        self.since = datetime.now(tz=UTC).isoformat(timespec="seconds")
        _LOGGER.info("Accesso diretto in ascolto sulla porta %s (%s)", self.port, "HTTPS" if https else "HTTP")

    async def _serve(self, server: _Server, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit as err:
            # uvicorn chiude il processo con sys.exit su alcuni errori: qui deve fermarsi solo questo server
            raise DirectError("server dell'accesso diretto fermato") from err
        finally:
            sock.close()
            if self._server is server:
                self.running = False

    async def _stop(self) -> None:
        server, task = self._server, self._task
        self._server, self._task = None, None
        self.running = False
        self.since = ""
        if server is None or task is None:
            return
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except TimeoutError:
            server.force_exit = True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        except Exception:  # noqa: BLE001 - già fermato con un errore
            _LOGGER.debug("Server dell'accesso diretto fermato con un errore", exc_info=True)


def _error_text(err: BaseException) -> str:
    if isinstance(err, DirectError):
        return str(err)
    if isinstance(err, OSError) and err.errno in (98, 48):
        return "porta già in uso"
    text = str(err) or type(err).__name__
    return f"errore: {text}"
