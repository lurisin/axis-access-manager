"""Accesso diretto (fuori da Home Assistant): login con la password dell'app, sessioni, tentativi sbagliati, protezioni
delle richieste e server sulla porta propria."""

import json
import subprocess
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import direct
from app.backup import common_snapshot
from app.direct import (
    COOKIE,
    LoginLimiter,
    hash_password,
    mark_direct,
    password_problem,
    temporary_password,
    verify_password,
)
from app.main import create_app
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
    assert status["running"] is True and status["https"] is False
    port = app.state.direct.bound_port
    base = f"http://127.0.0.1:{port}"
    assert httpx.get(f"{base}/", timeout=5).status_code == 200
    me = httpx.get(f"{base}/api/me", headers=ADMIN, timeout=5)
    assert me.status_code == 401 and me.json()["login"] is True
    off = ingress.put("/api/direct-access", json={"enabled": False}, headers=ADMIN).json()
    assert off["status"]["running"] is False
    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{base}/", timeout=2)


def test_https_with_the_home_assistant_certificate(clients, tmp_path):
    ingress, _, app = clients
    app.state.direct.ssl_dir = tmp_path
    missing = enable(ingress, https=True)
    assert missing["status"]["running"] is False
    assert "fullchain.pem non trovato" in missing["status"]["error"]
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=hotel.lan",
            "-keyout",
            str(tmp_path / "privkey.pem"),
            "-out",
            str(tmp_path / "fullchain.pem"),
        ],
        check=True,
        capture_output=True,
    )
    status = enable(ingress, https=True)["status"]
    assert status["running"] is True and status["https"] is True
    port = app.state.direct.bound_port
    page = httpx.get(f"https://127.0.0.1:{port}/", verify=False, timeout=5)
    assert page.status_code == 200
    staff(ingress)
    temporary = ingress.post("/api/users/maria/password", headers=ADMIN).json()["password"]
    response = httpx.post(
        f"https://127.0.0.1:{port}/api/login",
        json={"username": "maria", "password": temporary},
        headers=SAFE,
        verify=False,
        timeout=5,
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-aam_session=") and "Secure" in cookie and "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
