"""Accounts, sessions, CSRF, rate limiting and the audit trail."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import abort, current_app, g, redirect, request, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .timing import as_utc, utcnow

SESSION_COOKIE = "kc_app"
SESSION_HOURS = 24
PASSWORD_MIN = 10


# ------------------------------------------------------------------ passwords


def hash_password(password: str) -> str:
    return generate_password_hash(password)


_dummy_hash: str | None = None


def _dummy() -> str:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = generate_password_hash("koalacare-timing-equaliser")
    return _dummy_hash


def verify_password(stored: str | None, password: str) -> bool:
    """Always spend a hash comparison so failure timing cannot reveal whether
    the account exists."""
    if not stored:
        check_password_hash(_dummy(), password)
        return False
    return check_password_hash(stored, password)


def password_problem(password: str, *, username: str = "", email: str = "") -> str:
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password.encode("utf-8")) > 1024:
        return "That password is too long."
    lowered = password.lower()
    if username and lowered == username.lower():
        return "The password cannot be your username."
    if email and lowered == email.lower():
        return "The password cannot be your email address."
    return ""


# --------------------------------------------------------------------- tokens


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_token(raw: str, stored_hash: str) -> bool:
    return bool(raw) and hmac.compare_digest(hash_token(raw), stored_hash or "")


# ------------------------------------------------------------------ requests


def client_ip() -> str:
    """The last X-Forwarded-For entry is the one the edge proxy appended.

    Returns "system" when there is no request in flight, so background work such
    as first-boot seeding can be audited without a request context.
    """
    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
    except RuntimeError:
        return "system"
    if forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1][:64]
    try:
        return (request.remote_addr or "unknown")[:64]
    except RuntimeError:
        return "system"


def is_cross_site() -> bool:
    """Fail-closed origin check for state-changing requests."""
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return True
    origin = request.headers.get("Origin")
    if not origin:
        return False
    return urlparse(origin).netloc != request.host


# ------------------------------------------------------------------ sessions


def session_cookie_name() -> str:
    return current_app.config["KC_SESSION_COOKIE"]

def start_session(db, user) -> str:
    raw = new_token()
    now = utcnow()
    db["sessions"].insert_one(
        {
            "token_hash": hash_token(raw),
            "user_id": user["_id"],
            "created_at": now,
            "last_seen_at": now,
            "expires_at": now + timedelta(hours=SESSION_HOURS),
            "revoked_at": None,
            "csrf": new_token(),
            "ip": client_ip(),
        }
    )
    return raw


def load_identity(db) -> None:
    """Resolve the caller from the cookie on every request."""
    g.session = None
    g.user = None
    g.csrf = ""
    raw = request.cookies.get(session_cookie_name(), "")
    if not raw:
        return
    doc = db["sessions"].find_one({"token_hash": hash_token(raw), "revoked_at": None})
    if not doc:
        return
    now = utcnow()
    expires = as_utc(doc.get("expires_at"))
    if expires is None or expires <= now:
        return
    user = db["users"].find_one({"_id": doc["user_id"]})
    if not user:
        return
    g.session = doc
    g.user = user
    g.csrf = doc.get("csrf", "")
    last = as_utc(doc.get("last_seen_at"))
    if last is None or (now - last).total_seconds() > 60:
        db["sessions"].update_one({"_id": doc["_id"]}, {"$set": {"last_seen_at": now}})


def end_session(db) -> None:
    doc = getattr(g, "session", None)
    if doc:
        db["sessions"].update_one({"_id": doc["_id"]}, {"$set": {"revoked_at": utcnow()}})
    g.session = None
    g.user = None


def apply_session_cookie(response, raw: str):
    response.set_cookie(
        session_cookie_name(),
        raw,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        secure=current_app.config["KC_COOKIE_SECURE"],
        samesite="Lax",
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(session_cookie_name(), path="/")
    return response


def current_user():
    """The signed-in user, or None.

    Safe to call outside a request context, which is what lets first-boot
    seeding write an audit entry without a caller.
    """
    try:
        return getattr(g, "user", None)
    except RuntimeError:
        return None


# ------------------------------------------------------------------ security


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            target = url_for("auth.login")
            if request.method == "GET":
                target = url_for("auth.login", next=request.full_path)
            return redirect(target)
        return view(*args, **kwargs)

    return wrapper


def safe_next(raw: str | None, default: str) -> str:
    """Only ever redirect to a local, single-slash path."""
    if not raw or len(raw) > 512:
        return default
    if any(ch in raw for ch in "\r\n\t\\"):
        return default
    if not raw.startswith("/") or raw.startswith("//"):
        return default
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        return default
    return raw


def csrf_token() -> str:
    return getattr(g, "csrf", "") or ""


def csrf_ok() -> bool:
    doc = getattr(g, "session", None)
    if not doc:
        return False
    submitted = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    if not submitted:
        return False
    return hmac.compare_digest(submitted, doc.get("csrf", ""))


def require_csrf() -> None:
    if not csrf_ok():
        abort(403)


class RateLimiter:
    """Small in-process sliding-window limiter."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(now)
            self._hits[key] = recent
            if len(self._hits) > 4096:
                for stale in [
                    k for k, v in self._hits.items() if not v or now - v[-1] > self.window
                ]:
                    self._hits.pop(stale, None)
            return True


# --------------------------------------------------------------------- audit


def record_audit(db, circle_id, action: str, target: str = "", meta: dict | None = None) -> None:
    actor = current_user()
    db["audit_log"].insert_one(
        {
            "ts": utcnow(),
            "circle_id": circle_id,
            "actor_id": actor["_id"] if actor else None,
            "actor_name": actor.get("username", "anonymous") if actor else "anonymous",
            "action": action,
            "target": target,
            "meta": meta or {},
            "ip": client_ip(),
        }
    )


# --------------------------------------------------------- one-time reveals


def set_reveal(db, payload: dict) -> None:
    """Park a freshly minted invite link on the session document.

    There is no mailer, so the link has to be handed over by hand. Keeping the
    raw token server-side rather than in a redirect URL keeps it out of access
    logs and browser history.
    """
    doc = getattr(g, "session", None)
    if doc:
        db["sessions"].update_one({"_id": doc["_id"]}, {"$set": {"reveal": payload}})


def take_reveal(db):
    doc = getattr(g, "session", None)
    if not doc:
        return None
    payload = doc.get("reveal")
    if payload:
        db["sessions"].update_one({"_id": doc["_id"]}, {"$unset": {"reveal": ""}})
    return payload
