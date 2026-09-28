"""Accounts: creation, lookup, authentication."""

from __future__ import annotations

import re

from pymongo.errors import DuplicateKeyError

from .security import hash_password, verify_password
from .timing import DEFAULT_TIMEZONE, utcnow

USERNAME_MIN = 3
USERNAME_MAX = 32
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$")


def normalise_username(value: str) -> str:
    return (value or "").strip().lower()[:USERNAME_MAX]


def normalise_email(value: str) -> str:
    return (value or "").strip().lower()[:254]


def username_problem(value: str) -> str:
    if len(value) < USERNAME_MIN:
        return f"Usernames need at least {USERNAME_MIN} characters."
    if len(value) > USERNAME_MAX:
        return f"Keep usernames to {USERNAME_MAX} characters or fewer."
    if not all(ch.isalnum() or ch in "._-" for ch in value):
        return "Use letters, numbers, dots, dashes or underscores only."
    return ""


def email_problem(value: str) -> str:
    if not value:
        return "Enter an email address."
    if not EMAIL_RE.match(value):
        return "That email address does not look complete."
    return ""


def find_by_username(db, username: str):
    return db["users"].find_one({"username": normalise_username(username)})


def find_by_email(db, email: str):
    normalised = normalise_email(email)
    if not normalised:
        return None
    return db["users"].find_one({"email": normalised})


def find_by_id(db, user_id):
    return db["users"].find_one({"_id": user_id})


def display_name(user) -> str:
    if not user:
        return "Someone"
    return user.get("name") or user.get("username", "someone")


def create_user(
    db,
    *,
    username: str,
    email: str,
    password: str,
    name: str = "",
    timezone: str = DEFAULT_TIMEZONE,
):
    """Return (user, error). Uniqueness is enforced here and by index."""
    username = normalise_username(username)
    email = normalise_email(email)
    problem = username_problem(username) or email_problem(email)
    if problem:
        return None, problem
    if find_by_username(db, username):
        return None, "That username is already taken."
    if find_by_email(db, email):
        return None, "That email address already has an account."

    now = utcnow()
    doc = {
        "username": username,
        "email": email,
        "name": (name or username)[:80],
        "timezone": timezone or DEFAULT_TIMEZONE,
        "password_hash": hash_password(password),
        "created_at": now,
        "updated_at": now,
        "last_login_at": None,
    }
    try:
        result = db["users"].insert_one(doc)
    except DuplicateKeyError:
        # Two requests can pass the lookup above at the same moment; the unique
        # index is the real arbiter, so translate the race into a plain message.
        return None, "That username is already taken."
    doc["_id"] = result.inserted_id
    return doc, ""


def authenticate(db, username: str, password: str):
    """Return the user, or None. Never reveals which part was wrong."""
    user = find_by_username(db, username)
    stored = user.get("password_hash") if user else None
    if not verify_password(stored, password):
        return None
    if not user:
        return None
    db["users"].update_one({"_id": user["_id"]}, {"$set": {"last_login_at": utcnow()}})
    return user


def set_password(db, user_id, password: str) -> None:
    db["users"].update_one(
        {"_id": user_id},
        {"$set": {"password_hash": hash_password(password), "updated_at": utcnow()}},
    )


def update_profile(db, user_id, *, name: str = None, timezone: str = None) -> None:
    changes = {"updated_at": utcnow()}
    if name:
        changes["name"] = name[:80]
    if timezone:
        changes["timezone"] = timezone
    db["users"].update_one({"_id": user_id}, {"$set": changes})
