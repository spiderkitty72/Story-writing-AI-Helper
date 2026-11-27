"""Accounts, sessions, CSRF, rate limiting, per-user books and security headers."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.config import load_config

PW = "correct horse battery"


@pytest.fixture()
def app_dir(tmp_path):
    cfg = load_config()
    cfg.data_dir = tmp_path
    main.state.reload(cfg)
    auth.limiter.reset()
    auth.store().create("admin", PW, is_admin=True)
    auth.store().create("writer", PW)
    return tmp_path


def client(base_url="http://testserver"):
    return TestClient(main.app, base_url=base_url)


def login(c, username="admin", password=PW):
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    if r.status_code == 200:
        c.headers["X-CSRF-Token"] = c.cookies.get(auth.CSRF_COOKIE)
    return r


def test_password_hashing():
    h = auth.hash_password("s3cret-password")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert auth.verify_password("s3cret-password", h)
    assert not auth.verify_password("wrong", h)
    assert auth.hash_password("s3cret-password") != h  # salted


def test_login_required(app_dir):
    c = client()
    assert c.get("/api/books").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/static/login.js").status_code == 200
    assert c.get("/healthz").json() == {"ok": True}


def test_login_logout_and_cookie_flags(app_dir):
    c = client()
    assert login(c, password="nope").status_code == 401
    assert login(c, "nobody").status_code == 401
    r = login(c, "ADMIN ")  # usernames are case-insensitive
    assert r.status_code == 200
    cookie = r.headers.get_list("set-cookie")
    sess = next(h for h in cookie if h.startswith(auth.SESSION_COOKIE))
    assert "HttpOnly" in sess and "SameSite=lax" in sess and "Secure" not in sess  # plain http here
    assert c.get("/api/auth/me").json()["username"] == "admin"
    # the database only has a hash of the token
    token = c.cookies.get(auth.SESSION_COOKIE)
    rows = sqlite3.connect(app_dir / "auth.db").execute("SELECT token_hash FROM sessions").fetchall()
    assert all(token not in r[0] for r in rows)
    assert c.post("/api/auth/logout").status_code == 200
    c.cookies.set(auth.SESSION_COOKIE, token)
    assert c.get("/api/auth/me").status_code == 401


def test_https_cookie_and_headers(app_dir):
    c = client("https://stories.example.com")
    r = login(c)
    assert all("Secure" in h for h in r.headers.get_list("set-cookie"))
    r = c.get("/api/books")
    assert "max-age=" in r.headers["strict-transport-security"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_csrf_and_origin(app_dir):
    c = client()
    login(c)
    token = c.headers.pop("X-CSRF-Token")
    assert c.post("/api/books", json={"title": "x"}).status_code == 403
    assert c.post("/api/books", json={"title": "x"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/api/books", json={"title": "x"}, headers={"X-CSRF-Token": token}).status_code == 200
    evil = {"X-CSRF-Token": token, "Origin": "https://evil.example"}
    assert c.post("/api/books", json={"title": "x"}, headers=evil).status_code == 403
    same = {"X-CSRF-Token": token, "Origin": "http://testserver"}
    assert c.post("/api/books", json={"title": "x"}, headers=same).status_code == 200
    # login itself is also protected from other sites
    assert c.post("/api/auth/login", json={"username": "admin", "password": PW},
                  headers={"Origin": "https://evil.example"}).status_code == 403


def test_rate_limit(app_dir):
    c = client()
    for _ in range(5):
        assert login(c, "writer", "bad password").status_code == 401
    r = login(c, "writer")  # even the right password is refused while locked
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    assert login(c, "admin").status_code == 200  # other accounts are not locked out


def test_books_are_private(app_dir):
    a, w = client(), client()
    login(a)
    login(w, "writer")
    book = a.post("/api/books", json={"title": "Secret"}).json()["id"]
    assert [b["id"] for b in a.get("/api/books").json()] == [book]
    assert w.get("/api/books").json() == []
    assert w.get(f"/api/books/{book}").status_code == 404
    assert w.delete(f"/api/books/{book}").status_code == 404
    ch = a.get(f"/api/books/{book}").json()["chapters"][0]["id"]
    job = a.post(f"/api/books/{book}/knowledge/build", json={"chapter": ch, "provider": "echo"}).json()
    assert any(j["id"] == job["id"] for j in a.get("/api/jobs").json())
    assert w.get("/api/jobs").json() == []
    assert w.get(f"/api/jobs/{job['id']}").status_code == 404
    assert w.post(f"/api/jobs/{job['id']}/cancel").status_code == 404
    assert (app_dir / "users" / "1" / "books" / book).is_dir()


def test_admin_only(app_dir):
    w = client()
    login(w, "writer")
    assert w.get("/api/admin/users").status_code == 403
    assert w.get("/api/config/files/settings.yaml").status_code == 403
    assert w.put("/api/config/files/agents.yaml", json={"text": "agents: {}"}).status_code == 403
    assert w.get("/api/config").status_code == 200  # can still pick agents and providers


def test_admin_manages_users(app_dir):
    a = client()
    login(a)
    r = a.post("/api/admin/users", json={"username": "New.Person", "password": "short"})
    assert r.status_code == 400
    u = a.post("/api/admin/users", json={"username": "New.Person", "password": PW}).json()
    assert u["username"] == "new.person" and not u["is_admin"]
    assert a.post("/api/admin/users", json={"username": "new.person", "password": PW}).status_code == 400
    n = client()
    assert login(n, "new.person").status_code == 200
    # disabling signs them out
    assert a.patch(f"/api/admin/users/{u['id']}", json={"disabled": True}).status_code == 200
    assert n.get("/api/books").status_code == 401
    assert login(n, "new.person").status_code == 401
    me = a.get("/api/auth/me").json()
    assert a.delete(f"/api/admin/users/{me['id']}").status_code == 400
    assert a.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False}).status_code == 400  # last admin
    assert a.delete(f"/api/admin/users/{u['id']}").status_code == 200
    assert [x["username"] for x in a.get("/api/admin/users").json()] == ["admin", "writer"]


def test_password_change_signs_out_other_devices(app_dir):
    phone, laptop = client(), client()
    login(phone, "writer")
    login(laptop, "writer")
    r = laptop.post("/api/auth/password", json={"current_password": "wrong", "new_password": "another long one"})
    assert r.status_code == 400
    r = laptop.post("/api/auth/password", json={"current_password": PW, "new_password": "another long one"})
    assert r.status_code == 200
    laptop.headers["X-CSRF-Token"] = laptop.cookies.get(auth.CSRF_COOKIE)
    assert laptop.get("/api/auth/me").status_code == 200
    assert laptop.post("/api/books", json={"title": "still works"}).status_code == 200
    assert phone.get("/api/auth/me").status_code == 401
    assert login(client(), "writer", "another long one").status_code == 200


def test_first_user_adopts_existing_books(tmp_path, monkeypatch):
    legacy = tmp_path / "books" / "old-book" / "chapters"
    legacy.mkdir(parents=True)
    (legacy / "01.md").write_text("# Old\n")
    monkeypatch.setenv("ADMIN_USERNAME", "josh")
    monkeypatch.setenv("ADMIN_PASSWORD", PW)
    cfg = load_config()
    cfg.data_dir = tmp_path
    main.state.reload(cfg)
    auth.limiter.reset()
    c = client()
    assert login(c, "josh").status_code == 200
    assert [b["id"] for b in c.get("/api/books").json()] == ["old-book"]
    assert not (tmp_path / "books" / "old-book").exists()


def test_cli(app_dir, monkeypatch, capsys):
    import io

    from app import users

    cfg = main.state.config
    monkeypatch.setattr("app.config.load_config", lambda: cfg)
    monkeypatch.setattr("sys.stdin", io.StringIO(PW + "\n"))
    users.main(["add", "cli-user", "--password-stdin"])
    users.main(["list"])
    out = capsys.readouterr().out
    assert "cli-user" in out and "admin  (admin)" in out
    assert login(client(), "cli-user").status_code == 200
