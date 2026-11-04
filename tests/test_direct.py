"""Accesso diretto (fuori da Home Assistant): login con la password dell'app, sessioni, tentativi sbagliati, protezioni
delle richieste e server sulla porta propria."""

import json
import logging
import socket
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import direct
from app.backup import common_snapshot
from app.direct import (
    COOKIE,
    LoginLimiter,
    hash_password,
    host_problem,
    mark_direct,
    password_problem,
    temporary_password,
    verify_password,
)
from app.main import _secure, create_app
from app.users import UserDirectory

ADMIN = {"X-Remote-User-Name": "admin", "X-Remote-User-Id": "id-admin", "X-Remote-User-Display-Name": "Admin"}
SAFE = {"X-AAM-Request": "1"}
NEW_PASSWORD = "Reception-Verde-7"


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    # Parametri leggeri nei test: lo scrypt vero costa un terzo di secondo a password
    monkeypatch.setattr(direct, "SCRYPT_N", 2**8)
    monkeypatch.setattr(direct, "SCRYPT_P", 1)


@pytest.fixture
def clients(settings, controller, tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    app = create_app(settings, allow_any_client=True, controller=controller, db_path=tmp_path / "d.db", direct_port=0)
    with TestClient(app) as ingress:
        # Le richieste dell'accesso diretto passano dall'app segnata, nello stesso ciclo di eventi
        site = TestClient(mark_direct(app), base_url="http://hotel.lan:8100")
        site.portal = ingress.portal
        assert ingress.get("/api/me", headers=ADMIN).json()["admin"] is True
        yield ingress, site, app


def enable(ingress, **extra):
    response = ingress.put("/api/direct-access", json={"enabled": True, **extra}, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


def staff(ingress, username="maria", perms=("persone", "persone_modifica", "regole")) -> str:
    """Utente del personale con la password provvisoria."""
    response = ingress.post(
        "/api/users", json={"username": username, "name": username.title(), "perms": list(perms)}, headers=ADMIN
    )
    assert response.status_code == 200, response.text
    return ingress.post(f"/api/users/{username}/password", headers=ADMIN).json()["password"]


def login(site, username, password):
    return site.post("/api/login", json={"username": username, "password": password}, headers=SAFE)


def ready(ingress, site, username="maria", perms=("persone", "persone_modifica", "regole")):
    """Utente entrato dall'accesso diretto con la password già cambiata."""
    temporary = staff(ingress, username, perms)
    assert login(site, username, temporary).json() == {"ok": True, "must_change": True}
    response = site.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE)
    assert response.status_code == 200, response.text


def test_password_hashing_and_policy():
    stored = hash_password("una password lunga")
    assert stored.startswith("scrypt$") and "una password" not in stored
    assert verify_password("una password lunga", stored)
    assert not verify_password("una password lungA", stored)
    assert not verify_password("x", "rotto")
    assert hash_password("uguale12345") != hash_password("uguale12345")
    assert password_problem("corta", "maria")
    assert "nome utente" in password_problem("maria-2026-hotel", "maria")
    assert "facile" in password_problem("1234567890", "maria")
    assert "facile" in password_problem("aaaaaaaaaaaa", "maria")
    assert password_problem(NEW_PASSWORD, "maria") == ""
    temporary = temporary_password()
    assert len(temporary) == 14 and temporary.count("-") == 2 and not set(temporary) & set("01ilo")


def test_off_until_enabled(clients):
    ingress, site, _ = clients
    response = site.get("/api/me")
    assert response.status_code == 403
    assert response.json()["detail"] == "accesso diretto spento"
    assert ingress.get("/api/direct-access", headers=ADMIN).json()["settings"]["enabled"] is False


def test_activity_says_on_and_off(clients):
    ingress, _, app = clients
    enable(ingress)
    assert ingress.put("/api/direct-access", json={"enabled": False}, headers=ADMIN).status_code == 200
    # Come nell'interfaccia: Attivo e Disattivato
    actions = [a["action"] for a in app.state.db.list_audit()]
    assert actions[:2] == ["accesso diretto disattivato", "accesso diretto attivo"]


def test_ingress_headers_never_count_on_direct_access(clients):
    ingress, site, app = clients
    enable(ingress)
    for path in ("/api/me", "/api/persons", "/api/users", "/api/summary"):
        response = site.get(path, headers={**ADMIN, "X-Remote-User-Name": "intruso"})
        assert response.status_code == 401, path
        assert response.json()["login"] is True
    # Nessun utente creato dalle intestazioni, nessun accesso locale
    assert [u["username"] for u in app.state.db.list_app_users()] == ["admin"]
    assert site.get("/").status_code == 200


def test_temporary_password_must_be_changed_first(clients):
    ingress, site, _ = clients
    enable(ingress)
    temporary = staff(ingress)
    response = login(site, "MARIA ", temporary)
    assert response.json() == {"ok": True, "must_change": True}
    assert COOKIE in response.cookies
    blocked = site.get("/api/persons")
    assert blocked.status_code == 403 and blocked.json()["must_change"] is True
    me = site.get("/api/me").json()
    assert me["direct"] is True and me["must_change"] is True and me["admin"] is False
    # Uguale alla provvisoria, troppo corta, attuale sbagliata
    assert site.post("/api/password", json={"current": temporary, "new": temporary}, headers=SAFE).status_code == 400
    assert site.post("/api/password", json={"current": temporary, "new": "corta"}, headers=SAFE).status_code == 400
    wrong = site.post("/api/password", json={"current": "sbagliata", "new": NEW_PASSWORD}, headers=SAFE)
    assert wrong.json()["detail"] == "password attuale errata"
    changed = site.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE)
    assert changed.status_code == 200
    assert site.get("/api/persons").status_code == 200
    assert site.get("/api/me").json()["must_change"] is False
    # La provvisoria non vale più, la nuova sì
    site.cookies.clear()
    assert login(site, "maria", temporary).status_code == 401
    assert login(site, "maria", NEW_PASSWORD).json() == {"ok": True, "must_change": False}


