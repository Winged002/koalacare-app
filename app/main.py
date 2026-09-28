"""KoalaCare — the care coordination app.

This is the working product: Today, Circle, Updates, Medications,
Appointments, Tasks, Documents, People, visit handover, "what changed" and the
care-load view. The marketing site is a separate deployment.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

from flask import Flask, g, jsonify, redirect, render_template, request, url_for
from pymongo.errors import DuplicateKeyError

from . import accounts, care, domain, security
from .db import Database
from .security import RateLimiter, load_identity, set_reveal, take_reveal
from .timing import (
    DEFAULT_TIMEZONE,
    as_utc,
    format_local,
    humanise_age,
    parse_local,
    relative_day,
    to_local,
    tz_for,
    utcnow,
)
from .views_auth import bp as auth_blueprint
from .views_care import bp as care_blueprint

DEFAULT_CANONICAL_HOST = "my.koalacare.app"
DEFAULT_ALIAS_HOSTS = ""


def _host_list(value: str) -> set:
    return {host.strip().lower() for host in (value or "").split(",") if host.strip()}


def _seed_demo(db) -> None:
    """Build a small, realistic circle so the product is explorable on first
    run. Every account here uses the same known demo password, which is why
    KOALACARE_SEED_DEMO exists to turn the whole thing off.
    """
    # A previous attempt may have died part-way, so "is this done?" is judged by
    # whether the demo circle has content, not by whether a lock row exists.
    circle = db["circles"].find_one({"name": "Margaret"})
    if circle and db["medications"].count_documents({"circle_id": circle["_id"]}) > 0:
        return
    if db["circles"].count_documents({}) > 0 and not circle:
        return  # real data belonging to a real user; never touch it

    # gunicorn boots both workers at once; claiming a fixed _id is atomic.
    try:
        db["seed_lock"].insert_one({"_id": "demo", "claimed_at": utcnow()})
    except DuplicateKeyError:
        pass

    username = os.environ.get("KOALACARE_DEMO_USER") or "andrej"
    password = os.environ.get("KOALACARE_DEMO_PASSWORD") or "koalacare-demo-2026"

    accounts_spec = [
        (username, "Andrej", "andrej@example.com"),
        ("priya", "Priya", "priya@example.com"),
        ("daniel", "Daniel", "daniel@example.com"),
        ("marek", "Marek", "marek@example.com"),
        ("drosei", "Dr Osei", "osei@example.com"),
        ("helen", "Helen", "helen@example.com"),
    ]
    created = {}
    for handle, label, email in accounts_spec:
        user = accounts.find_by_username(db, handle)
        if not user:
            user, _error = accounts.create_user(
                db, username=handle, email=email, password=password, name=label
            )
        if user:
            created[handle] = user

    owner = created.get(username)
    if not owner:
        return

    if circle is None:
        circle = domain.create_circle(
            db, owner, name="Margaret", subject_name="Margaret", timezone=DEFAULT_TIMEZONE
        )
    circle_id = circle["_id"]
    role_for = {
        "priya": "family",
        "daniel": "family",
        "marek": "carer",
        "drosei": "clinician",
        "helen": "neighbour",
    }
    for handle, role in role_for.items():
        member = created.get(handle)
        if member and not db["circle_members"].find_one(
            {"circle_id": circle_id, "user_id": member["_id"]}
        ):
            try:
                db["circle_members"].insert_one(
                    {
                        "circle_id": circle_id,
                        "user_id": member["_id"],
                        "invited_email": member.get("email", ""),
                        "role": role,
                        "status": "active",
                        "invited_by": owner["_id"],
                        "joined_at": utcnow(),
                    }
                )
            except DuplicateKeyError:
                pass

    gp = domain.add_person(
        db, circle_id, name="Dr Müller", kind="gp", org="Praxis am Ring",
        phone="+49 30 1234567", created_by=owner["_id"],
    )
    domain.add_person(
        db, circle_id, name="Dr Osei", kind="specialist", org="Cardiology",
        created_by=owner["_id"],
    )
    domain.add_person(
        db, circle_id, name="Apotheke Mitte", kind="pharmacy",
        phone="+49 30 7654321", created_by=owner["_id"],
    )
    domain.add_person(
        db, circle_id, name="Marek Nowak", kind="carer", org="Weekdays 08:00-12:00",
        created_by=owner["_id"],
    )
    domain.add_person(
        db, circle_id, name="Priya", kind="emergency", phone="+49 170 0000001",
        created_by=owner["_id"],
    )

    ramipril = care.add_medication(
        db, circle, owner, name="Ramipril", strength="5 mg", form="tablet",
        dose="1 tablet with breakfast", times=["08:00"], purpose="Blood pressure",
        supply_left="14 days",
    )
    care.add_medication(
        db, circle, owner, name="Metformin", strength="500 mg", form="tablet",
        dose="1 tablet twice daily", times=["08:00", "18:00"], purpose="Blood sugar",
    )
    care.add_medication(
        db, circle, owner, name="Bisoprolol", strength="2.5 mg", form="tablet",
        dose="1 tablet in the evening", times=["20:00"], purpose="Heart rate",
    )
    care.record_change(
        db, circle, ramipril, owner, "strength", "2.5 mg", "5 mg",
        note="Cardiology review",
    )

    now = utcnow()
    care.add_appointment(
        db, circle, owner, title="Cardiology review",
        starts_at=now + timedelta(days=3, hours=2), location="Klinik Mitte, 2nd floor",
        person_id=gp,
        driver_user_id=(created.get("priya") or {}).get("_id"),
        questions=["Ask about the new dose", "Is the swelling a concern?"],
        bring=["Medication list", "Blood pressure diary"],
    )
    care.add_appointment(
        db, circle, owner, title="Blood test",
        starts_at=now + timedelta(days=10), location="Praxis am Ring", person_id=gp,
    )

    care.add_task(
        db, circle, owner, title="Collect prescription from Apotheke Mitte",
        owner_user_id=(created.get("daniel") or {}).get("_id"),
        due_at=now + timedelta(days=1), priority="soon",
    )
    care.add_task(db, circle, owner, title="Book blood test appointment")
    care.add_task(
        db, circle, owner, title="Check blood-pressure monitor is working",
        owner_user_id=(created.get("helen") or {}).get("_id"),
    )

    care.post_update(
        db, circle, created.get("marek") or owner,
        body="Margaret ate a good lunch, a bit more tired than usual this afternoon. "
             "Evening medication is prepared.",
        kind="handover",
        title="Visit handover by Marek",
        items=[
            {"type": "medication", "text": "Evening medication prepared"},
            {"type": "observation", "text": "More tired than usual this afternoon"},
            {"type": "communication", "text": "Pharmacy called about the prescription"},
        ],
        source="handover",
    )
    care.post_update(
        db, circle, created.get("priya") or owner,
        body="Mum saw Dr Müller. Ramipril increased to 5 mg and a blood test was requested.",
        kind="appointment", title="Cardiology review notes",
    )


# ------------------------------------------------------------------ filters


def _request_tz():
    return tz_for(getattr(g, "tz", None))


def install_template_helpers(app: Flask) -> None:
    @app.template_filter("dt")
    def _dt(value, fmt="%d %b, %H:%M"):
        moment = to_local(value, getattr(g, "tz", None))
        return moment.strftime(fmt) if moment else "—"

    @app.template_filter("clock")
    def _clock(value):
        moment = to_local(value, getattr(g, "tz", None))
        return moment.strftime("%H:%M") if moment else "—"

    @app.template_filter("day")
    def _day(value):
        return relative_day(value, getattr(g, "tz", None))

    @app.template_filter("ago")
    def _ago(value):
        return humanise_age(value)

    @app.template_filter("dy")
    def _dy(value):
        """A calendar day, written the way people say it."""
        if isinstance(value, datetime):
            local = to_local(value, getattr(g, "tz", None))
            return local.strftime("%A %d %B") if local else "—"
        if isinstance(value, date):
            return value.strftime("%A %d %B")
        return "—"

    @app.template_filter("iso")
    def _iso(value):
        """The YYYY-MM-DD a form needs."""
        if isinstance(value, datetime):
            local = to_local(value, getattr(g, "tz", None))
            return local.strftime("%Y-%m-%d") if local else ""
        if isinstance(value, date):
            return value.isoformat()
        return ""


# -------------------------------------------------------------- app factory


def create_app(config: dict | None = None, mongo_client=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = care.MAX_UPLOAD_BYTES + 512 * 1024
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400

    is_production = os.environ.get("KOALACARE_ENV") == "production"
    secret = os.environ.get("KOALACARE_SECRET") or "koalacare-app-local-secret"
    secure_env = os.environ.get("KOALACARE_COOKIE_SECURE")
    cookie_secure = (
        is_production
        if secure_env is None
        else secure_env.strip().lower() in {"1", "true", "yes"}
    )
    canonical_host = (
        os.environ.get("KOALACARE_CANONICAL_HOST") or DEFAULT_CANONICAL_HOST
    ).strip().lower()
    alias_hosts = _host_list(
        os.environ.get("KOALACARE_ALIAS_HOSTS", DEFAULT_ALIAS_HOSTS)
    )
    site_url = f"https://{canonical_host}"

    app.config.update(
        KC_SECRET=secret,
        KC_SESSION_COOKIE=security.SESSION_COOKIE,
        KC_COOKIE_SECURE=cookie_secure,
        KC_PRODUCTION=is_production,
    )
    if config:
        app.config.update(config)

    database = Database(client=mongo_client)
    database.connect()
    app.extensions["kc_db"] = database.db
    app.extensions["kc_database"] = database
    app.extensions["kc_login_limiter"] = RateLimiter(12, 300.0)
    app.extensions["kc_take_reveal"] = lambda: take_reveal(database.db)
    app.extensions["kc_set_reveal"] = lambda payload: set_reveal(database.db, payload)

    if database.ready and (os.environ.get("KOALACARE_SEED_DEMO") or "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        try:
            _seed_demo(database.db)
        except Exception:  # noqa: BLE001
            # Demo data is a convenience, never a dependency. gunicorn boots
            # several workers at once and they can legitimately race on the
            # same insert, so a failure here must not stop a worker booting.
            pass

    install_template_helpers(app)
    app.register_blueprint(auth_blueprint)
    app.register_blueprint(care_blueprint)

    # ------------------------------------------------------------- hooks
    @app.before_request
    def _canonical_host():
        host = (request.host or "").split(":")[0].strip().lower()
        if host not in alias_hosts:
            return None
        query = request.query_string.decode("latin-1")
        target = f"{site_url}{request.path}" + (f"?{query}" if query else "")
        return redirect(target, code=301 if request.method in ("GET", "HEAD") else 308)

    @app.before_request
    def _identity():
        load_identity(database.db)
        user = security.current_user()
        g.tz = (user or {}).get("timezone") or DEFAULT_TIMEZONE

    @app.context_processor
    def inject_globals() -> dict:
        return {
            "current_user": security.current_user(),
            "csrf_token": security.csrf_token,
            "site_url": site_url,
            "current_year": time.gmtime().tm_year,
        }

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "font-src 'self'; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'",
        )
        if response.mimetype == "text/html":
            response.headers.setdefault("Cache-Control", "no-store")
        elif request.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        if is_production and request.headers.get("X-Forwarded-Proto") == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.errorhandler(403)
    def handle_403(_error):
        return (
            render_template(
                "error.html",
                code=403,
                title="That is not yours to open",
                message="Your role in this circle does not include this. "
                        "Ask the organiser if you think that is wrong.",
            ),
            403,
        )

    @app.errorhandler(404)
    def handle_404(_error):
        return (
            render_template(
                "error.html",
                code=404,
                title="We could not find that",
                message="The link may be out of date, or it may belong to a circle you are not in.",
            ),
            404,
        )

    @app.errorhandler(500)
    def handle_500(_error):
        return (
            render_template(
                "error.html",
                code=500,
                title="Something went wrong on our side",
                message="This is our fault, not yours. Try again in a moment.",
            ),
            500,
        )

    return app
