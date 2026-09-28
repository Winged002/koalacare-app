"""Circles, membership, roles, the people directory.

Access is modelled as a small, readable capability matrix rather than a pile of
per-route checks. A neighbour picking Margaret up on Thursday holds the
``neighbour`` role, which carries exactly two capabilities, so there is no
route that can accidentally hand them a medical history.
"""

from __future__ import annotations

from datetime import timedelta

from bson import ObjectId
from flask import abort, g

from .security import hash_token, new_token, record_audit
from .timing import DEFAULT_TIMEZONE, as_utc, utcnow

# --------------------------------------------------------------------- roles

OWNER = "owner"
ROLES = ("owner", "family", "carer", "clinician", "neighbour")

ROLE_LABELS = {
    "owner": "Organiser",
    "family": "Family",
    "carer": "Carer",
    "clinician": "Doctor or clinic",
    "neighbour": "Helper",
}

ROLE_BLURB = {
    "owner": "Runs the circle. Sees everything, including who else has access.",
    "family": "Everything except restricted documents such as powers of attorney.",
    "carer": "Today, medications, tasks and the notes needed to do the visit.",
    "clinician": "Medication list, medical documents and appointment notes.",
    "neighbour": "Only the tasks assigned to them.",
}

_ALL = frozenset(
    {
        "view_today",
        "view_updates",
        "post_updates",
        "view_medications",
        "manage_medications",
        "view_appointments",
        "manage_appointments",
        "view_tasks",
        "manage_tasks",
        "view_documents",
        "manage_documents",
        "view_people",
        "manage_people",
        "view_load",
        "view_changes",
        "use_handover",
        "use_extract",
        "invite_members",
        "manage_members",
        "manage_circle",
    }
)

CAPABILITIES = {
    "owner": _ALL,
    # Family can do the work of caring; only the organiser changes who is in.
    "family": _ALL
    - {
        "manage_members",
        "manage_circle",
        "manage_people",
    },
    "carer": frozenset(
        {
            "view_today",
            "view_updates",
            "post_updates",
            "view_medications",
            "view_appointments",
            "view_tasks",
            "manage_tasks",
            "use_handover",
        }
    ),
    "clinician": frozenset(
        {
            "view_today",
            "view_updates",
            "post_updates",
            "view_medications",
            "view_appointments",
            "view_documents",
            "view_people",
        }
    ),
    "neighbour": frozenset({"view_today", "view_tasks"}),
}

# Plain-language table for the access screen: what each role can actually reach.
ACCESS_TABLE = (
    ("Today", {"owner", "family", "carer", "clinician", "neighbour"}),
    ("Updates", {"owner", "family", "carer", "clinician"}),
    ("Medications", {"owner", "family", "carer", "clinician"}),
    ("Appointments", {"owner", "family", "carer", "clinician"}),
    ("Tasks", {"owner", "family", "carer", "neighbour"}),
    ("Documents", {"owner", "family", "clinician"}),
    ("Restricted documents", {"owner"}),
    ("People directory", {"owner", "family", "clinician"}),
    ("Invite others", {"owner", "family"}),
    ("Manage who has access", {"owner"}),
)


def can(role: str | None, capability: str) -> bool:
    return bool(role) and capability in CAPABILITIES.get(role, frozenset())


def capabilities(role: str | None) -> frozenset:
    return CAPABILITIES.get(role or "", frozenset())


# ------------------------------------------------------------------ helpers


def oid(value):
    """Parse an ObjectId from a URL segment, or None. Never raises."""
    if isinstance(value, ObjectId):
        return value
    if not value or not isinstance(value, str) or not ObjectId.is_valid(value):
        return None
    return ObjectId(value)


def membership(db, circle_id, user_id):
    return db["circle_members"].find_one(
        {"circle_id": circle_id, "user_id": user_id, "status": "active"}
    )


def role_of(db, circle_id, user_id) -> str | None:
    found = membership(db, circle_id, user_id)
    return found.get("role") if found else None