def test_same_answer_for_wrong_password_and_unknown_user(clients):
    ingress, site, _ = clients
    enable(ingress)
    staff(ingress)
    wrong = login(site, "maria", "non è questa")
    unknown = login(site, "nessuno", "non è questa")
    # L'amministratore senza password: neanche lui entra
    no_password = login(site, "admin", "non è questa")
    assert wrong.status_code == unknown.status_code == no_password.status_code == 401
    assert wrong.json() == unknown.json() == no_password.json() == {"detail": "nome utente o password errati"}


def test_blocked_after_five_wrong_attempts_even_with_the_right_password(clients):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    for _ in range(5):
        assert login(site, "maria", "sbagliata").status_code == 401
    blocked = login(site, "maria", temporary)
    assert blocked.status_code == 429
    assert "riprova tra 15 minuti" in blocked.json()["detail"]
    assert any(a["action"] == "accesso diretto bloccato per troppi tentativi" for a in app.state.db.list_audit())
    # Una password nuova dall'amministratore sblocca subito
    temporary = ingress.post("/api/users/maria/password", headers=ADMIN).json()["password"]
    assert login(site, "maria", temporary).status_code == 200


def test_limiter_counts_attempts_before_checking_the_password():
    now = [1000.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    # Cinque tentativi partiti insieme, prima che la password sia controllata: il sesto è bloccato
    started = [limiter.attempt("10.0.0.5", "maria") for _ in range(5)]
    assert started[-1] and not any(started[:-1])
    assert limiter.blocked("10.0.0.5", "maria") > 0
    # Da un altro indirizzo l'utente può ancora provare
    assert limiter.blocked("10.0.0.6", "maria") == 0
    now[0] += 15 * 60 + 1
    assert limiter.blocked("10.0.0.5", "maria") == 0
    # Un accesso riuscito non lascia tentativi in sospeso
    limiter.attempt("10.0.0.5", "maria")
    limiter.success("10.0.0.5", "maria")
    assert not limiter.failures[("pair", "10.0.0.5", "maria")]


def test_changes_need_the_page_header_and_the_same_origin(clients):
    ingress, site, _ = clients
    enable(ingress)
    ready(ingress, site)
    body = {"name": "Ospite Prova", "card": "aa11bb22"}
    assert site.post("/api/persons", json=body).status_code == 403
    assert site.post("/api/persons", json=body, headers={**SAFE, "Origin": "http://altro.sito"}).status_code == 403
    assert site.post("/api/persons", json=body, headers={**SAFE, "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert site.post("/api/login", json={"username": "maria", "password": NEW_PASSWORD}).status_code == 403
    same = {**SAFE, "Origin": "http://hotel.lan:8100", "Sec-Fetch-Site": "same-origin"}
    assert site.post("/api/persons", json=body, headers=same).status_code == 200


def test_administration_only_from_home_assistant(clients):
    ingress, site, _ = clients
    enable(ingress)
    temporary = ingress.post("/api/users/admin/password", headers=ADMIN).json()["password"]
    login(site, "admin", temporary)
    site.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE)
    me = site.get("/api/me").json()
    assert me["admin"] is False and "persone_modifica" in me["perms"] and "utenti" not in me["perms"]
    for method, path in (
        ("GET", "/api/users"),
        ("POST", "/api/users/admin/password"),
        ("PUT", "/api/direct-access"),
        ("GET", "/api/audit"),
    ):
        response = site.request(method, path, headers=SAFE, json={"enabled": True} if method == "PUT" else None)
        assert response.status_code == 403, path
        assert response.json()["detail"] == "si fa solo dall'app dentro Home Assistant"
    assert site.get("/api/persons").status_code == 200


def test_admin_only_on_direct_access_does_not_count_as_the_last_admin(clients):
    """Revisione: un amministratore che entra solo dall'accesso diretto contava come amministratore. L'ultimo
    amministratore di Home Assistant poteva togliersi i permessi, e nessuno poteva più gestire utenti e ripristini."""
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site, "direttore", perms=("utenti",))
    assert site.get("/api/users").status_code == 403
    response = ingress.put("/api/users/admin", json={"name": "Admin", "perms": ["stato"]}, headers=ADMIN)
    assert response.status_code == 400
    assert response.json()["detail"].startswith("serve almeno un amministratore che entra da Home Assistant")
    assert ingress.delete("/api/users/admin", headers=ADMIN).status_code == 400
    # Un altro amministratore già entrato da Home Assistant conta
    luca = {"X-Remote-User-Name": "luca", "X-Remote-User-Id": "id-luca"}
    ingress.get("/api/me", headers=luca)
    assert ingress.put("/api/users/luca", json={"perms": ["utenti"]}, headers=ADMIN).status_code == 200
    assert ingress.put("/api/users/admin", json={"name": "Admin", "perms": ["stato"]}, headers=ADMIN).status_code == 200


def test_home_assistant_reinstalled_with_an_admin_only_on_direct_access(db):
    """Revisione: Home Assistant reinstallato (id nuovo per lo stesso nome utente) con un amministratore solo
    dell'accesso diretto: i permessi finivano sotto il vecchio id e l'amministratore restava «Solo Stato»."""
    directory = UserDirectory(db)
    assert directory.resolve("luca", "Luca", "id-luca").can("utenti")
    db.save_app_user("direttore", "Direttore", ["utenti"])
    db.set_password("direttore", hash_password("password-del-direttore"), False, "luca")
    db.touch_app_user("direttore", "")
    again = directory.resolve("luca", "Luca", "id-luca-nuovo")
    assert again.username == "luca" and again.can("utenti")


def test_new_temporary_password_removal_and_deletion_close_sessions(clients):
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site)
    assert site.get("/api/persons").status_code == 200
    ingress.post("/api/users/maria/password", headers=ADMIN)
    assert site.get("/api/persons").status_code == 401
    ready_again = ingress.post("/api/users/maria/password", headers=ADMIN).json()["password"]
    login(site, "maria", ready_again)
    site.post("/api/password", json={"current": ready_again, "new": NEW_PASSWORD}, headers=SAFE)
    assert ingress.delete("/api/users/maria/sessions", headers=ADMIN).json()["closed"] == 1
    assert site.get("/api/persons").status_code == 401
    assert login(site, "maria", NEW_PASSWORD).status_code == 200
    assert ingress.delete("/api/users/maria/password", headers=ADMIN).json()["closed"] == 1
    assert site.get("/api/persons").status_code == 401
    assert login(site, "maria", NEW_PASSWORD).status_code == 401
    # Eliminato l'utente, password e sessioni se ne vanno con lui
    staff(ingress, "luca")
    ingress.delete("/api/users/luca", headers=ADMIN)
    assert app.state.db.get_password("luca") is None


