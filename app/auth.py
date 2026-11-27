"""User accounts, logins and the security layer in front of the whole app.

Accounts and sessions live in ``<data_dir>/auth.db`` (SQLite). Every user gets
their own folder of books under ``<data_dir>/users/<user id>/``.

Security measures:

- Passwords are hashed with scrypt (memory-hard) and a random salt.
- Sessions are random tokens in an HttpOnly, SameSite=Lax cookie that is marked
  Secure when the app is reached over HTTPS. Only a SHA-256 of each token is
  stored, so a copy of the database cannot be used to log in.
- Every request that changes something must carry the session's CSRF token in
  an ``X-CSRF-Token`` header, and its Origin (when sent) must match the site.
- Failed logins are rate limited per client IP and per username.
- Security headers (CSP, frame blocking, HSTS over HTTPS, ...) on every response.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

log = logging.getLogger("storyhelper.auth")

STATIC = Path(__file__).parent / "static"

SESSION_COOKIE = "storyhelper_session"
CSRF_COOKIE = "storyhelper_csrf"
CSRF_HEADER = "x-csrf-token"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


SESSION_IDLE_SECONDS = _env_int("SESSION_IDLE_HOURS", 24 * 7) * 3600
SESSION_MAX_SECONDS = _env_int("SESSION_MAX_DAYS", 30) * 86400
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}$")

# ---------------------------------------------------------------- passwords
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                         maxmem=_SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, key = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=_SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, expected)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError("Password is too long")


def normalize_username(name: str) -> str:
    name = (name or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("Username must be 2-32 characters: letters, digits, '.', '_' or '-'")
    return name


# ---------------------------------------------------------------- store
@dataclass
class User:
    id: int
    username: str
    is_admin: bool
    disabled: bool = False
    created: float = 0.0

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "is_admin": self.is_admin,
                "disabled": self.disabled, "created": self.created}


@dataclass
class Session:
    user: User
    csrf: str
    token_hash: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class UserStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "auth.db"
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    pw_hash TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    csrf TEXT NOT NULL,
                    created REAL NOT NULL,
                    last_seen REAL NOT NULL
                );
            """)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            db = self._db()
            try:
                return db.execute(sql, args).fetchall()
            finally:
                db.close()

    @staticmethod
    def _user(row: sqlite3.Row) -> User:
        return User(id=row["id"], username=row["username"], is_admin=bool(row["is_admin"]),
                    disabled=bool(row["disabled"]), created=row["created"])

    # users
    def count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM users")[0]["n"]

    def list_users(self) -> list[User]:
        return [self._user(r) for r in self._q("SELECT * FROM users ORDER BY username")]

    def get(self, user_id: int) -> User | None:
        rows = self._q("SELECT * FROM users WHERE id = ?", (user_id,))
        return self._user(rows[0]) if rows else None

    def get_by_name(self, username: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE username = ?", ((username or "").strip().lower(),))
        return self._user(rows[0]) if rows else None

    def create(self, username: str, password: str, is_admin: bool = False) -> User:
        username = normalize_username(username)
        check_password_rules(password)
        if self.get_by_name(username):
            raise ValueError(f"User '{username}' already exists")
        first = self.count() == 0
        self._q("INSERT INTO users (username, pw_hash, is_admin, created) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), int(is_admin), time.time()))
        user = self.get_by_name(username)
        assert user is not None
        if first:
            self._adopt_legacy_books(user)
        return user

    def authenticate(self, username: str, password: str) -> User | None:
        rows = self._q("SELECT * FROM users WHERE username = ?", ((username or "").strip().lower(),))
        if not rows:
            verify_password(password, _DUMMY_HASH)  # same work either way, so timing reveals nothing
            return None
        if not verify_password(password, rows[0]["pw_hash"]) or rows[0]["disabled"]:
            return None
        return self._user(rows[0])

    def set_password(self, user_id: int, password: str) -> None:
        check_password_rules(password)
        self._q("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), user_id))
        self.revoke_sessions(user_id)

    def update(self, user_id: int, *, is_admin: bool | None = None, disabled: bool | None = None) -> None:
        user = self.get(user_id)
        if not user:
            raise KeyError("user not found")
        if (is_admin is False or disabled is True) and user.is_admin and not user.disabled \
                and self._active_admins() <= 1:
            raise ValueError("There must always be at least one active admin")
        if is_admin is not None:
            self._q("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
        if disabled is not None:
            self._q("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
            if disabled:
                self.revoke_sessions(user_id)

    def delete(self, user_id: int) -> None:
        user = self.get(user_id)
        if not user:
            raise KeyError("user not found")
        if user.is_admin and not user.disabled and self._active_admins() <= 1:
            raise ValueError("There must always be at least one active admin")
        self._q("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        self._q("DELETE FROM users WHERE id = ?", (user_id,))

    def _active_admins(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND disabled = 0")[0]["n"]

    def user_dir(self, user: User) -> Path:
        return self.data_dir / "users" / str(user.id)

    def _adopt_legacy_books(self, user: User) -> None:
        """Books written before accounts existed (data/books) go to the first user."""
        legacy = self.data_dir / "books"
        target = self.user_dir(user) / "books"
        if not legacy.is_dir() or target.exists():
            return
        books = [p for p in legacy.iterdir() if p.is_dir()]
        if not books:
            return
        target.mkdir(parents=True)
        for p in books:
            shutil.move(str(p), str(target / p.name))
        log.warning("Moved %d existing book(s) from %s to %s (owned by %s)", len(books), legacy, target,
                    user.username)

    # sessions
    def create_session(self, user: User) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        self._q("INSERT INTO sessions (token_hash, user_id, csrf, created, last_seen) VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), user.id, csrf, now, now))
        self._q("DELETE FROM sessions WHERE last_seen < ? OR created < ?",
                (now - SESSION_IDLE_SECONDS, now - SESSION_MAX_SECONDS))
        return token, csrf

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 200:
            return None
        th = _token_hash(token)
        rows = self._q("SELECT s.csrf, s.created, s.last_seen, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = ?", (th,))
        if not rows:
            return None
        row, now = rows[0], time.time()
        if row["disabled"] or now - row["last_seen"] > SESSION_IDLE_SECONDS or now - row["created"] > SESSION_MAX_SECONDS:
            self._q("DELETE FROM sessions WHERE token_hash = ?", (th,))
            return None
        if now - row["last_seen"] > 300:
            self._q("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, th))
        return Session(user=self._user(row), csrf=row["csrf"], token_hash=th)

    def delete_session(self, token_hash: str) -> None:
        self._q("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def revoke_sessions(self, user_id: int, keep: str | None = None) -> None:
        self._q("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, keep or ""))


# ---------------------------------------------------------------- rate limiting
class LoginLimiter:
    """Counts failed logins in a sliding window, per client IP and per username."""

    def __init__(self, per_ip: int = 10, per_user: int = 5, window: float = 900):
        self.per_ip, self.per_user, self.window = per_ip, per_user, window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, ip: str, username: str) -> int:
        """Seconds until another try is allowed, or 0."""
        now = time.time()
        with self._lock:
            waits = []
            for key, limit in ((f"ip:{ip}", self.per_ip), (f"user:{username}", self.per_user)):
                q = self._recent(key, now)
                if len(q) >= limit:
                    waits.append(int(self.window - (now - q[-limit])) + 1)
            return max(waits, default=0)

    def failed(self, ip: str, username: str) -> None:
        now = time.time()
        with self._lock:
            for key in (f"ip:{ip}", f"user:{username}"):
                self._recent(key, now).append(now)
            if len(self._fails) > 10000:  # keep memory bounded
                for k in [k for k, q in self._fails.items() if not self._recent(k, now)]:
                    del self._fails[k]

    def succeeded(self, username: str) -> None:
        with self._lock:
            self._fails.pop(f"user:{username}", None)

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()


limiter = LoginLimiter(per_ip=_env_int("LOGIN_MAX_FAILS_PER_IP", 10),
                       per_user=_env_int("LOGIN_MAX_FAILS_PER_USER", 5),
                       window=_env_int("LOGIN_LOCKOUT_MINUTES", 15) * 60)

# ---------------------------------------------------------------- current user
current_user: ContextVar[User | None] = ContextVar("current_user", default=None)
current_session: ContextVar[Session | None] = ContextVar("current_session", default=None)

_data_dir_getter: Callable[[], Path] | None = None
_stores: dict[Path, UserStore] = {}
_store_lock = threading.Lock()


def configure(data_dir_getter: Callable[[], Path]) -> None:
    global _data_dir_getter
    _data_dir_getter = data_dir_getter


def store() -> UserStore:
    if _data_dir_getter is None:
        raise RuntimeError("auth.configure() was not called")
    d = Path(_data_dir_getter()).resolve()
    with _store_lock:
        if d not in _stores:
            _stores[d] = UserStore(d)
            _bootstrap_admin(_stores[d])
        return _stores[d]


def _bootstrap_admin(s: UserStore) -> None:
    """Create the first admin from ADMIN_USERNAME / ADMIN_PASSWORD if there are no users yet."""
    if s.count():
        return
    name, pw = os.environ.get("ADMIN_USERNAME", "").strip(), os.environ.get("ADMIN_PASSWORD", "")
    if name and pw:
        try:
            s.create(name, pw, is_admin=True)
            log.warning("Created admin account '%s' from ADMIN_USERNAME/ADMIN_PASSWORD. "
                        "You can remove ADMIN_PASSWORD from your .env now.", name)
        except ValueError as e:
            log.error("Could not create the admin from ADMIN_USERNAME/ADMIN_PASSWORD: %s", e)
    else:
        log.warning("No user accounts exist yet. Set ADMIN_USERNAME and ADMIN_PASSWORD in .env and restart, "
                    "or run: docker compose exec app python -m app.users add <name> --admin")


def require_user() -> User:
    user = current_user.get()
    if user is None:
        raise HTTPException(401, "Please log in")
    return user


# ---------------------------------------------------------------- cookies
def _cookie_secure(request: Request) -> bool:
    mode = os.environ.get("COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    return request.url.scheme == "https"


def _set_cookies(resp: Response, request: Request, token: str, csrf: str) -> None:
    secure = _cookie_secure(request)
    resp.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_SECONDS, httponly=True, secure=secure,
                    samesite="lax", path="/")
    # Readable by the page's JavaScript, which sends it back in X-CSRF-Token.
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_MAX_SECONDS, httponly=False, secure=secure,
                    samesite="lax", path="/")


def _clear_cookies(resp: Response) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")


# ---------------------------------------------------------------- middleware
# The service worker and web app manifest hold no data. Browsers fetch the
# manifest without cookies, so both must be reachable before login.
PUBLIC_PATHS = {"/login", "/api/auth/login", "/healthz", "/favicon.ico", "/sw.js", "/manifest.webmanifest"}
PUBLIC_PREFIXES = ("/static/",)
ADMIN_PREFIXES = ("/api/admin/", "/api/config/files/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https:; font-src 'self' data:; connect-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


def _security_headers(scheme: str, path: str) -> list[tuple[bytes, bytes]]:
    h = [
        (b"content-security-policy", CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]
    if scheme == "https" and os.environ.get("HSTS", "true").lower() not in ("0", "false", "no", "off"):
        h.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
    if path.startswith("/api/") or path in ("/", "/login", "/account"):
        h.append((b"cache-control", b"no-store"))
    return h


def _allowed_origins() -> set[str]:
    return {o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}


def _origin_ok(request: Request) -> bool:
    """Reject cross-site requests. Browsers send Origin on every fetch that changes something."""
    origin = request.headers.get("origin")
    if origin is None:
        ref = request.headers.get("referer")
        if not ref:
            return True  # not a browser (curl, tests); the CSRF token still protects these
        parts = urlsplit(ref)
        origin = f"{parts.scheme}://{parts.netloc}"
    origin = origin.rstrip("/").lower()
    if origin in _allowed_origins():
        return True
    # Compare host names only: a proxy may drop a non-standard port from Host.
    host = (request.headers.get("host") or "").lower()
    host = host[1:host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    try:
        origin_host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return bool(host) and origin_host == host


class AuthMiddleware:
    """Pure ASGI middleware, so the logged-in user is visible to endpoints and to the
    background jobs they start (context variables are copied into new tasks)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                request = Request(scope)
                session = store().get_session(request.cookies.get(SESSION_COOKIE))
                if session is None or not _origin_ok(request):
                    await send({"type": "websocket.close", "code": 4401})
                    return
                t1, t2 = current_user.set(session.user), current_session.set(session)
                try:
                    return await self.app(scope, receive, send)
                finally:
                    current_user.reset(t1)
                    current_session.reset(t2)
            return await self.app(scope, receive, send)

        request = Request(scope)
        path = scope["path"]
        extra = _security_headers(scope.get("scheme", "http"), path)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(k, v) for k, v in extra if k not in names]
            await send(message)

        async def reply(resp: Response):
            await resp(scope, receive, send_with_headers)

        method = scope["method"].upper()
        if method in UNSAFE_METHODS and not _origin_ok(request):
            return await reply(JSONResponse({"detail": "Cross-site request blocked"}, status_code=403))

        public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
        session = store().get_session(request.cookies.get(SESSION_COOKIE))
        if session is None and not public:
            if path.startswith("/api/"):
                resp = JSONResponse({"detail": "Please log in"}, status_code=401)
                _clear_cookies(resp)
                return await reply(resp)
            return await reply(RedirectResponse("/login", status_code=303))

        if session is not None and method in UNSAFE_METHODS and path != "/api/auth/login":
            sent = request.headers.get(CSRF_HEADER, "")
            if not hmac.compare_digest(sent.encode(), session.csrf.encode()):
                return await reply(JSONResponse({"detail": "Missing or invalid CSRF token. Reload the page."},
                                                status_code=403))

        if session is not None and path.startswith(ADMIN_PREFIXES) and not session.user.is_admin:
            return await reply(JSONResponse({"detail": "Only admins can do that"}, status_code=403))

        t1 = current_user.set(session.user if session else None)
        t2 = current_session.set(session)
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            current_user.reset(t1)
            current_session.reset(t2)


# ---------------------------------------------------------------- routes
router = APIRouter()


class LoginBody(BaseModel):
    username: str
    password: str


class PasswordBody(BaseModel):
    current_password: str
    new_password: str


class NewUserBody(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserPatch(BaseModel):
    password: str | None = None
    is_admin: bool | None = None
    disabled: bool | None = None


def _client_ip(request: Request) -> str:
    # request.client already holds the real client address when uvicorn runs with
    # --proxy-headers and the proxy's IP is in FORWARDED_ALLOW_IPS.
    return request.client.host if request.client else "unknown"


@router.get("/login")
async def login_page():
    return FileResponse(STATIC / "login.html")


@router.get("/account")
async def account_page():
    return FileResponse(STATIC / "account.html")


@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.post("/api/auth/login")
async def login(body: LoginBody, request: Request):
    ip, name = _client_ip(request), body.username.strip().lower()[:64]
    wait = limiter.retry_after(ip, name)
    if wait:
        return JSONResponse({"detail": f"Too many failed logins. Try again in {wait // 60 + 1} minutes."},
                            status_code=429, headers={"Retry-After": str(wait)})
    user = store().authenticate(name, body.password[:MAX_PASSWORD_LENGTH])
    if user is None:
        limiter.failed(ip, name)
        log.warning("Failed login for %r from %s", name, ip)
        return JSONResponse({"detail": "Wrong username or password"}, status_code=401)
    limiter.succeeded(name)
    old = request.cookies.get(SESSION_COOKIE)
    if old:  # never reuse a session id across logins
        store().delete_session(_token_hash(old))
    token, csrf = store().create_session(user)
    resp = JSONResponse({"ok": True, "user": user.public()})
    _set_cookies(resp, request, token, csrf)
    log.info("Login: %s from %s", user.username, ip)
    return resp


@router.post("/api/auth/logout")
async def logout():
    s = current_session.get()
    if s:
        store().delete_session(s.token_hash)
    resp = JSONResponse({"ok": True})
    _clear_cookies(resp)
    return resp


@router.get("/api/auth/me")
async def me():
    return require_user().public()


@router.post("/api/auth/password")
async def change_password(body: PasswordBody, request: Request):
    user = require_user()
    if store().authenticate(user.username, body.current_password) is None:
        raise HTTPException(400, "Current password is wrong")
    store().set_password(user.id, body.new_password)  # signs out every session ...
    token, csrf = store().create_session(user)  # ... so give this browser a fresh one
    resp = JSONResponse({"ok": True})
    _set_cookies(resp, request, token, csrf)
    return resp


@router.get("/api/admin/users")
async def admin_list_users():
    return [u.public() for u in store().list_users()]


@router.post("/api/admin/users")
async def admin_create_user(body: NewUserBody):
    return store().create(body.username, body.password, body.is_admin).public()


@router.patch("/api/admin/users/{user_id}")
async def admin_update_user(user_id: int, body: UserPatch):
    s = store()
    if s.get(user_id) is None:
        raise HTTPException(404, "user not found")
    if body.password:
        s.set_password(user_id, body.password)
    s.update(user_id, is_admin=body.is_admin, disabled=body.disabled)
    return s.get(user_id).public()


@router.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: int):
    me_ = require_user()
    if me_.id == user_id:
        raise HTTPException(400, "You cannot delete your own account")
    s = store()
    if s.get(user_id) is None:
        raise HTTPException(404, "user not found")
    folder = s.user_dir(s.get(user_id))
    s.delete(user_id)
    return {"ok": True, "note": f"Their books were kept on disk in {folder}"}