def require_circle(db, circle_id_value: str):
    """Return (circle, membership) or abort. Same answer whether the circle is
    missing or simply not yours, so it cannot be used to probe for existence."""
    circle_id = oid(circle_id_value)
    user = g.user
    if circle_id is None or user is None:
        abort(404)
    found = membership(db, circle_id, user["_id"])
    circle = db["circles"].find_one({"_id": circle_id})
    if not circle or not found:
        abort(404)
    return circle, found


def require_capability(db, circle_id_value: str, capability: str):
    """Authorisation at the mutation boundary, not merely at sign-in."""
    circle, found = require_circle(db, circle_id_value)
    if not can(found.get("role"), capability):
        abort(403)
    return circle, found


def touch_circle(db, circle_id) -> None:
    db["circles"].update_one({"_id": circle_id}, {"$set": {"updated_at": utcnow()}})


# ------------------------------------------------------------------ circles


def create_circle(db, user, *, name: str, subject_name: str = "", timezone: str = ""):
    now = utcnow()
    result = db["circles"].insert_one(
        {
            "name": name[:80],
            "subject_name": (subject_name or "")[:80],
            "timezone": timezone or user.get("timezone") or DEFAULT_TIMEZONE,
            "created_by": user["_id"],
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
    )
    db["circle_members"].insert_one(
        {
            "circle_id": result.inserted_id,
            "user_id": user["_id"],
            "invited_email": user.get("email", ""),
            "role": OWNER,
            "status": "active",
            "invited_by": user["_id"],
            "joined_at": now,
        }
    )
    record_audit(db, result.inserted_id, "circle.create", target=name)
    return db["circles"].find_one({"_id": result.inserted_id})


def circles_for_user(db, user_id) -> list:
    memberships = list(
        db["circle_members"].find({"user_id": user_id, "status": "active"})
    )
    circles = []
    for found in memberships:
        circle = db["circles"].find_one({"_id": found["circle_id"]})
        if not circle:
            continue
        circle["role"] = found.get("role")
        circles.append(circle)
    circles.sort(key=lambda item: item.get("name", ""))
    return circles


def default_circle(db, user):
    circles = circles_for_user(db, user["_id"])
    return circles[0] if circles else None


def members_of(db, circle_id) -> list:
    rows = list(
        db["circle_members"].find({"circle_id": circle_id, "status": "active"}).sort(
            "joined_at", 1
        )
    )
    for row in rows:
        account = db["users"].find_one({"_id": row.get("user_id")})
        row["user"] = account
        row["label"] = (account or {}).get("name") or (account or {}).get(
            "username", row.get("invited_email", "Invited")
        )
    return rows


def set_role(db, circle_id, member_id, role: str) -> bool:
    if role not in ROLES or role == OWNER:
        return False
    target = db["circle_members"].find_one({"_id": member_id, "circle_id": circle_id})
    if not target or target.get("role") == OWNER:
        # The organiser cannot be demoted out of their own circle.
        return False
    db["circle_members"].update_one(
        {"_id": member_id}, {"$set": {"role": role, "updated_at": utcnow()}}
    )
    return True


def remove_member(db, circle_id, member_id) -> bool:
    target = db["circle_members"].find_one({"_id": member_id, "circle_id": circle_id})
    if not target or target.get("role") == OWNER:
        return False
    db["circle_members"].update_one(
        {"_id": member_id}, {"$set": {"status": "removed", "removed_at": utcnow()}}
    )
    return True


# ------------------------------------------------------------------ invites

INVITE_DAYS = 14


def invite(db, circle, *, email: str, role: str, created_by):
    role = role if role in ROLES and role != OWNER else "family"
    raw = new_token()
    now = utcnow()
    db["circle_invites"].insert_one(
        {
            "circle_id": circle["_id"],
            "email": email,
            "role": role,
            "token_hash": hash_token(raw),
            "created_by": created_by,
            "created_at": now,
            "expires_at": now + timedelta(days=INVITE_DAYS),
            "accepted_at": None,
        }
    )
    record_audit(db, circle["_id"], "circle.invite", target=email, meta={"role": role})
    return raw


def invite_state(db, token: str):
    """Return (invite, circle, state) where state is open/used/expired/unknown."""
    if not token:
        return None, None, "unknown"
    found = db["circle_invites"].find_one({"token_hash": hash_token(token)})
    if not found:
        return None, None, "unknown"
    circle = db["circles"].find_one({"_id": found["circle_id"]})
    if not circle:
        return found, None, "unknown"
    if found.get("accepted_at"):
        return found, circle, "used"
    expires = as_utc(found.get("expires_at"))
    if expires and expires <= utcnow():
        return found, circle, "expired"
    return found, circle, "open"


def accept_invite(db, found, circle, user) -> bool:
    """Join the circle. Unique on (circle, user), so a double submit is a no-op."""
    existing = membership(db, circle["_id"], user["_id"])
    if existing:
        db["circle_invites"].update_one(
            {"_id": found["_id"]}, {"$set": {"accepted_at": utcnow()}}
        )
        return False
    now = utcnow()
    db["circle_members"].insert_one(
        {
            "circle_id": circle["_id"],
            "user_id": user["_id"],
            "invited_email": user.get("email", ""),
            "role": found.get("role", "family"),
            "status": "active",
            "invited_by": found.get("created_by"),
            "joined_at": now,
        }
    )
    db["circle_invites"].update_one(
        {"_id": found["_id"]}, {"$set": {"accepted_at": now}}
    )
    return True


def pending_invites(db, circle_id) -> list:
    return list(
        db["circle_invites"].find({"circle_id": circle_id, "accepted_at": None}).sort(
            "created_at", -1
        )
    )


def invites_waiting_for(db, email: str) -> list:
    if not email:
        return []
    return list(
        db["circle_invites"].find({"email": email, "accepted_at": None}).sort(
            "created_at", -1
        )
    )


# ------------------------------------------------------------------- people

PERSON_KINDS = (
    ("gp", "GP"),
    ("specialist", "Specialist"),
    ("pharmacy", "Pharmacy"),
    ("carer", "Paid carer"),
    ("family", "Family"),
    ("emergency", "Emergency contact"),
    ("other", "Other"),
)

PERSON_LABELS = dict(PERSON_KINDS)


def people_of(db, circle_id, kinds=None) -> list:
    selector = {"circle_id": circle_id}
    if kinds:
        selector["kind"] = {"$in": list(kinds)}
    return list(db["people"].find(selector).sort("name", 1))


def add_person(db, circle_id, *, name, kind, org="", phone="", email="", notes="", created_by):
    now = utcnow()
    db["people"].insert_one(
        {
            "circle_id": circle_id,
            "name": name[:80],
            "kind": kind if kind in PERSON_LABELS else "other",
            "org": org[:80],
            "phone": phone[:40],
            "email": email[:120],
            "notes": notes[:400],
            "created_by": created_by,
            "created_at": now,
        }
    )


def remove_person(db, circle_id, person_id) -> bool:
    result = db["people"].delete_one({"_id": person_id, "circle_id": circle_id})
    return result.deleted_count == 1


# ---------------------------------------------------------------- documents

DOCUMENT_KINDS = (
    ("prescription", "Prescription"),
    ("discharge", "Discharge letter"),
    ("lab", "Lab result"),
    ("referral", "Referral"),
    ("care_plan", "Care plan"),
    ("insurance", "Insurance"),
    ("poa", "Power of attorney"),
    ("other", "Other"),
)

DOCUMENT_LABELS = dict(DOCUMENT_KINDS)

# Documents a family member should not automatically see.
RESTRICTED_KINDS = frozenset({"poa"})


def document_visible(document, role: str | None, user_id) -> bool:
    """Restricted documents belong to the organiser and whoever filed them."""
    if not document.get("restricted") and document.get("kind") not in RESTRICTED_KINDS:
        return True
    return role == OWNER or document.get("uploaded_by") == user_id


def visible_documents(db, circle_id, role: str | None, user_id) -> list:
    rows = list(db["documents"].find({"circle_id": circle_id}).sort("created_at", -1))
    return [row for row in rows if document_visible(row, role, user_id)]