def test_users_page_shows_direct_access(clients):
    ingress, site, _ = clients
    enable(ingress)
    ready(ingress, site)
    users = {u["username"]: u for u in ingress.get("/api/users", headers=ADMIN).json()["users"]}
    assert users["admin"]["direct"] is None
    access = users["maria"]["direct"]
    assert access["temporary"] is False and access["expired"] is False
    assert len(access["sessions"]) == 1 and access["sessions"][0]["device"]


def test_session_stays_open_until_unused_for_ninety_days(clients):
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site)
    db = app.state.db
    old = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat(timespec="seconds")
    db._execute("UPDATE sessions SET created_at = ?, last_seen = ?", (old, old))
    response = site.get("/api/persons")
    assert response.status_code == 200
    # Usata: si rinnova, cookie compreso
    assert COOKIE in response.cookies
    assert db.list_sessions()[0]["last_seen"] > old
    stale = (datetime.now(tz=UTC) - timedelta(days=91)).isoformat(timespec="seconds")
    db._execute("UPDATE sessions SET last_seen = ?", (stale,))
    assert site.get("/api/persons").status_code == 401
    assert db.list_sessions() == []


def test_full_disk_does_not_stop_direct_access_reads(clients, monkeypatch):
    """Disco pieno: il rinnovo della sessione falliva e ogni richiesta del personale rispondeva 500."""
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site)
    db = app.state.db
    old = (datetime.now(tz=UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    db._execute("UPDATE sessions SET last_seen = ?", (old,))

    def full(*args):
        err = sqlite3.OperationalError("database or disk is full")
        err.sqlite_errorcode = sqlite3.SQLITE_FULL
        raise err

    monkeypatch.setattr(db, "touch_session", full)
    response = site.get("/api/persons")
    assert response.status_code == 200
    # Sessione non rinnovata: il cookie resta quello di prima
    assert COOKIE not in response.cookies
    assert db.list_sessions()[0]["last_seen"] == old
    # Al login un motivo chiaro invece di un errore senza messaggio
    monkeypatch.setattr(db, "add_session", full)
    response = login(site, "maria", NEW_PASSWORD)
    assert response.status_code == 503
    assert response.json()["detail"] == "spazio su disco esaurito: avvisa l'amministratore"


def test_temporary_password_and_its_session_expire(clients):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    db = app.state.db
    login(site, "maria", temporary)
    # Con la password provvisoria la sessione dura un'ora
    hour_ago = (datetime.now(tz=UTC) - timedelta(minutes=61)).isoformat(timespec="seconds")
    db._execute("UPDATE sessions SET created_at = ?", (hour_ago,))
    assert site.get("/api/me").status_code == 401
    week_ago = (datetime.now(tz=UTC) - timedelta(days=8)).isoformat(timespec="microseconds")
    db._execute("UPDATE app_user_passwords SET set_at = ?", (week_ago,))
    expired = login(site, "maria", temporary)
    assert expired.status_code == 401
    assert "scaduta" in expired.json()["detail"]
    users = {u["username"]: u for u in ingress.get("/api/users", headers=ADMIN).json()["users"]}
    assert users["maria"]["direct"]["expired"] is True


def test_logout_closes_the_session(clients):
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site)
    response = site.post("/api/logout", headers=SAFE)
    assert response.status_code == 200
    assert app.state.db.list_sessions() == []
    assert site.get("/api/persons").status_code == 401
    assert any(a["action"] == "uscita dall'accesso diretto" for a in app.state.db.list_audit())


def test_security_headers_only_on_direct_access(clients):
    ingress, site, _ = clients
    enable(ingress)
    page = site.get("/")
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert site.get("/api/me").headers["cache-control"] == "no-store"
    # Dentro Home Assistant la pagina sta in un frame
    assert "x-frame-options" not in ingress.get("/").headers


def test_login_only_on_direct_access(clients):
    ingress, _, _ = clients
    assert ingress.post("/api/login", json={"username": "admin", "password": "x"}, headers=SAFE).status_code == 404


def test_password_change_from_home_assistant_needs_the_current_one(clients):
    ingress, site, _ = clients
    enable(ingress)
    temporary = ingress.post("/api/users/admin/password", headers=ADMIN).json()["password"]
    wrong = ingress.post("/api/password", json={"current": "x", "new": NEW_PASSWORD}, headers=ADMIN)
    assert wrong.status_code == 400
    assert (
        ingress.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=ADMIN).status_code
        == 200
    )
    assert login(site, "admin", NEW_PASSWORD).json() == {"ok": True, "must_change": False}


