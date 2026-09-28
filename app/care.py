"""The care record: updates, medications, appointments, tasks, documents,
notifications, the “what changed” summary and the care-load view.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta

from pymongo.errors import DuplicateKeyError

from . import domain
from .domain import can, touch_circle
from .timing import (
    as_utc,
    day_bounds_utc,
    local_day,
    today_local,
    utcnow,
)

UPLOAD_DIR = os.environ.get("KOALACARE_UPLOAD_DIR") or "/data/uploads"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_UPLOADS = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "txt": "text/plain",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAGIC = {
    "pdf": b"%PDF",
    "png": b"\x89PNG\r\n\x1a\n",
    "jpg": b"\xff\xd8\xff",
    "jpeg": b"\xff\xd8\xff",
}

# --------------------------------------------------------------------- kinds

UPDATE_KINDS = (
    ("note", "Note"),
    ("observation", "Observation"),
    ("medication", "Medication"),
    ("appointment", "Appointment"),
    ("task", "Task"),
    ("communication", "Communication"),
    ("handover", "Visit handover"),
    ("document", "Document"),
)
UPDATE_LABELS = dict(UPDATE_KINDS)

ITEM_TYPES = ("medication", "observation", "task", "communication", "appointment")


# -------------------------------------------------------------- notifications


def notify(
    db,
    recipient_id,
    *,
    title: str,
    body: str = "",
    link: str = "",
    circle_id=None,
    actor_id=None,
    dedupe_key: str | None = None,
) -> None:
    """Never notify someone about their own action, and never notify twice."""
    if not recipient_id or recipient_id == actor_id:
        return
    if dedupe_key and db["notifications"].find_one({"dedupe_key": dedupe_key}):
        return
    db["notifications"].insert_one(
        {
            "user_id": recipient_id,
            "circle_id": circle_id,
            "title": title[:120],
            "body": body[:240],
            "link": link,
            "read": False,
            "dedupe_key": dedupe_key,
            "created_at": utcnow(),
        }
    )


def notify_circle(db, circle, actor_id, *, title, body="", link="", dedupe_key=None):
    for member in db["circle_members"].find(
        {"circle_id": circle["_id"], "status": "active"}
    ):
        notify(
            db,
            member.get("user_id"),
            title=title,
            body=body,
            link=link,
            circle_id=circle["_id"],
            actor_id=actor_id,
            dedupe_key=f"{dedupe_key}:{member.get('user_id')}" if dedupe_key else None,
        )


def notifications_for(db, user_id, limit: int = 50) -> list:
    return list(
        db["notifications"].find({"user_id": user_id}).sort("created_at", -1).limit(limit)
    )


def unread_count(db, user_id) -> int:
    return db["notifications"].count_documents({"user_id": user_id, "read": False})


def mark_notifications_read(db, user_id) -> None:
    db["notifications"].update_many(
        {"user_id": user_id, "read": False}, {"$set": {"read": True}}
    )


# ------------------------------------------------------------------- updates


def post_update(
    db,
    circle,
    author,
    *,
    body: str,
    kind: str = "note",
    title: str = "",
    items: list | None = None,
    source: str = "manual",
):
    now = utcnow()
    doc = {
        "circle_id": circle["_id"],
        "author_id": author["_id"],
        "author_name": author.get("name") or author.get("username"),
        "kind": kind if kind in UPDATE_LABELS else "note",
        "title": (title or "")[:120],
        "body": (body or "")[:4000],
        "items": [
            {
                "type": item.get("type", "observation"),
                "text": str(item.get("text", ""))[:400],
            }
            for item in (items or [])
            if str(item.get("text", "")).strip()
        ],
        "source": source,
        "created_at": now,
    }
    result = db["updates"].insert_one(doc)
    doc["_id"] = result.inserted_id
    touch_circle(db, circle["_id"])
    notify_circle(
        db,
        circle,
        author["_id"],
        title=f"New update in {circle['name']}",
        body=doc["body"][:140],
        link=f"/updates?circle={circle['_id']}",
    )
    return doc


def updates_of(db, circle_id, limit: int = 60, kind: str | None = None) -> list:
    selector = {"circle_id": circle_id}
    if kind:
        selector["kind"] = kind
    return list(db["updates"].find(selector).sort("created_at", -1).limit(limit))


# -------------------------------------------------------------- medications


def add_medication(db, circle, user, *, name, strength="", form="", dose="", times=None, purpose="", prescriber_id=None, repeat_due=None, supply_left=None):
    now = utcnow()
    doc = {
        "circle_id": circle["_id"],
        "name": name[:80],
        "strength": strength[:40],
        "form": form[:40],
        "dose": dose[:80],
        "times": [t for t in (times or []) if t][:8],
        "purpose": purpose[:160],
        "prescriber_id": prescriber_id,
        "repeat_due": repeat_due,
        "supply_left": supply_left,
        "active": True,
        "started_at": now,
        "created_by": user["_id"],
        "created_at": now,
        "updated_at": now,
    }
    result = db["medications"].insert_one(doc)
    doc["_id"] = result.inserted_id
    record_change(db, circle, doc, user, "started", "", describe_medication(doc))
    touch_circle(db, circle["_id"])
    return doc


def describe_medication(med) -> str:
    parts = [med.get("name", "")]
    if med.get("strength"):
        parts.append(med["strength"])
    if med.get("dose"):
        parts.append(f"({med['dose']})")
    return " ".join(part for part in parts if part).strip()


MED_FIELDS = {
    "strength": "strength",
    "dose": "dose",
    "purpose": "purpose",
    "form": "form",
}


def update_medication(db, circle, user, medication_id, changes: dict, note: str = ""):
    """Apply changes and write a permanent history entry for each one."""
    med = db["medications"].find_one({"_id": medication_id, "circle_id": circle["_id"]})
    if not med:
        return None
    applied = {}
    for field, label in MED_FIELDS.items():
        if field in changes and changes[field] != med.get(field, ""):
            applied[field] = changes[field]
    if "times" in changes:
        new_times = [t for t in changes["times"] if t][:8]
        if new_times != med.get("times", []):
            applied["times"] = new_times
    if "supply_left" in changes and changes["supply_left"] != med.get("supply_left"):
        applied["supply_left"] = changes["supply_left"]
    if "repeat_due" in changes and changes["repeat_due"] != med.get("repeat_due"):
        applied["repeat_due"] = changes["repeat_due"]
    if not applied:
        return med

    applied["updated_at"] = utcnow()
    db["medications"].update_one({"_id": med["_id"]}, {"$set": applied})
    for field, value in applied.items():
        if field in ("updated_at",):
            continue
        old = med.get(field)
        record_change(
            db,
            circle,
            med,
            user,
            field,
            format_value(old),
            format_value(value),
            note=note,
        )
    touch_circle(db, circle["_id"])
    return db["medications"].find_one({"_id": med["_id"]})


def format_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


FIELD_LABELS = {
    "started": "Started",
    "stopped": "Stopped",
    "strength": "Strength",
    "dose": "Dose",
    "times": "Times",
    "purpose": "Purpose",
    "form": "Form",
    "supply_left": "Supply left",
    "repeat_due": "Repeat due",
}


def record_change(db, circle, med, user, field, old, new, note: str = "") -> None:
    db["medication_changes"].insert_one(
        {
            "circle_id": circle["_id"],
            "medication_id": med["_id"],
            "medication_name": med.get("name", ""),
            "field": field,
            "from_value": old[:160],
            "to_value": new[:160],
            "note": note[:400],
            "changed_by": user["_id"],
            "changed_by_name": user.get("name") or user.get("username"),
            "changed_at": utcnow(),
        }
    )


def stop_medication(db, circle, user, medication_id, note: str = "") -> bool:
    med = db["medications"].find_one({"_id": medication_id, "circle_id": circle["_id"]})
    if not med or not med.get("active"):
        return False
    db["medications"].update_one(
        {"_id": med["_id"]}, {"$set": {"active": False, "updated_at": utcnow()}}
    )
    record_change(db, circle, med, user, "stopped", "active", "stopped", note=note)
    touch_circle(db, circle["_id"])
    return True


def medications_of(db, circle_id, active_only: bool = True) -> list:
    selector = {"circle_id": circle_id}
    if active_only:
        selector["active"] = True
    return list(db["medications"].find(selector).sort("name", 1))


def changes_of(db, circle_id, limit: int = 80) -> list:
    return list(
        db["medication_changes"]
        .find({"circle_id": circle_id})
        .sort("changed_at", -1)
        .limit(limit)
    )


def doses_for_day(db, circle, on_date) -> list:
    """Every scheduled dose for a local calendar day, with its mark state."""
    marks = {
        (row["medication_id"], row["slot"]): row
        for row in db["dose_marks"].find(
            {"circle_id": circle["_id"], "on_date": on_date.isoformat()}
        )
    }
    rows = []
    for med in medications_of(db, circle["_id"]):
        for slot in med.get("times", []):
            mark = marks.get((med["_id"], slot))
            rows.append(
                {
                    "medication": med,
                    "slot": slot,
                    "taken": bool(mark and mark.get("status") == "taken"),
                    "mark": mark,
                }
            )
    rows.sort(key=lambda row: row["slot"])
    return rows


def mark_dose(db, circle, user, medication_id, on_date, slot: str, status: str = "taken"):
    if status not in ("taken", "skipped"):
        status = "taken"
    try:
        db["dose_marks"].update_one(
            {
                "circle_id": circle["_id"],
                "medication_id": medication_id,
                "on_date": on_date,
                "slot": slot,
            },
            {
                "$set": {
                    "status": status,
                    "marked_by": user["_id"],
                    "marked_by_name": user.get("name") or user.get("username"),
                    "marked_at": utcnow(),
                }
            },
            upsert=True,
        )
    except DuplicateKeyError:
        pass


def clear_dose(db, circle, medication_id, on_date, slot: str) -> None:
    db["dose_marks"].delete_one(
        {
            "circle_id": circle["_id"],
            "medication_id": medication_id,
            "on_date": on_date,
            "slot": slot,
        }
    )


# ------------------------------------------------------------- appointments


def add_appointment(db, circle, user, *, title, starts_at, location="", person_id=None, driver_user_id=None, duration_min=60, questions=None, bring=None):
    now = utcnow()
    doc = {
        "circle_id": circle["_id"],
        "title": title[:120],
        "starts_at": starts_at,
        "location": location[:160],
        "person_id": person_id,
        "driver_user_id": driver_user_id,
        "duration_min": duration_min or 60,
        "questions": [q[:200] for q in (questions or []) if q][:12],
        "bring": [b[:200] for b in (bring or []) if b][:12],
        "notes_after": "",
        "status": "upcoming",
        "created_by": user["_id"],
        "created_at": now,
        "updated_at": now,
    }
    result = db["appointments"].insert_one(doc)
    doc["_id"] = result.inserted_id
    touch_circle(db, circle["_id"])
    return doc


def appointments_of(db, circle_id, when: str = "upcoming", limit: int = 100) -> list:
    now = utcnow()
    selector = {"circle_id": circle_id}
    if when == "upcoming":
        selector["status"] = "upcoming"
        selector["starts_at"] = {"$gte": now - timedelta(hours=12)}
        return list(
            db["appointments"].find(selector).sort("starts_at", 1).limit(limit)
        )
    if when == "past":
        selector["$or"] = [
            {"starts_at": {"$lt": now - timedelta(hours=12)}},
            {"status": {"$in": ["completed", "cancelled"]}},
        ]
        return list(
            db["appointments"].find(selector).sort("starts_at", -1).limit(limit)
        )
    return list(db["appointments"].find(selector).sort("starts_at", -1).limit(limit))


def finish_appointment(db, circle, user, appointment_id, notes: str, status: str = "completed") -> bool:
    appt = db["appointments"].find_one({"_id": appointment_id, "circle_id": circle["_id"]})
    if not appt:
        return False
    db["appointments"].update_one(
        {"_id": appt["_id"]},
        {"$set": {"notes_after": notes[:4000], "status": status, "updated_at": utcnow()}},
    )
    if notes.strip():
        post_update(
            db,
            circle,
            user,
            body=notes,
            kind="appointment",
            title=f"After: {appt['title']}",
            source="appointment",
        )
    touch_circle(db, circle["_id"])
    return True


# --------------------------------------------------------------------- tasks


def add_task(db, circle, user, *, title, owner_user_id=None, due_at=None, priority="normal", source="manual") -> dict:
    now = utcnow()
    doc = {
        "circle_id": circle["_id"],
        "title": title[:160],
        "owner_user_id": owner_user_id,
        "due_at": due_at,
        "priority": priority if priority in ("normal", "soon") else "normal",
        "status": "open",
        "created_by": user["_id"],
        "created_at": now,
        "completed_at": None,
        "completed_by": None,
        "source": source,
    }
    result = db["tasks"].insert_one(doc)
    doc["_id"] = result.inserted_id
    touch_circle(db, circle["_id"])
    if owner_user_id and owner_user_id != user["_id"]:
        notify(
            db,
            owner_user_id,
            title="A task was assigned to you",
            body=title[:140],
            link=f"/tasks?circle={circle['_id']}",
            circle_id=circle["_id"],
            actor_id=user["_id"],
            dedupe_key=f"task:{result.inserted_id}",
        )
    return doc


def tasks_of(db, circle_id, status: str = "open", owner_user_id=None, query: str = "", limit: int = 200) -> list:
    selector = {"circle_id": circle_id}
    if status != "all":
        selector["status"] = status
    if owner_user_id:
        selector["owner_user_id"] = owner_user_id
    if query:
        selector["title"] = {"$regex": query, "$options": "i"}
    return list(db["tasks"].find(selector).sort("due_at", 1).limit(limit))


def tasks_visible_to(db, circle_id, role: str | None, user_id) -> list:
    """A helper sees only their own tasks. Everyone else sees the whole board."""
    if role == "neighbour":
        return tasks_of(db, circle_id, status="open", owner_user_id=user_id)
    return tasks_of(db, circle_id, status="open")


def set_task_status(db, circle, user, task_id, status: str) -> bool:
    if status not in ("open", "done", "cancelled"):
        return False
    task = db["tasks"].find_one({"_id": task_id, "circle_id": circle["_id"]})
    if not task:
        return False
    if status == "done":
        db["tasks"].update_one(
            {"_id": task["_id"]},
            {
                "$set": {
                    "status": "done",
                    "completed_at": utcnow(),
                    "completed_by": user["_id"],
                    "completed_by_name": user.get("name") or user.get("username"),
                }
            },
        )
    else:
        db["tasks"].update_one(
            {"_id": task["_id"]},
            {"$set": {"status": status, "completed_at": None, "completed_by": None}},
        )
    touch_circle(db, circle["_id"])
    return True


def assign_task(db, circle, user, task_id, owner_user_id) -> bool:
    task = db["tasks"].find_one({"_id": task_id, "circle_id": circle["_id"]})
    if not task:
        return False
    db["tasks"].update_one({"_id": task["_id"]}, {"$set": {"owner_user_id": owner_user_id}})
    if owner_user_id and owner_user_id != user["_id"]:
        notify(
            db,
            owner_user_id,
            title="A task is now yours",
            body=task["title"][:140],
            link=f"/tasks?circle={circle['_id']}",
            circle_id=circle["_id"],
            actor_id=user["_id"],
            dedupe_key=f"task-assign:{task['_id']}:{owner_user_id}",
        )
    touch_circle(db, circle["_id"])
    return True


def care_load(db, circle_id, days: int = 30) -> list:
    """Who is actually carrying the work. Deliberately a plain count, not a score."""
    since = utcnow() - timedelta(days=days)
    members = domain.members_of(db, circle_id)
    rows = []
    unassigned = db["tasks"].count_documents(
        {"circle_id": circle_id, "status": "open", "owner_user_id": None}
    )
    for member in members:
        user_id = member.get("user_id")
        if not user_id:
            continue
        completed = db["tasks"].count_documents(
            {"circle_id": circle_id, "completed_by": user_id, "completed_at": {"$gte": since}}
        )
        open_tasks = db["tasks"].count_documents(
            {"circle_id": circle_id, "status": "open", "owner_user_id": user_id}
        )
        updates = db["updates"].count_documents(
            {"circle_id": circle_id, "author_id": user_id, "created_at": {"$gte": since}}
        )
        rows.append(
            {
                "label": member.get("label"),
                "role": member.get("role"),
                "completed": completed,
                "open": open_tasks,
                "updates": updates,
                "total": completed + open_tasks,
            }
        )
    rows.sort(key=lambda row: -(row["total"]))
    return rows, unassigned


# ----------------------------------------------------------------- documents


def store_upload(file_storage):
    """Validate and persist an upload. Returns (stored_name, size, error)."""
    filename = (file_storage.filename or "").strip()
    if not filename or "." not in filename:
        return None, 0, "Choose a file with an extension, such as .pdf"
    extension = filename.rsplit(".", 1)[1].lower()
    if extension not in ALLOWED_UPLOADS:
        allowed = ", ".join(sorted(ALLOWED_UPLOADS))
        return None, 0, f"That file type is not accepted. Allowed: {allowed}."

    payload = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not payload:
        return None, 0, "That file was empty."
    if len(payload) > MAX_UPLOAD_BYTES:
        return None, 0, "That file is larger than 8 MB."

    expected = MAGIC.get(extension)
    if expected and not payload.startswith(expected):
        return None, 0, "That file does not look like what its extension claims."

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    stored_name = f"{uuid.uuid4().hex}.{extension}"
    with open(os.path.join(UPLOAD_DIR, stored_name), "wb") as handle:
        handle.write(payload)
    return stored_name, len(payload), ""


def add_document(db, circle, user, *, title, kind, doc_date=None, notes="", stored_name="", original_name="", content_type="", size=0):
    now = utcnow()
    restricted = kind in domain.RESTRICTED_KINDS
    doc = {
        "circle_id": circle["_id"],
        "title": title[:120],
        "kind": kind if kind in domain.DOCUMENT_LABELS else "other",
        "doc_date": doc_date,
        "notes": notes[:1000],
        "stored_name": stored_name,
        "original_name": original_name[:160],
        "content_type": content_type or "application/octet-stream",
        "size": size,
        "restricted": restricted,
        "uploaded_by": user["_id"],
        "uploaded_by_name": user.get("name") or user.get("username"),
        "created_at": now,
    }
    result = db["documents"].insert_one(doc)
    doc["_id"] = result.inserted_id
    touch_circle(db, circle["_id"])
    return doc


def document_path(document) -> str | None:
    name = document.get("stored_name")
    if not name:
        return None
    candidate = os.path.join(UPLOAD_DIR, os.path.basename(name))
    return candidate if os.path.exists(candidate) else None


# ---------------------------------------------------- what changed / today


def mark_seen(db, user_id, circle_id) -> None:
    db["seen"].update_one(
        {"user_id": user_id, "circle_id": circle_id},
        {"$set": {"last_seen_at": utcnow()}},
        upsert=True,
    )


def last_seen(db, user_id, circle_id):
    row = db["seen"].find_one({"user_id": user_id, "circle_id": circle_id})
    return as_utc(row.get("last_seen_at")) if row else None


def what_changed(db, circle, user) -> dict:
    """Everything that moved since this person last looked."""
    since = last_seen(db, user["_id"], circle["_id"]) or (
        utcnow() - timedelta(days=7)
    )
    now = utcnow()

    medication_changes = list(
        db["medication_changes"]
        .find({"circle_id": circle["_id"], "changed_at": {"$gt": since}})
        .sort("changed_at", -1)
    )
    completed_appointments = db["appointments"].count_documents(
        {"circle_id": circle["_id"], "status": "completed", "updated_at": {"$gt": since}}
    )
    upcoming = db["appointments"].count_documents(
        {
            "circle_id": circle["_id"],
            "status": "upcoming",
            "starts_at": {"$gte": now, "$lt": now + timedelta(days=30)},
        }
    )
    tasks_done = db["tasks"].count_documents(
        {"circle_id": circle["_id"], "status": "done", "completed_at": {"$gt": since}}
    )
    tasks_open = db["tasks"].count_documents(
        {"circle_id": circle["_id"], "status": "open"}
    )
    tasks_unassigned = db["tasks"].count_documents(
        {"circle_id": circle["_id"], "status": "open", "owner_user_id": None}
    )
    new_documents = list(
        db["documents"]
        .find({"circle_id": circle["_id"], "created_at": {"$gt": since}})
        .sort("created_at", -1)
    )
    new_updates = list(
        db["updates"]
        .find({"circle_id": circle["_id"], "created_at": {"$gt": since}})
        .sort("created_at", -1)
        .limit(20)
    )

    return {
        "since": since,
        "medication_changes": medication_changes,
        "completed_appointments": completed_appointments,
        "upcoming_appointments": upcoming,
        "tasks_done": tasks_done,
        "tasks_open": tasks_open,
        "tasks_unassigned": tasks_unassigned,
        "new_documents": new_documents,
        "new_updates": new_updates,
        "is_first_visit": last_seen(db, user["_id"], circle["_id"]) is None,
    }


def today_view(db, circle, role: str | None, user_id) -> dict:
    tz = circle.get("timezone")
    day = today_local(tz)
    start, end = day_bounds_utc(day, tz)
    tomorrow = day + timedelta(days=1)
    tomorrow_start, _ = day_bounds_utc(tomorrow, tz)

    doses = doses_for_day(db, circle, day) if can(role, "view_medications") else []

    appointments = []
    if can(role, "view_appointments"):
        appointments = list(
            db["appointments"]
            .find(
                {
                    "circle_id": circle["_id"],
                    "status": "upcoming",
                    "starts_at": {"$gte": start, "$lt": tomorrow_start},
                }
            )
            .sort("starts_at", 1)
        )

    open_tasks = tasks_visible_to(db, circle["_id"], role, user_id)
    due_soon = [
        task
        for task in open_tasks
        if task.get("due_at") and as_utc(task["due_at"]) < tomorrow_start
    ]
    unassigned = [task for task in open_tasks if not task.get("owner_user_id")]

    recent = []
    if can(role, "view_updates"):
        recent = updates_of(db, circle["_id"], limit=5)

    return {
        "day": day,
        "window_start": start,
        "window_end": tomorrow_start,
        "doses": doses,
        "outstanding_doses": [dose for dose in doses if not dose["taken"]],
        "appointments": appointments,
        "tasks": open_tasks,
        "tasks_due_soon": due_soon,
        "tasks_unassigned": unassigned,
        "recent": recent,
    }


def search(db, circle_id, query: str, role: str | None, user_id, limit: int = 6) -> dict:
    """Bounded search over normalised fields, never over raw documents."""
    needle = (query or "").strip()[:80]
    if not needle:
        return {"query": "", "groups": [], "total": 0}
    pattern = {"$regex": needle, "$options": "i"}
    results = []

    if can(role, "view_updates"):
        rows = list(
            db["updates"]
            .find({"circle_id": circle_id, "$or": [{"body": pattern}, {"title": pattern}]})
            .sort("created_at", -1)
            .limit(limit)
        )
        if rows:
            results.append(("Updates", [{"label": r.get("title") or r.get("body", "")[:70], "sub": r.get("body", "")[:90], "href": f"/updates?q={needle}"} for r in rows]))

    if can(role, "view_medications"):
        rows = list(
            db["medications"]
            .find({"circle_id": circle_id, "$or": [{"name": pattern}, {"purpose": pattern}]})
            .limit(limit)
        )
        if rows:
            results.append(("Medications", [{"label": describe_medication(r), "sub": r.get("purpose", ""), "href": f"/medications?circle={circle_id}"} for r in rows]))

    if can(role, "view_tasks"):
        rows = tasks_of(db, circle_id, status="all", query=needle, limit=limit)
        if role == "neighbour":
            rows = [r for r in rows if r.get("owner_user_id") == user_id]
        if rows:
            results.append(("Tasks", [{"label": r["title"], "sub": r.get("status", ""), "href": f"/tasks?circle={circle_id}"} for r in rows]))

    if can(role, "view_appointments"):
        rows = list(
            db["appointments"]
            .find({"circle_id": circle_id, "$or": [{"title": pattern}, {"location": pattern}]})
            .sort("starts_at", -1)
            .limit(limit)
        )
        if rows:
            results.append(("Appointments", [{"label": r["title"], "sub": r.get("location", ""), "href": f"/appointments/{r['_id']}?circle={circle_id}"} for r in rows]))

    if can(role, "view_documents"):
        rows = [
            row
            for row in db["documents"]
            .find({"circle_id": circle_id, "$or": [{"title": pattern}, {"notes": pattern}]})
            .limit(limit)
            if domain.document_visible(row, role, user_id)
        ]
        if rows:
            results.append(("Documents", [{"label": r["title"], "sub": domain.DOCUMENT_LABELS.get(r.get("kind"), ""), "href": f"/documents?circle={circle_id}"} for r in rows]))

    if can(role, "view_people"):
        rows = list(
            db["people"]
            .find({"circle_id": circle_id, "$or": [{"name": pattern}, {"org": pattern}]})
            .limit(limit)
        )
        if rows:
            results.append(("People", [{"label": r["name"], "sub": r.get("org") or domain.PERSON_LABELS.get(r.get("kind"), ""), "href": f"/people?circle={circle_id}"} for r in rows]))

    return {
        "query": needle,
        "groups": results,
        "total": sum(len(items) for _title, items in results),
    }
