"""The care surfaces: Today, Updates, Medications, Appointments, Tasks,
Documents, People, Circle, Handover, What changed and Settings."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import care, domain, extract
from .care import (
    MAX_UPLOAD_BYTES,
    add_appointment,
    add_document,
    add_medication,
    add_task,
    appointments_of,
    care_load,
    changes_of,
    clear_dose,
    document_path,
    finish_appointment,
    mark_dose,
    mark_notifications_read,
    mark_seen,
    medications_of,
    notify,
    notifications_for,
    post_update,
    search,
    set_task_status,
    store_upload,
    tasks_of,
    tasks_visible_to,
    today_view,
    unread_count,
    update_medication,
    updates_of,
    what_changed,
)
from .accounts import display_name
from .domain import (
    ACCESS_TABLE,
    PERSON_KINDS,
    ROLE_BLURB,
    ROLE_LABELS,
    add_person,
    can,
    document_visible,
    oid,
    require_capability,
    require_circle,
    visible_documents,
)
from .security import (
    csrf_token,
    current_user,
    is_cross_site,
    login_required,
    record_audit,
    require_csrf,
    safe_next,
)
from .timing import (
    COMMON_TIMEZONES,
    DEFAULT_TIMEZONE,
    format_local,
    humanise_age,
    parse_date,
    parse_local,
    relative_day,
    today_local,
    tz_for,
    utcnow,
)

bp = Blueprint("care", __name__)


# ------------------------------------------------------------ action feedback

# Every write used to end in a bare redirect, so the app never said whether the
# thing you just did actually happened — a real gap in the interface, not just a
# nicety. Rather than store a message (which then has to be cleared, and can go
# stale across a back button), the redirect carries a key and the shell looks it
# up. The confirmation therefore belongs to exactly one page load.
ACTION_NOTICES = {
    "medication-added": "Medication added to the record.",
    "medication-updated": "Medication updated, and the change was logged.",
    "medication-stopped": "Medication stopped. It stays in the history below.",
    "appointment-added": "Appointment added.",
    "appointment-finished": "Appointment saved and posted to Updates.",
    "appointment-cancelled": "Appointment cancelled.",
    "task-added": "Task added.",
    "task-done": "Task marked done.",
    "task-reopened": "Task reopened.",
    "task-assigned": "Task reassigned.",
    "task-cancelled": "Task cancelled.",
    "document-added": "Document saved.",
    "person-added": "Added to the directory.",
    "person-removed": "Removed from the directory.",
    "circle-renamed": "Circle details saved.",
    "invite-created": "Invite ready — copy the link below and send it on.",
    "role-changed": "Role updated.",
    "member-removed": "They no longer have access to this circle.",
    "update-posted": "Posted to Updates.",
    "handover-done": "Visit handed over. The circle has been told.",
    "all-read": "Everything marked as read.",
}


def _with_notice(target: str, key: str) -> str:
    """Attach an action notice to a target that is already a URL.

    Needed where a handler honours the caller's own next= — appending the notice
    must not replace where they were going.
    """
    parts = urlsplit(target)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("ok", key))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _db():
    return current_app.extensions["kc_db"]


# --------------------------------------------------------------- circle pick


def active_circle(db, user):
    """The circle the shell is showing — always carrying the caller's role.

    The role has to travel *with* the document. The navigation, the capability
    checks and the templates all read it, and a bare circle record from the
    database has no role on it, which silently collapsed the menu.
    """
    session = getattr(g, "session", None) or {}
    chosen = session.get("active_circle")
    if chosen:
        circle = db["circles"].find_one({"_id": chosen})
        if circle:
            found = domain.membership(db, circle["_id"], user["_id"])
            if found:
                circle["role"] = found.get("role")
                return circle
    return domain.default_circle(db, user)


def select_circle(db, value):
    """Remember the chosen circle, and keep the in-memory session in step.

    Without updating ``g.session`` the change only takes effect on the *next*
    request, so the page just rendered would disagree with the one after it.
    """
    circle_id = oid(value)
    if circle_id and g.session:
        db["sessions"].update_one(
            {"_id": g.session["_id"]}, {"$set": {"active_circle": circle_id}}
        )
        g.session["active_circle"] = circle_id


def _context(extra: dict | None = None) -> dict:
    """Everything the shell needs: the active circle, role and unread count."""
    user = current_user()
    db = _db()
    circles = domain.circles_for_user(db, user["_id"])
    circle = active_circle(db, user)
    role = circle.get("role") if circle else None
    if circle:
        g.tz = circle.get("timezone") or getattr(g, "tz", None)
    context = {
        "circles": circles,
        "circle": circle,
        "role": role,
        "role_label": ROLE_LABELS.get(role, ""),
        # Every shell template needs these two, so they live in the one place
        # that builds the shell context rather than in each view that happens
        # to remember to pass them.
        "role_labels": ROLE_LABELS,
        "role_blurb": ROLE_BLURB,
        "caps": domain.capabilities(role),
        "unread": unread_count(db, user["_id"]),
        "nav_counts": {},
    }
    notice_key = (request.args.get("ok") or "").strip()
    if notice_key in ACTION_NOTICES:
        context["flash"] = ACTION_NOTICES[notice_key]
    if circle:
        context["nav_counts"] = {
            "tasks": len(tasks_visible_to(db, circle["_id"], role, user["_id"])),
        }
        context["members"] = domain.members_of(db, circle["_id"])
    if extra:
        context.update(extra)
    return context


def _need_circle():
    user = current_user()
    db = _db()
    circle = active_circle(db, user)
    if not circle:
        return None, None, None, redirect(url_for("auth.welcome"))
    return db, circle, domain.membership(db, circle["_id"], user["_id"]), None


# ---------------------------------------------------------------- operational


@bp.get("/healthz")
def healthz():
    database = current_app.extensions["kc_database"]
    if not database.ready:
        database.connect()
    ok = database.ready and database.ping()
    return {"status": "ok" if ok else "unavailable"}, (200 if ok else 503)


# --------------------------------------------------------------------- today


@bp.get("/")
@login_required
def today():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    view = today_view(db, circle, role, user["_id"])
    mark_seen(db, user["_id"], circle["_id"])
    return render_template(
        "today.html",
        **_context(
            {
                "view": view,
                "day": view["day"],
                "greeting": _greeting(circle),
            }
        ),
    )


def _greeting(circle) -> str:
    local = utcnow().astimezone(tz_for(circle.get("timezone")))
    if local.hour < 12:
        return "Good morning"
    if local.hour < 18:
        return "Good afternoon"
    return "Good evening"


@bp.post("/circle/switch")
@login_required
def switch_circle():
    require_csrf()
    select_circle(_db(), request.form.get("circle"))
    return redirect(safe_next(request.form.get("next"), url_for("care.today")))


# ------------------------------------------------------------------- updates


@bp.get("/updates")
@login_required
def updates():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_updates")
    query = (request.args.get("q") or "").strip()[:80]
    rows = updates_of(db, circle["_id"], limit=60)
    if query:
        needle = query.lower()
        rows = [
            row
            for row in rows
            if needle in (row.get("body", "") + row.get("title", "")).lower()
        ]
    return render_template(
        "updates.html",
        **_context(
            {
                "rows": rows,
                "query": query,
                "kinds": care.UPDATE_KINDS,
            }
        ),
    )


@bp.post("/updates")
@login_required
def post_update_view():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "post_updates")
    body = (request.form.get("body") or "").strip()[:4000]
    kind = request.form.get("kind", "note")
    if not body:
        return redirect(url_for("care.updates"))
    post_update(db, circle, current_user(), body=body, kind=kind)
    return redirect(url_for("care.updates", ok="update-posted"))


# --------------------------------------------------------------- medications


@bp.get("/medications")
@login_required
def medications():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_medications")
    return render_template(
        "medications.html",
        **_context(
            {
                "active": medications_of(db, circle["_id"], True),
                "stopped": medications_of(db, circle["_id"], False),
                "history": changes_of(db, circle["_id"], 60),
                "people": domain.people_of(db, circle["_id"], kinds=["gp", "specialist", "pharmacy"]),
                "field_labels": care.FIELD_LABELS,
                "today": today_local(circle.get("timezone")),
            }
        ),
    )


@bp.post("/medications")
@login_required
def medication_add():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_medications")
    name = (request.form.get("name") or "").strip()
    if name:
        times = [t.strip() for t in request.form.getlist("times") if t.strip()]
        add_medication(
            db,
            circle,
            current_user(),
            name=name,
            strength=(request.form.get("strength") or "").strip(),
            form=(request.form.get("form") or "").strip(),
            dose=(request.form.get("dose") or "").strip(),
            times=times,
            purpose=(request.form.get("purpose") or "").strip(),
            prescriber_id=oid(request.form.get("prescriber_id")),
            repeat_due=parse_date(request.form.get("repeat_due")),
            supply_left=(request.form.get("supply_left") or "").strip(),
        )
    return redirect(url_for("care.medications", ok="medication-added"))


@bp.post("/medications/<medication_id>")
@login_required
def medication_edit(medication_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_medications")
    med_id = oid(medication_id)
    if not med_id:
        abort(404)
    changes = {
        "strength": (request.form.get("strength") or "").strip(),
        "dose": (request.form.get("dose") or "").strip(),
        "purpose": (request.form.get("purpose") or "").strip(),
        "times": [t.strip() for t in request.form.getlist("times") if t.strip()],
    }
    update_medication(
        db, circle, current_user(), med_id, changes, note=(request.form.get("note") or "").strip()
    )
    return redirect(url_for("care.medications", ok="medication-updated"))


@bp.post("/medications/<medication_id>/stop")
@login_required
def medication_stop(medication_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_medications")
    med_id = oid(medication_id)
    if med_id:
        care.stop_medication(
            db, circle, current_user(), med_id, note=(request.form.get("note") or "").strip()
        )
    return redirect(url_for("care.medications", ok="medication-stopped"))


@bp.post("/doses")
@login_required
def dose_mark():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "view_medications")
    med_id = oid(request.form.get("medication_id"))
    slot = (request.form.get("slot") or "").strip()[:10]
    on_date = (request.form.get("on_date") or "").strip()[:10]
    action = request.form.get("action", "taken")
    if med_id and slot and on_date:
        if action == "clear":
            clear_dose(db, circle, med_id, on_date, slot)
        else:
            mark_dose(db, circle, current_user(), med_id, on_date, slot, action)
    return redirect(safe_next(request.form.get("next"), url_for("care.today")))


# -------------------------------------------------------------- appointments


@bp.get("/appointments")
@login_required
def appointments():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_appointments")
    when = request.args.get("when", "upcoming")
    if when not in ("upcoming", "past", "all"):
        when = "upcoming"
    members = domain.members_of(db, circle["_id"])
    return render_template(
        "appointments.html",
        **_context(
            {
                "rows": appointments_of(db, circle["_id"], when),
                "when": when,
                "people": domain.people_of(db, circle["_id"], kinds=["gp", "specialist", "other"]),
                "members": members,
            }
        ),
    )


@bp.post("/appointments")
@login_required
def appointment_add():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_appointments")
    title = (request.form.get("title") or "").strip()
    starts_at = parse_local(request.form.get("starts_at"), circle.get("timezone"))
    if title and starts_at:
        add_appointment(
            db,
            circle,
            current_user(),
            title=title,
            starts_at=starts_at,
            location=(request.form.get("location") or "").strip(),
            person_id=oid(request.form.get("person_id")),
            driver_user_id=oid(request.form.get("driver_user_id")),
            questions=[q.strip() for q in request.form.get("questions", "").splitlines()],
            bring=[b.strip() for b in request.form.get("bring", "").splitlines()],
        )
    return redirect(url_for("care.appointments", ok="appointment-added"))


@bp.get("/appointments/<appointment_id>")
@login_required
def appointment(appointment_id: str):
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "view_appointments")
    appt_id = oid(appointment_id)
    row = db["appointments"].find_one({"_id": appt_id, "circle_id": circle["_id"]})
    if not row:
        abort(404)
    person = db["people"].find_one({"_id": row.get("person_id")}) if row.get("person_id") else None
    driver = db["users"].find_one({"_id": row.get("driver_user_id")}) if row.get("driver_user_id") else None
    return render_template(
        "appointment.html",
        **_context({"appt": row, "person": person, "driver": driver}),
    )


@bp.post("/appointments/<appointment_id>/finish")
@login_required
def appointment_finish(appointment_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_appointments")
    appt_id = oid(appointment_id)
    if appt_id:
        finish_appointment(
            db,
            circle,
            current_user(),
            appt_id,
            notes=(request.form.get("notes_after") or "").strip(),
            status=request.form.get("status", "completed"),
        )
    return redirect(
        url_for(
            "care.appointments",
            when="past",
            ok="appointment-cancelled"
            if request.form.get("status") == "cancelled"
            else "appointment-finished",
        )
    )


# --------------------------------------------------------------------- tasks


@bp.get("/tasks")
@login_required
def tasks():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_tasks")
    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    query = (request.args.get("q") or "").strip()[:80]
    if role == "neighbour":
        rows = tasks_of(db, circle["_id"], status="open", owner_user_id=user["_id"], query=query)
        done = []
    else:
        rows = tasks_of(db, circle["_id"], status="open", query=query)
        done = tasks_of(db, circle["_id"], status="done", query=query, limit=25)
    members = domain.members_of(db, circle["_id"])
    owners = {m.get("user_id"): m.get("label") for m in members}
    for row in rows + done:
        row["owner_label"] = owners.get(row.get("owner_user_id"))
    load, unassigned = care_load(db, circle["_id"], 30)
    return render_template(
        "tasks.html",
        **_context(
            {
                "rows": rows,
                "done": done,
                "query": query,
                "members": members,
                "load": load,
                "unassigned": unassigned,
                "today": today_local(circle.get("timezone")),
            }
        ),
    )


@bp.post("/tasks")
@login_required
def task_add():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_tasks")
    title = (request.form.get("title") or "").strip()
    if title:
        add_task(
            db,
            circle,
            current_user(),
            title=title,
            owner_user_id=oid(request.form.get("owner_user_id")),
            due_at=parse_local(request.form.get("due_at"), circle.get("timezone")),
            priority=request.form.get("priority", "normal"),
        )
    return redirect(url_for("care.tasks", ok="task-added"))


@bp.post("/tasks/<task_id>")
@login_required
def task_update(task_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    task_id_value = oid(task_id)
    if not task_id_value:
        abort(404)
    row = db["tasks"].find_one({"_id": task_id_value, "circle_id": circle["_id"]})
    if not row:
        abort(404)

    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    action = request.form.get("action", "done")

    # A helper may only touch their own tasks, and may only complete them.
    if role == "neighbour":
        if row.get("owner_user_id") != user["_id"] or action not in ("done", "reopen"):
            abort(403)
    else:
        require_capability(db, str(circle["_id"]), "manage_tasks")

    if action == "done":
        set_task_status(db, circle, user, task_id_value, "done")
    elif action == "reopen":
        set_task_status(db, circle, user, task_id_value, "open")
    elif action == "assign":
        care.assign_task(db, circle, user, task_id_value, oid(request.form.get("owner_user_id")))
    elif action == "cancel":
        set_task_status(db, circle, user, task_id_value, "cancelled")
    # Finishing a task from Today has to come back to Today, so the notice rides
    # along with the caller's own next= rather than replacing it.
    return redirect(
        _with_notice(
            safe_next(request.form.get("next"), url_for("care.tasks")),
            {
                "done": "task-done",
                "reopen": "task-reopened",
                "assign": "task-assigned",
                "cancel": "task-cancelled",
            }.get(action, "task-done"),
        )
    )


# ----------------------------------------------------------------- documents


@bp.get("/documents")
@login_required
def documents():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_documents")
    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    rows = visible_documents(db, circle["_id"], role, user["_id"])
    return render_template(
        "documents.html",
        **_context(
            {
                "rows": rows,
                "kinds": domain.DOCUMENT_KINDS,
                "labels": domain.DOCUMENT_LABELS,
                "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
            }
        ),
    )


@bp.post("/documents")
@login_required
def document_add():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_documents")
    title = (request.form.get("title") or "").strip()
    kind = request.form.get("kind", "other")
    upload = request.files.get("file")
    stored_name = ""
    original_name = ""
    content_type = ""
    size = 0
    error = ""

    if upload and upload.filename:
        stored_name, size, error = store_upload(upload)
        if error:
            stored_name = ""
        original_name = upload.filename
        content_type = upload.mimetype or "application/octet-stream"

    if not title:
        title = original_name.rsplit(".", 1)[0][:120] if original_name else "Untitled document"

    if not error:
        add_document(
            db,
            circle,
            current_user(),
            title=title,
            kind=kind,
            doc_date=parse_date(request.form.get("doc_date")),
            notes=(request.form.get("notes") or "").strip(),
            stored_name=stored_name,
            original_name=original_name,
            content_type=content_type,
            size=size,
        )
    else:
        user = current_user()
        role = domain.role_of(db, circle["_id"], user["_id"])
        return (
            render_template(
                "documents.html",
                **_context(
                    {
                        "rows": visible_documents(db, circle["_id"], role, user["_id"]),
                        "kinds": domain.DOCUMENT_KINDS,
                        "labels": domain.DOCUMENT_LABELS,
                        "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
                        "error": error,
                    }
                ),
            ),
            400,
        )
    return redirect(url_for("care.documents", ok="document-added"))


@bp.get("/documents/<document_id>/file")
@login_required
def document_file(document_id: str):
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "view_documents")
    doc_id = oid(document_id)
    row = db["documents"].find_one({"_id": doc_id, "circle_id": circle["_id"]})
    if not row or not document_visible(
        row, domain.role_of(db, circle["_id"], current_user()["_id"]), current_user()["_id"]
    ):
        abort(404)
    path = document_path(row)
    if not path:
        abort(404)
    return send_file(
        path,
        mimetype="application/octet-stream",
        as_attachment=True,
        download_name=row.get("original_name") or "document",
    )


# -------------------------------------------------------------------- people


@bp.get("/people")
@login_required
def people():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_people")
    rows = domain.people_of(db, circle["_id"])
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get("kind", "other"), []).append(row)
    return render_template(
        "people.html",
        **_context(
            {
                "grouped": grouped,
                "kinds": PERSON_KINDS,
                "labels": domain.PERSON_LABELS,
                "members": domain.members_of(db, circle["_id"]),
            }
        ),
    )


@bp.post("/people")
@login_required
def person_add():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_people")
    name = (request.form.get("name") or "").strip()
    if name:
        add_person(
            db,
            circle["_id"],
            name=name,
            kind=request.form.get("kind", "other"),
            org=(request.form.get("org") or "").strip(),
            phone=(request.form.get("phone") or "").strip(),
            email=(request.form.get("email") or "").strip(),
            notes=(request.form.get("notes") or "").strip(),
            created_by=current_user()["_id"],
        )
    return redirect(url_for("care.people", ok="person-added"))


@bp.post("/people/<person_id>/remove")
@login_required
def person_remove(person_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_people")
    person = oid(person_id)
    if person:
        domain.remove_person(db, circle["_id"], person)
    return redirect(url_for("care.people", ok="person-removed"))


# -------------------------------------------------------------------- circle


@bp.get("/circle")
@login_required
def circle_home():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    members = domain.members_of(db, circle["_id"])
    load, unassigned = care_load(db, circle["_id"], 30)
    role = domain.role_of(db, circle["_id"], current_user()["_id"])
    return render_template(
        "circle.html",
        **_context(
            {
                "members": members,
                "load": load,
                "unassigned": unassigned,
                "pending": domain.pending_invites(db, circle["_id"]),
                "access_table": ACCESS_TABLE,
                "role_labels": ROLE_LABELS,
                "role_blurb": ROLE_BLURB,
                "reveal": current_app.extensions["kc_take_reveal"](),
                "member_count": len(members),
                "can_invite": can(role, "invite_members"),
                "timezones": COMMON_TIMEZONES,
            }
        ),
    )


@bp.post("/circle/rename")
@login_required
def circle_rename():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_circle")
    name = (request.form.get("name") or "").strip()
    subject = (request.form.get("subject_name") or "").strip()
    timezone = request.form.get("timezone") or circle.get("timezone")
    if timezone not in COMMON_TIMEZONES:
        timezone = circle.get("timezone") or DEFAULT_TIMEZONE
    if name:
        db["circles"].update_one(
            {"_id": circle["_id"]},
            {"$set": {"name": name[:80], "subject_name": subject[:80], "timezone": timezone}},
        )
    return redirect(url_for("care.circle_home", ok="circle-renamed"))


@bp.post("/circle/invite")
@login_required
def circle_invite():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "invite_members")
    email = (request.form.get("email") or "").strip().lower()[:254]
    role = request.form.get("role", "family")
    if "@" in email:
        raw = domain.invite(db, circle, email=email, role=role, created_by=current_user()["_id"])
        current_app.extensions["kc_set_reveal"](
            {"kind": "invite", "email": email, "path": url_for("auth.invite", token=raw)}
        )
    return redirect(url_for("care.circle_home", ok="invite-created"))


@bp.post("/circle/member/<member_id>/role")
@login_required
def circle_member_role(member_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_members")
    target = oid(member_id)
    if target:
        domain.set_role(db, circle["_id"], target, request.form.get("role", ""))
    return redirect(url_for("care.circle_home", ok="role-changed"))


@bp.post("/circle/member/<member_id>/remove")
@login_required
def circle_member_remove(member_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "manage_members")
    target = oid(member_id)
    if target:
        domain.remove_member(db, circle["_id"], target)
    return redirect(url_for("care.circle_home", ok="member-removed"))


# ------------------------------------------------------------------ handover


@bp.get("/handover")
@login_required
def handover():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "use_handover")
    meds = medications_of(db, circle["_id"], True)
    return render_template(
        "handover.html",
        **_context({"medications": meds, "members": domain.members_of(db, circle["_id"])}),
    )


@bp.post("/handover")
@login_required
def handover_submit():
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "use_handover")
    user = current_user()

    items = []
    for med in medications_of(db, circle["_id"], True):
        if request.form.get(f"prepared_{med['_id']}"):
            items.append({"type": "medication", "text": f"{care.describe_medication(med)} prepared"})
    for line in (request.form.get("observations") or "").splitlines():
        if line.strip():
            items.append({"type": "observation", "text": line.strip()})
    for line in (request.form.get("communication") or "").splitlines():
        if line.strip():
            items.append({"type": "communication", "text": line.strip()})

    body = (request.form.get("summary") or "").strip()
    created = post_update(
        db,
        circle,
        user,
        body=body or "Visit ended.",
        kind="handover",
        title=f"Visit handover by {display_name(user)}",
        items=items,
        source="handover",
    )

    # Anything the next person has to do becomes a real, assigned task.
    for line in (request.form.get("tasks") or "").splitlines():
        text = line.strip()
        if text:
            add_task(
                db,
                circle,
                user,
                title=text,
                owner_user_id=oid(request.form.get("task_owner")) or user["_id"],
                source="handover",
            )
    return redirect(url_for("care.updates", ok="handover-done"))


# -------------------------------------------------------------- what changed


@bp.get("/changes")
@login_required
def changes():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    require_capability(db, str(circle["_id"]), "view_changes")
    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    summary = what_changed(db, circle, user)
    summary["new_documents"] = [
        row
        for row in summary["new_documents"]
        if document_visible(row, role, user["_id"])
    ]
    mark_seen(db, user["_id"], circle["_id"])
    return render_template("changes.html", **_context({"summary": summary}))


# ------------------------------------------------------------------- search


@bp.get("/search")
@login_required
def search_view():
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    select_circle(db, request.args.get("circle"))
    user = current_user()
    role = domain.role_of(db, circle["_id"], user["_id"])
    query = (request.args.get("q") or "").strip()
    results = search(db, circle["_id"], query, role, user["_id"]) if query else {
        "query": "",
        "groups": [],
        "total": 0,
    }
    return render_template(
        "search.html", **_context({"results": results, "query": query})
    )


# ------------------------------------------------------------ notifications


@bp.get("/notifications")
@login_required
def notifications():
    db = _db()
    user = current_user()
    return render_template(
        "notifications.html",
        **_context({"items": notifications_for(db, user["_id"], 60)}),
    )


@bp.post("/notifications/read")
@login_required
def notifications_read():
    require_csrf()
    mark_notifications_read(_db(), current_user()["_id"])
    return redirect(url_for("care.notifications", ok="all-read"))


# ----------------------------------------------------------------- settings


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    db = _db()
    user = current_user()
    error = ""
    notice = ""

    if request.method == "POST":
        require_csrf()
        action = request.form.get("action", "profile")
        if action == "profile":
            from .accounts import update_profile

            name = (request.form.get("name") or "").strip()
            timezone = request.form.get("timezone") or user.get("timezone")
            if timezone not in COMMON_TIMEZONES:
                timezone = DEFAULT_TIMEZONE
            update_profile(db, user["_id"], name=name, timezone=timezone)
            notice = "Your details have been saved."
        else:
            from .accounts import authenticate, set_password
            from .security import password_problem

            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            if not authenticate(db, user["username"], current):
                error = "That current password is not right."
            else:
                problem = password_problem(
                    new, username=user["username"], email=user.get("email", "")
                )
                if problem:
                    error = problem
                else:
                    set_password(db, user["_id"], new)
                    notice = "Your password has been changed."

    refreshed = db["users"].find_one({"_id": user["_id"]})
    return render_template(
        "settings.html",
        **_context(
            {
                "error": error,
                "notice": notice,
                "profile": refreshed,
                "timezones": COMMON_TIMEZONES,
                "memberships": domain.circles_for_user(db, user["_id"]),
            }
        ),
    )


# ---------------------------------------------------------------- proposals


@bp.post("/structure")
@login_required
def structure():
    """Read a block of text and propose structured items. Nothing is saved yet."""
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "use_extract")
    text = (request.form.get("text") or "").strip()[:6000]
    proposals = extract.extract(text) if text else []
    result = db["proposals"].insert_one(
        {
            "circle_id": circle["_id"],
            "created_by": current_user()["_id"],
            "source_text": text,
            "items": proposals,
            "status": "pending",
            "created_at": utcnow(),
        }
    )
    return redirect(url_for("care.proposals", proposal_id=str(result.inserted_id)))


@bp.get("/proposals/<proposal_id>")
@login_required
def proposals(proposal_id: str):
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "use_extract")
    row = db["proposals"].find_one({"_id": oid(proposal_id), "circle_id": circle["_id"]})
    if not row:
        abort(404)
    return render_template(
        "proposals.html",
        **_context({"proposal": row, "counts": extract.summarise(row.get("items", []))}),
    )


@bp.post("/proposals/<proposal_id>")
@login_required
def proposal_decide(proposal_id: str):
    require_csrf()
    db, circle, _member, bounce = _need_circle()
    if bounce:
        return bounce
    require_capability(db, str(circle["_id"]), "use_extract")
    row = db["proposals"].find_one({"_id": oid(proposal_id), "circle_id": circle["_id"]})
    if not row:
        abort(404)
    user = current_user()
    index = request.form.get("index")
    action = request.form.get("action", "accept")
    items = row.get("items", [])
    if index is None or not index.isdigit() or int(index) >= len(items):
        return redirect(url_for("care.proposals", proposal_id=proposal_id))
    item = items[int(index)]

    if action == "accept":
        _apply_proposal(db, circle, user, item)
    elif action == "accept_all":
        for candidate in items:
            _apply_proposal(db, circle, user, candidate)
        db["proposals"].update_one(
            {"_id": row["_id"]}, {"$set": {"status": "applied", "items": []}}
        )
        return redirect(url_for("care.updates"))

    remaining = [candidate for position, candidate in enumerate(items) if position != int(index)]
    db["proposals"].update_one(
        {"_id": row["_id"]},
        {"$set": {"items": remaining, "status": "applied" if not remaining else "pending"}},
    )
    if not remaining:
        return redirect(url_for("care.updates"))
    return redirect(url_for("care.proposals", proposal_id=proposal_id))


def _apply_proposal(db, circle, user, item: dict) -> None:
    """Turn one confirmed proposal into a real record."""
    kind = item.get("type")
    text = item.get("text", "").strip()
    if kind == "task":
        due = None
        if item.get("due_at"):
            due = parse_date(item["due_at"])
            if due:
                from datetime import datetime, time
                from .timing import tz_for

                due = datetime.combine(due, time(9, 0), tzinfo=tz_for(circle.get("timezone")))
        add_task(
            db,
            circle,
            user,
            title=item.get("title") or text,
            owner_user_id=user["_id"],
            due_at=due,
            source="extract",
        )
    elif kind == "appointment":
        add_task(
            db,
            circle,
            user,
            title=item.get("title") or text,
            owner_user_id=user["_id"],
            source="extract",
        )
    elif kind == "medication":
        name = (item.get("name") or "").strip()
        if name and item.get("action") == "stop":
            existing = db["medications"].find_one(
                {"circle_id": circle["_id"], "name": name, "active": True}
            )
            if existing:
                care.stop_medication(
                    db, circle, user, existing["_id"], note="Confirmed from pasted note"
                )
        elif name:
            existing = db["medications"].find_one(
                {"circle_id": circle["_id"], "name": name, "active": True}
            )
            if existing and item.get("strength"):
                update_medication(
                    db,
                    circle,
                    user,
                    existing["_id"],
                    {"strength": item["strength"]},
                    note=item.get("detail", "Confirmed from pasted note")[:400],
                )
            else:
                add_medication(
                    db,
                    circle,
                    user,
                    name=name,
                    strength=item.get("strength", ""),
                    purpose=item.get("detail", "")[:160],
                )
    post_update(
        db,
        circle,
        user,
        body=text,
        kind={
            "task": "task",
            "appointment": "appointment",
            "medication": "medication",
            "communication": "communication",
        }.get(kind, "observation"),
        title="Added from a confirmed note",
        source="extract",
    )