def test_credentials_follow_a_renamed_user(db):
    directory = UserDirectory(db)
    directory.resolve("mario", "Mario", "id-1")
    db.set_password("mario", hash_password("password-di-mario"), False, "admin")
    db.add_session("abc", "mario", db.get_password("mario")["set_at"], "", "")
    # Nome utente cambiato in Home Assistant: password e sessione lo seguono
    directory.resolve("mario.rossi", "Mario", "id-1")
    assert db.get_password("mario") is None
    assert db.get_password("mario.rossi") is not None
    assert db.get_session("abc")["username"] == "mario.rossi"


def test_passwords_and_sessions_stay_out_of_backups(clients):
    ingress, site, app = clients
    enable(ingress)
    ready(ingress, site)
    text = json.dumps(common_snapshot(app.state.db, []))
    assert "scrypt$" not in text and "token_hash" not in text and "direct_access" not in text


def test_real_server_on_its_own_port(clients):
    ingress, _, app = clients
    status = enable(ingress)["status"]
    assert status["running"] is True
    port = app.state.direct.bound_port
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/", timeout=5).status_code == 200
    me = httpx.get(f"{base}/api/me", headers=ADMIN, timeout=5)
    assert me.status_code == 401 and me.json()["login"] is True
    off = ingress.put("/api/direct-access", json={"enabled": False}, headers=ADMIN).json()
    assert off["status"]["running"] is False
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{base}/", timeout=2)


