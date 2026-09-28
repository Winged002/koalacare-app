"""MongoDB connection and index declarations."""

from __future__ import annotations

import os

from pymongo import ASCENDING, MongoClient

DEFAULT_URI = "mongodb://127.0.0.1:27017"
DEFAULT_DB = "koalacare"

# Unique indexes are the real arbiter of correctness (one membership per person,
# one dose mark per slot). Indexes are created on boot; there are no TTL indexes
# because expiry is enforced in application code, which keeps behaviour
# identical against the in-memory test double.
INDEXES = {
    "users": [("username", {"unique": True}), ("email", {})],
    "sessions": [("token_hash", {"unique": True}), ("user_id", {})],
    "circles": [("created_by", {}), ("archived", {})],
    "circle_members": [
        ("circle_id", {}),
        ("user_id", {}),
        ("invited_email", {}),
    ],
    "circle_invites": [("token_hash", {"unique": True}), ("circle_id", {})],
    "people": [("circle_id", {}), ("kind", {})],
    "updates": [("circle_id", {}), ("created_at", {})],
    "medications": [("circle_id", {}), ("active", {})],
    "medication_changes": [("circle_id", {}), ("medication_id", {})],
    "dose_marks": [
        ("circle_id", {}),
        ("medication_id", {}),
        ("on_date", {}),
        ("slot", {}),
    ],
    "appointments": [("circle_id", {}), ("starts_at", {}), ("status", {})],
    "tasks": [("circle_id", {}), ("owner_user_id", {}), ("status", {}), ("due_at", {})],
    "documents": [("circle_id", {}), ("kind", {}), ("created_at", {})],
    "notifications": [("user_id", {}), ("created_at", {}), ("dedupe_key", {})],
    "seen": [("user_id", {}), ("circle_id", {})],
    "proposals": [("circle_id", {}), ("created_at", {})],
    "audit_log": [("ts", {}), ("circle_id", {})],
}

# Collections that must never contain two identical business keys.
UNIQUE_PAIRS = (
    ("circle_members", ("circle_id", "user_id")),
    ("dose_marks", ("circle_id", "medication_id", "on_date", "slot")),
    ("seen", ("user_id", "circle_id")),
)


def unique_indexes() -> list[tuple[str, tuple[str, ...]]]:
    return list(UNIQUE_PAIRS)


class Database:
    def __init__(self, uri: str | None = None, name: str | None = None, client=None) -> None:
        self.uri = uri or os.environ.get("MONGODB_URI") or DEFAULT_URI
        self.name = name or os.environ.get("MONGODB_DB") or DEFAULT_DB
        self._client = client
        self._db = None
        self.ready = False
        self.error = ""

    @property
    def client(self):
        if self._client is None:
            self._client = MongoClient(
                self.uri, serverSelectionTimeoutMS=2000, tz_aware=True
            )
        return self._client

    @property
    def db(self):
        if self._db is None:
            self._db = self.client[self.name]
        return self._db

    def __getitem__(self, collection: str):
        return self.db[collection]

    def connect(self) -> bool:
        try:
            self.ensure_indexes()
            self.ready = True
            self.error = ""
        except Exception as exc:  # noqa: BLE001 - any driver or transport failure
            self.ready = False
            self.error = str(exc)
        return self.ready

    def ensure_indexes(self) -> None:
        for collection, specs in INDEXES.items():
            handle = self.db[collection]
            for field, options in specs:
                handle.create_index([(field, ASCENDING)], **options)
        for collection, fields in UNIQUE_PAIRS:
            self.db[collection].create_index(
                [(field, ASCENDING) for field in fields], unique=True
            )

    def ping(self) -> bool:
        try:
            self.db["users"].count_documents({})
            return True
        except Exception:  # noqa: BLE001
            return False
