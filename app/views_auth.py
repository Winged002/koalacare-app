"""Sign in, sign up, invitations and first-run onboarding."""

from __future__ import annotations

from flask import Blueprint, current_app, g, redirect, render_template, request, url_for

from . import accounts, domain
from .accounts import authenticate
from .security import (
    RateLimiter,
    apply_session_cookie,
    clear_session_cookie,
    client_ip,
    current_user,
    end_session,
    is_cross_site,
    login_required,
    new_token,
    password_problem,
    safe_next,
    start_session,
)
from .timing import COMMON_TIMEZONES, DEFAULT_TIMEZONE

bp = Blueprint("auth", __name__)

login_limiter = RateLimiter(12, 300.0)


def _db():
    return current_app.extensions["kc_db"]


def _limiter():
    return current_app.extensions["kc_login_limiter"]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("care.today"))
    next_url = safe_next(request.values.get("next"), url_for("care.today"))
    error = ""
    status = 200
    username = ""

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not _limiter().allow(f"login:{client_ip()}"):
            error = "Too many attempts. Wait a few minutes, then try again."
            status = 429
        else:
            user = authenticate(_db(), username, password)
            if user:
                raw = start_session(_db(), user)
                return apply_session_cookie(redirect(next_url), raw)
            error = "Invalid username or password."
    return (
        render_template("login.html", error=error, next_url=next_url, username=username),
        status,
    )


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("care.today"))
    error = ""
    values = {"username": "", "email": "", "name": ""}

    if request.method == "POST":
        if is_cross_site():
            error = "That request did not come from this site."
        else:
            username = (request.form.get("username") or "").strip()
            email = (request.form.get("email") or "").strip()
            name = (request.form.get("name") or "").strip()
            password = request.form.get("password") or ""
            timezone = request.form.get("timezone") or DEFAULT_TIMEZONE
            values = {"username": username, "email": email, "name": name}

            problem = password_problem(
                password, username=username.lower(), email=email.lower()
            )
            if problem:
                error = problem
            else:
                user, error = accounts.create_user(
                    _db(),
                    username=username,
                    email=email,
                    password=password,
                    name=name,
                    timezone=timezone,
                )
                if user:
                    raw = start_session(_db(), user)
                    return apply_session_cookie(redirect(url_for("auth.welcome")), raw)

    return (
        render_template(
            "register.html",
            error=error,
            values=values,
            timezones=COMMON_TIMEZONES,
            default_timezone=DEFAULT_TIMEZONE,
        ),
        400 if error else 200,
    )


@bp.post("/logout")
def logout():
    if current_user():
        end_session(_db())
    return clear_session_cookie(redirect(url_for("auth.login")))


@bp.route("/welcome", methods=["GET", "POST"])
@login_required
def welcome():
    """First run: either accept the invite you came for, or start a circle."""
    user = current_user()
    db = _db()

    if domain.circles_for_user(db, user["_id"]):
        return redirect(url_for("care.today"))

    error = ""
    waiting = domain.invites_waiting_for(db, user.get("email", ""))

    if request.method == "POST":
        from .security import require_csrf

        require_csrf()
        name = (request.form.get("name") or "").strip()
        subject = (request.form.get("subject_name") or "").strip()
        timezone = request.form.get("timezone") or user.get("timezone") or DEFAULT_TIMEZONE
        if not name:
            error = "Give the circle a name, such as “Mum” or “Margaret”s circle”."
        else:
            circle = domain.create_circle(
                db, user, name=name, subject_name=subject, timezone=timezone
            )
            return redirect(url_for("care.today", circle=str(circle["_id"])))

    return render_template(
        "welcome.html",
        error=error,
        waiting=waiting,
        timezones=COMMON_TIMEZONES,
        default_timezone=user.get("timezone") or DEFAULT_TIMEZONE,
    )


@bp.route("/invite/<token>", methods=["GET", "POST"])
def invite(token: str):
    """An invitation is itself the permission to create an account."""
    db = _db()
    found, circle, state = domain.invite_state(db, token)
    user = current_user()

    if state == "unknown":
        return render_template("invite.html", state="unknown"), 404

    if user:
        if state != "open":
            return render_template("invite.html", state=state)
        joined = domain.accept_invite(db, found, circle, user)
        if joined:
            from .care import notify

            notify(
                db,
                circle.get("created_by"),
                title=f"{accounts.display_name(user)} joined {circle['name']}",
                body="They accepted your invitation.",
                link=url_for("care.circle"),
                circle_id=circle["_id"],
                actor_id=user["_id"],
            )
        return redirect(url_for("care.today", circle=str(circle["_id"])))

    if state in ("used", "expired"):
        return render_template("invite.html", state=state, circle=circle)

    error = ""
    values = {"username": "", "name": ""}

    if request.method == "POST":
        if is_cross_site():
            error = "That request did not come from this site."
        else:
            username = (request.form.get("username") or "").strip()
            name = (request.form.get("name") or "").strip()[:80]
            password = request.form.get("password") or ""
            values = {"username": username, "name": name}
            problem = password_problem(
                password, username=username.lower(), email=found.get("email", "")
            )
            if problem:
                error = problem
            else:
                created, error = accounts.create_user(
                    db,
                    username=username,
                    email=found.get("email", ""),
                    password=password,
                    name=name or found.get("email", "").split("@")[0],
                )
                if created:
                    domain.accept_invite(db, found, circle, created)
                    raw = start_session(db, created)
                    return apply_session_cookie(
                        redirect(url_for("care.today")), raw
                    )

    return (
        render_template(
            "invite.html",
            state="register",
            invite=found,
            circle=circle,
            error=error,
            values=values,
        ),
        400 if error else 200,
    )