def test_host_header_cannot_change_the_checked_path(clients):
    ingress, site, _ = clients
    enable(ingress)
    temporary = staff(ingress)
    assert login(site, "maria", temporary).status_code == 200
    # Con la password provvisoria si può solo cambiarla: un Host con / e ? non fa vedere al controllo un altro percorso
    assert site.get("/api/persons").status_code == 403
    assert site.get("/api/persons", headers={"host": "hotel.lan/api/me?"}).status_code == 400
    assert (
        site.post("/api/persons", json={"name": "X"}, headers={**SAFE, "host": "hotel.lan/api/me?"}).status_code == 400
    )
    # Lo schema delle API non si pubblica
    assert ingress.get("/openapi.json").status_code == 404
    assert site.get("/openapi.json", headers={"host": "hotel.lan/api/login?"}).status_code == 400


def test_public_names_refused_over_http(clients):
    ingress, site, _ = clients
    enable(ingress)
    staff(ingress)
    # DNS rebinding: una pagina di un altro sito che punta all'IP di Home Assistant ha il suo nome nell'Host
    evil = {**SAFE, "host": "evil.example:8100", "origin": "http://evil.example:8100"}
    assert site.post("/api/login", json={"username": "maria", "password": "x" * 12}, headers=evil).status_code == 421
    page = site.get("/", headers={"host": "evil.example:8100"})
    assert page.status_code == 421 and "indirizzo IP" in page.text
    for host in ("192.168.1.10:8100", "[fe80::1]:8100", "homeassistant.local:8100", "homeassistant:8100"):
        assert site.get("/", headers={"host": host}).status_code == 200, host


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("10.103.200.5:8100", True),
        ("10.103.200.5", True),
        ("[fd00::5]:8100", True),
        ("ha.hotel.lan:8100", True),
        ("homeassistant.local", True),
        ("HomeAssistant.LOCAL:8100", True),
        ("reception-pc", True),
        ("accessi.hotel.it:8100", False),
        ("hotel.lan/api/me?", False),
        ("hotel.lan:8100 x", False),
        ("user@hotel.lan", False),
        ("", False),
    ],
)
def test_host_problem(host, ok):
    assert (host_problem(host) == "") is ok


def test_request_body_limits(clients):
    ingress, site, _ = clients
    enable(ingress)
    staff(ingress)
    huge = {"username": "maria", "password": "x" * 20000}
    response = site.post("/api/login", json=huge, headers=SAFE)
    assert response.status_code == 413 and response.json()["detail"] == "richiesta troppo grande"

    def chunks():
        yield b'{"username": "maria", "password": "'
        yield b"x" * 20000
        yield b'"}'

    # Senza Content-Length (a pezzi) il limite vale lo stesso
    chunked = site.post("/api/login", content=chunks(), headers={**SAFE, "content-type": "application/json"})
    assert chunked.status_code == 413
    big = site.post("/api/import/persons", content=b"x" * (direct.BODY_LIMIT + 1), headers=SAFE)
    assert big.status_code == 413
    # Un login normale passa
    assert login(site, "maria", "x" * 12).status_code == 401


def test_no_websockets_on_direct_access(clients):
    ingress, site, _ = clients
    enable(ingress)
    with pytest.raises(WebSocketDisconnect), site.websocket_connect("/api/me"):
        pass


def test_no_browser_cache_for_api_answers_on_direct_access():
    # Le foto degli eventi hanno una cache di un giorno: dall'accesso diretto no, su un PC condiviso resterebbero
    photo = _secure(Response(b"jpeg", headers={"Cache-Control": "max-age=86400"}), "/api/events/7/snapshot")
    assert photo.headers["cache-control"] == "no-store"
    page = _secure(Response(b"html", headers={"Cache-Control": "no-cache"}), "/")
    assert page.headers["cache-control"] == "no-cache"


def test_limiter_keeps_counting_across_a_block():
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    allowed = 0
    # Un tentativo ogni 5 secondi per un'ora, da 50 indirizzi diversi: per l'utente al massimo 30 più uno a blocco
    for i in range(720):
        ip = f"10.0.1.{i % 50}"
        if not limiter.blocked(ip, "maria"):
            limiter.attempt(ip, "maria")
            allowed += 1
        now[0] += 5
    assert 30 <= allowed <= 34


def test_limiter_counts_hidden_addresses_together():
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0])
    # PC dietro il gateway Docker (indirizzo non visibile): un nome utente diverso a ogni tentativo non basta
    for i in range(direct.HIDDEN_IP_LIMIT - 1):
        limiter.attempt("", f"utente{i}")
    assert limiter.blocked("", "un-altro") == 0
    limiter.attempt("", "ultimo")
    assert limiter.blocked("", "un-altro") > 0
    assert limiter.blocked("10.0.0.9", "un-altro") == 0


def test_limiter_memory_is_bounded():
    now = [0.0]
    limiter = LoginLimiter(clock=lambda: now[0], max_keys=100)
    for i in range(2000):
        for _ in range(5):
            limiter.attempt(f"10.1.{i // 250}.{i % 250}", f"utente{i}")
    assert len(limiter.failures) <= 100 and len(limiter.blocked_until) <= 100
    # Quelli recenti restano
    assert limiter.blocked("10.1.7.249", "utente1999") > 0


def test_login_refused_without_counting_when_hashing_is_busy(clients, monkeypatch):
    ingress, site, app = clients
    enable(ingress)
    staff(ingress)
    monkeypatch.setattr(app.state.hasher, "full", lambda: True)
    response = login(site, "maria", "x" * 12)
    assert response.status_code == 429 and "troppe richieste" in response.json()["detail"]
    assert not app.state.login_limiter.failures


def test_password_change_loses_to_admin_removal(clients, monkeypatch):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    assert login(site, "maria", temporary).status_code == 200
    real_hash = app.state.hasher.hash

    async def hash_while_admin_removes_access(password):
        result = await real_hash(password)
        app.state.db.delete_password("maria")
        return result

    monkeypatch.setattr(app.state.hasher, "hash", hash_while_admin_removes_access)
    response = site.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE)
    assert response.status_code == 409
    assert app.state.db.get_password("maria") is None
    assert not app.state.db.list_sessions()


def test_new_user_does_not_inherit_leftover_credentials(db):
    db.save_app_user("maria", "Maria", ["persone"])
    set_at = db.set_password("maria", "scrypt$x", False, "admin")
    db.add_session("abc", "maria", set_at, "", "")
    # Righe rimaste senza l'utente (scritte mentre veniva eliminato)
    with db._lock:
        db._conn.execute("DELETE FROM app_users WHERE username = 'maria'")
        db._conn.commit()
    assert db.set_password("maria", "scrypt$y", False, "admin") is None
    db.save_app_user("maria", "Maria Nuova", ["stato"])
    assert db.get_password("maria") is None and db.get_session("abc") is None


def test_real_server_closes_slow_and_extra_connections(clients, monkeypatch):
    ingress, _, app = clients
    monkeypatch.setattr(direct, "HEADER_TIMEOUT", 0.3)
    monkeypatch.setattr(direct, "MAX_CONNECTIONS_PER_IP", 3)
    enable(ingress)
    port = app.state.direct.bound_port
    # Richiesta mai finita: il server chiude la connessione
    with socket.create_connection(("127.0.0.1", port), timeout=5) as slow:
        slow.sendall(b"GET / HTTP/1.1\r\nHost: hotel.lan\r\n")
        assert slow.recv(100) == b""
    monkeypatch.setattr(direct, "HEADER_TIMEOUT", 5)
    sockets = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(4)]
    try:
        # Oltre il limite per indirizzo: chiusa subito
        assert sockets[3].recv(100) == b""
        sockets[0].sendall(b"GET / HTTP/1.1\r\nHost: hotel.lan\r\nConnection: close\r\n\r\n")
        assert sockets[0].recv(20).startswith(b"HTTP/1.1 200")
    finally:
        for sock in sockets:
            sock.close()


def test_right_password_on_the_fifth_attempt_leaves_no_block(clients):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    for _ in range(4):
        assert login(site, "maria", "sbagliata-123").status_code == 401
    # Il quinto tentativo arriva al limite, ma la password è giusta: il cambio obbligatorio deve poter partire
    assert login(site, "maria", temporary).json()["must_change"] is True
    response = site.post("/api/password", json={"current": temporary, "new": NEW_PASSWORD}, headers=SAFE)
    assert response.status_code == 200, response.text
    assert not app.state.login_limiter.blocked_until
    assert not any("bloccato" in a["action"] for a in app.state.db.list_audit())


def test_new_temporary_password_also_clears_the_shared_block(clients):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    # Nei test l'indirizzo non è visibile (come dietro il gateway Docker): 40 tentativi con nomi a caso bloccano tutti
    for i in range(40):
        login(site, f"nessuno{i}", "sbagliata-123")
    assert login(site, "maria", temporary).status_code == 429
    assert any(a["detail"] == "tutti i PC con indirizzo non visibile" for a in app.state.db.list_audit())
    temporary = ingress.post("/api/users/maria/password", headers=ADMIN).json()["password"]
    assert login(site, "maria", temporary).status_code == 200


def test_direct_server_leaves_process_logging_alone(clients):
    ingress, _, _ = clients
    access = logging.getLogger("uvicorn.access")
    marker = logging.NullHandler()
    access.addHandler(marker)
    try:
        enable(ingress)
        assert marker in access.handlers
    finally:
        access.removeHandler(marker)


def test_real_server_closes_requests_whose_body_never_arrives(clients, monkeypatch):
    ingress, _, app = clients
    monkeypatch.setattr(direct, "HEADER_TIMEOUT", 0.2)
    monkeypatch.setattr(direct, "BODY_TIMEOUT", 0.5)
    enable(ingress)
    port = app.state.direct.bound_port
    with socket.create_connection(("127.0.0.1", port), timeout=5) as stalled:
        stalled.sendall(
            b"POST /api/login HTTP/1.1\r\nHost: hotel.lan\r\nX-AAM-Request: 1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
        )
        started = time.monotonic()
        assert stalled.recv(100) == b""
        assert time.monotonic() - started < 3
    # Una richiesta lenta ma che arriva passa
    with socket.create_connection(("127.0.0.1", port), timeout=5) as slow:
        body = b'{"username": "maria", "password": "sbagliata-123"}'
        slow.sendall(
            b"POST /api/login HTTP/1.1\r\nHost: hotel.lan\r\nX-AAM-Request: 1\r\nConnection: close\r\n"
            b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        time.sleep(0.3)
        slow.sendall(body)
        assert slow.recv(20).startswith(b"HTTP/1.1 401")


def test_expired_temporary_password_on_the_limit_leaves_no_block(clients):
    ingress, site, app = clients
    enable(ingress)
    temporary = staff(ingress)
    old = (datetime.now(tz=UTC) - timedelta(days=8)).isoformat(timespec="microseconds")
    with app.state.db._lock:
        app.state.db._conn.execute("UPDATE app_user_passwords SET set_at = ? WHERE username = 'maria'", (old,))
        app.state.db._conn.commit()
    for _ in range(4):
        login(site, "maria", "sbagliata-123")
    response = login(site, "maria", temporary)
    assert response.status_code == 401 and "scaduta" in response.json()["detail"]
    assert not app.state.login_limiter.blocked_until


def test_only_a_wrong_current_password_is_flagged(clients):
    ingress, site, _ = clients
    enable(ingress)
    temporary = staff(ingress)
    login(site, "maria", temporary)
    wrong = site.post("/api/password", json={"current": "sbagliata-123", "new": NEW_PASSWORD}, headers=SAFE)
    assert wrong.status_code == 400 and wrong.json()["current_wrong"] is True
    # La nuova non va bene: la pagina tiene la provvisoria che ha in memoria
    weak = site.post("/api/password", json={"current": temporary, "new": "maria-12345678"}, headers=SAFE)
    assert weak.status_code == 400 and "current_wrong" not in weak.json()
