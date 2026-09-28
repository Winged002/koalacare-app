"""Behavioural tests for the KoalaCare app.

Everything runs against an in-memory MongoDB double, so registration, roles,
the care record, handover, the rule-based reader and the ownership boundaries
are all exercised at the application boundary without a live database.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import mongomock
except ImportError:  # pragma: no cover - the build stage installs it
    mongomock = None

# Importing the app is deliberately unguarded: if it cannot be imported the
# image must fail to build rather than silently skip every test.
from app import care, domain  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_token  # noqa: E402
from app.timing import utcnow  # noqa: E402

MONGO_AVAILABLE = mongomock is not None

PASSWORD = "koalacare-test-password"


@unittest.skipUnless(MONGO_AVAILABLE, "mongomock is not installed")
class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["KOALACARE_SECRET"] = "test-secret"
        os.environ["KOALACARE_ENV"] = "test"
        os.environ.pop("KOALACARE_COOKIE_SECURE", None)
        os.environ.pop("KOALACARE_SEED_DEMO", None)
        os.environ.pop("KOALACARE_UPLOAD_DIR", None)

        self.mongo = mongomock.MongoClient()
        self.app = create_app(mongo_client=self.mongo)
        self.app.config.update(TESTING=True)
        self.db = self.app.extensions["kc_db"]
        self.client = self.app.test_client()

    # ------------------------------------------------------------- helpers

    def csrf_for(self, client) -> str:
        cookie = client.get_cookie("kc_app")
        self.assertIsNotNone(cookie, "expected a session cookie")
        doc = self.db["sessions"].find_one({"token_hash": hash_token(cookie.value)})
        self.assertIsNotNone(doc, "expected a stored session")
        return doc["csrf"]

    def csrf(self) -> str:
        return self.csrf_for(self.client)

    def make_account(self, client, username="andrej"):
        response = client.post(
            "/register",
            data={
                "username": username,
                "email": f"{username}@example.com",
                "name": username.title(),
                "timezone": "Europe/Berlin",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 302)
        return self.db["users"].find_one({"username": username})

    def make_circle(self, client, name="Margaret"):
        response = client.post(
            "/welcome",
            data={
                "name": name,
                "subject_name": name,
                "timezone": "Europe/Berlin",
                "csrf_token": self.csrf_for(client),
            },
        )
        self.assertEqual(response.status_code, 302)
        return self.db["circles"].find_one({"name": name})

    def signed_in(self, username="andrej"):
        """Register and create a circle on the default client, so the shared
        csrf() helper reads the same session."""
        self.make_account(self.client, username)
        circle = self.make_circle(self.client)
        return self.client, circle

    def add_member(self, circle, username, role):
        user = self.db["users"].find_one({"username": username})
        if not user:
            from app import accounts

            user, _error = accounts.create_user(
                self.db, username=username, email=f"{username}@example.com",
                password=PASSWORD, name=username.title(),
            )
        self.db["circle_members"].insert_one(
            {
                "circle_id": circle["_id"],
                "user_id": user["_id"],
                "invited_email": user["email"],
                "role": role,
                "status": "active",
                "joined_at": utcnow(),
            }
        )
        return user

    def login_as(self, username):
        client = self.app.test_client()
        response = client.post(
            "/login", data={"username": username, "password": PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        return client


class ShellTests(BaseCase):
    def test_the_menu_does_not_collapse_after_choosing_a_circle(self):
        client, circle = self.signed_in()
        # Visiting with an explicit circle persists it on the session, which is
        # the code path that previously returned a circle without its role.
        client.get(f"/?circle={circle['_id']}")
        body = client.get("/").get_data(as_text=True)
        for expected in ("/medications", "/documents", "/people", "/handover"):
            self.assertIn(expected, body)

    def test_every_shell_page_renders_for_a_signed_in_user(self):
        client, circle = self.signed_in()
        for path in (
            "/",
            "/updates",
            "/medications",
            "/appointments",
            "/tasks",
            "/documents",
            "/people",
            "/circle",
            "/changes",
            "/handover",
            "/search",
            "/notifications",
            "/settings",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

    def test_a_helper_sees_only_their_own_menu(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "helen", "neighbour")
        helper = self.login_as("helen")
        body = helper.get("/").get_data(as_text=True)
        self.assertIn("/tasks", body)
        self.assertNotIn("/documents", body)
        self.assertNotIn("/medications", body)


class GraphicsTests(BaseCase):
    def test_the_shell_draws_icons_not_bare_text(self):
        client, _circle = self.signed_in()
        body = client.get("/").get_data(as_text=True)
        # Nav, tab bar, bell, wordmark and empty-state art all ship as inline SVG.
        self.assertGreaterEqual(body.count("<svg"), 12)

    def test_the_circle_page_draws_the_care_ring(self):
        client, circle = self.signed_in()
        self.add_member(circle, "priya", "family")
        body = client.get("/circle").get_data(as_text=True)
        self.assertIn("care-ring__centre", body)
        self.assertIn("care-ring__item", body)

    def test_empty_states_render_their_motif_hook(self):
        client, _circle = self.signed_in()
        body = client.get("/documents").get_data(as_text=True)
        self.assertIn("empty__title", body)


class AuthTests(BaseCase):
    def test_health_endpoint_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)

    def test_today_requires_a_session(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_bad_credentials_do_not_disclose_and_do_not_create_a_session(self):
        self.make_account(self.client)
        before = self.db["sessions"].count_documents({})
        fresh = self.app.test_client()
        response = fresh.post("/login", data={"username": "andrej", "password": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)
        # A failed attempt must not mint a session for the new client.
        self.assertEqual(self.db["sessions"].count_documents({}), before)
        self.assertIsNone(fresh.get_cookie("kc_app"))

    def test_unknown_user_looks_identical_to_a_wrong_password(self):
        response = self.client.post(
            "/login", data={"username": "nobody", "password": "whatever-123"}
        )
        self.assertIn(b"Invalid username or password", response.data)

    def test_duplicate_username_is_refused(self):
        self.make_account(self.client)
        other = self.app.test_client()
        response = other.post(
            "/register",
            data={
                "username": "andrej",
                "email": "different@example.com",
                "name": "Someone",
                "timezone": "Europe/Berlin",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"already taken", response.data)

    def test_short_password_is_refused(self):
        response = self.client.post(
            "/register",
            data={
                "username": "shorty",
                "email": "shorty@example.com",
                "name": "Shorty",
                "timezone": "Europe/Berlin",
                "password": "abc",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_password_is_never_stored_in_plaintext(self):
        account = self.make_account(self.client)
        self.assertNotIn(PASSWORD, repr(account))
        self.assertTrue(account["password_hash"])

    def test_logout_revokes_the_session(self):
        self.make_account(self.client)
        self.client.post("/logout", data={"csrf_token": self.csrf()})
        self.assertIsNotNone(self.db["sessions"].find_one({"revoked_at": {"$ne": None}}))
        self.assertIn("/login", self.client.get("/").headers["Location"])


class CircleTests(BaseCase):
    def test_creating_a_circle_makes_you_the_organiser(self):
        _client, circle = self.signed_in()
        self.assertEqual(circle["name"], "Margaret")
        membership = self.db["circle_members"].find_one({"circle_id": circle["_id"]})
        self.assertEqual(membership["role"], "owner")

    def test_a_circle_you_are_not_in_is_not_found(self):
        _client, circle = self.signed_in()
        outsider = self.app.test_client()
        self.make_account(outsider, "outsider")
        self.make_circle(outsider, "Someone else")
        response = outsider.get(f"/medications?circle={circle['_id']}")
        # The circle stays selected as their own, so this is their page, not ours.
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Margaret", response.data)

    def test_invitation_lets_a_new_person_create_an_account(self):
        client, circle = self.signed_in()
        client.post(
            "/circle/invite",
            data={"email": "sister@example.com", "role": "family", "csrf_token": self.csrf()},
        )
        self.assertEqual(self.db["circle_invites"].count_documents({}), 1)
        cookie = client.get_cookie("kc_app")
        session = self.db["sessions"].find_one({"token_hash": hash_token(cookie.value)})
        path = session["reveal"]["path"]

        guest = self.app.test_client()
        page = guest.get(path)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Join Margaret", page.data)

        joined = guest.post(
            path,
            data={"username": "sister", "name": "Sister", "password": "sister-password-1"},
        )
        self.assertEqual(joined.status_code, 302)
        self.assertEqual(
            self.db["circle_members"].count_documents({"circle_id": circle["_id"]}), 2
        )

    def test_only_the_organiser_can_change_roles(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "priya", "family")
        priya = self.login_as("priya")
        member = self.db["circle_members"].find_one({"circle_id": circle["_id"], "role": "family"})
        response = priya.post(
            f"/circle/member/{member['_id']}/role",
            data={"role": "neighbour", "csrf_token": self.csrf_for(priya)},
        )
        self.assertEqual(response.status_code, 403)

    def test_the_organiser_cannot_be_demoted(self):
        client, circle = self.signed_in()
        owner_membership = self.db["circle_members"].find_one(
            {"circle_id": circle["_id"], "role": "owner"}
        )
        client.post(
            f"/circle/member/{owner_membership['_id']}/role",
            data={"role": "family", "csrf_token": self.csrf()},
        )
        refreshed = self.db["circle_members"].find_one({"_id": owner_membership["_id"]})
        self.assertEqual(refreshed["role"], "owner")


class RoleAccessTests(BaseCase):
    def test_a_helper_only_sees_their_own_tasks(self):
        _client, circle = self.signed_in()
        owner = self.db["users"].find_one({"username": "andrej"})
        helen = self.add_member(circle, "helen", "neighbour")

        care.add_task(self.db, circle, owner, title="Owner task", owner_user_id=owner["_id"])
        care.add_task(self.db, circle, owner, title="Helen's errand", owner_user_id=helen["_id"])

        helper = self.login_as("helen")
        body = helper.get("/tasks").get_data(as_text=True)
        self.assertIn("Helen&#39;s errand", body)
        self.assertNotIn("Owner task", body)

    def test_a_helper_cannot_open_documents(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "helen", "neighbour")
        helper = self.login_as("helen")
        self.assertEqual(helper.get("/documents").status_code, 403)

    def test_a_carer_cannot_manage_medications(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "marek", "carer")
        carer = self.login_as("marek")
        self.assertEqual(carer.get("/medications").status_code, 200)
        response = carer.post(
            "/medications", data={"name": "Something", "csrf_token": self.csrf_for(carer)}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db["medications"].count_documents({}), 0)

    def test_a_clinician_sees_medications_but_not_the_people_editor(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "drosei", "clinician")
        clinician = self.login_as("drosei")
        self.assertEqual(clinician.get("/medications").status_code, 200)
        response = clinician.post(
            "/people", data={"name": "Someone", "csrf_token": self.csrf_for(clinician)}
        )
        self.assertEqual(response.status_code, 403)

    def test_navigation_hides_what_the_role_cannot_reach(self):
        _client, circle = self.signed_in()
        self.add_member(circle, "helen", "neighbour")
        helper = self.login_as("helen")
        body = helper.get("/").get_data(as_text=True)
        self.assertNotIn("/documents", body)
        self.assertIn("/tasks", body)


class MedicationTests(BaseCase):
    def test_adding_a_medication_writes_a_history_entry(self):
        client, circle = self.signed_in()
        client.post(
            "/medications",
            data={
                "name": "Ramipril",
                "strength": "5 mg",
                "dose": "1 tablet",
                "times": "08:00",
                "csrf_token": self.csrf(),
            },
        )
        self.assertEqual(self.db["medications"].count_documents({}), 1)
        history = self.db["medication_changes"].find_one({})
        self.assertEqual(history["field"], "started")

    def test_a_dose_can_be_ticked_and_unticked(self):
        client, circle = self.signed_in()
        med = care.add_medication(
            self.db, circle, self.db["users"].find_one({}), name="Metformin", times=["08:00"]
        )
        from app.timing import today_local

        day = today_local("Europe/Berlin").isoformat()
        client.post(
            "/doses",
            data={
                "medication_id": str(med["_id"]),
                "slot": "08:00",
                "on_date": day,
                "action": "taken",
                "csrf_token": self.csrf(),
            },
        )
        self.assertEqual(self.db["dose_marks"].count_documents({}), 1)
        client.post(
            "/doses",
            data={
                "medication_id": str(med["_id"]),
                "slot": "08:00",
                "on_date": day,
                "action": "clear",
                "csrf_token": self.csrf(),
            },
        )
        self.assertEqual(self.db["dose_marks"].count_documents({}), 0)

    def test_stopping_a_medication_is_recorded_not_deleted(self):
        client, circle = self.signed_in()
        med = care.add_medication(self.db, circle, self.db["users"].find_one({}), name="Bisoprolol")
        client.post(
            f"/medications/{med['_id']}/stop",
            data={"note": "Cardiologist stopped it", "csrf_token": self.csrf()},
        )
        stored = self.db["medications"].find_one({"_id": med["_id"]})
        self.assertFalse(stored["active"])
        self.assertIsNotNone(
            self.db["medication_changes"].find_one({"field": "stopped"})
        )


class CareRecordTests(BaseCase):
    def test_finishing_an_appointment_posts_an_update(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        appt = care.add_appointment(
            self.db, circle, owner, title="Cardiology", starts_at=utcnow() - timedelta(hours=2)
        )
        client.post(
            f"/appointments/{appt['_id']}/finish",
            data={"notes_after": "Dose increased to 5 mg", "status": "completed", "csrf_token": self.csrf()},
        )
        stored = self.db["appointments"].find_one({"_id": appt["_id"]})
        self.assertEqual(stored["status"], "completed")
        update = self.db["updates"].find_one({"kind": "appointment"})
        self.assertIn("5 mg", update["body"])

    def test_a_task_can_be_completed_and_counts_towards_the_load(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        task = care.add_task(
            self.db, circle, owner, title="Collect prescription", owner_user_id=owner["_id"]
        )
        client.post(
            f"/tasks/{task['_id']}",
            data={"action": "done", "csrf_token": self.csrf()},
        )
        stored = self.db["tasks"].find_one({"_id": task["_id"]})
        self.assertEqual(stored["status"], "done")
        rows, unassigned = care.care_load(self.db, circle["_id"], 30)
        self.assertEqual(rows[0]["completed"], 1)

    def test_documents_reject_a_disallowed_extension(self):
        import io

        client, circle = self.signed_in()
        response = client.post(
            "/documents",
            data={
                "title": "Nasty",
                "kind": "other",
                "file": (io.BytesIO(b"#!/bin/sh\nrm -rf /"), "payload.sh"),
                "csrf_token": self.csrf(),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db["documents"].count_documents({}), 0)

    def test_a_restricted_document_is_not_visible_to_family(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        care.add_document(
            self.db, circle, owner, title="Power of attorney", kind="poa",
            stored_name="", original_name="poa.pdf",
        )
        family = self.add_member(circle, "priya", "family")
        role = domain.role_of(self.db, circle["_id"], family["_id"])
        self.assertEqual(role, "family")
        visible = domain.visible_documents(
            self.db, circle["_id"], role, family["_id"]
        )
        self.assertEqual(visible, [])
        owner_visible = domain.visible_documents(
            self.db, circle["_id"], "owner", owner["_id"]
        )
        self.assertEqual(len(owner_visible), 1)


class HandoverTests(BaseCase):
    def test_ending_a_visit_creates_a_structured_update_and_tasks(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        care.add_medication(self.db, circle, owner, name="Ramipril", times=["20:00"])

        response = client.post(
            "/handover",
            data={
                "summary": "Margaret ate a good lunch.",
                "observations": "More tired than usual",
                "communication": "Pharmacy called about the prescription",
                "tasks": "Collect prescription tomorrow",
                "task_owner": "",
                "csrf_token": self.csrf(),
            },
        )
        self.assertEqual(response.status_code, 302)

        update = self.db["updates"].find_one({"kind": "handover"})
        self.assertIsNotNone(update)
        kinds = {item["type"] for item in update["items"]}
        self.assertIn("observation", kinds)
        self.assertIn("communication", kinds)
        self.assertEqual(self.db["tasks"].count_documents({}), 1)
        task = self.db["tasks"].find_one({})
        self.assertEqual(task["owner_user_id"], owner["_id"])


class ExtractTests(BaseCase):
    def test_proposals_are_not_saved_until_confirmed(self):
        client, circle = self.signed_in()
        response = client.post(
            "/structure",
            data={
                "text": "doctor increased ramipril to 5mg and wants another blood test in 2 weeks",
                "csrf_token": self.csrf(),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.db["medications"].count_documents({}), 0)
        self.assertEqual(self.db["tasks"].count_documents({}), 0)
        proposal = self.db["proposals"].find_one({})
        self.assertTrue(proposal["items"])
        types = {item["type"] for item in proposal["items"]}
        self.assertIn("medication", types)

    def test_confirming_a_task_proposal_creates_the_task(self):
        client, circle = self.signed_in()
        client.post(
            "/structure",
            data={"text": "Book a blood test", "csrf_token": self.csrf()},
        )
        proposal = self.db["proposals"].find_one({})
        self.assertTrue(proposal["items"])
        client.post(
            f"/proposals/{proposal['_id']}",
            data={"index": "0", "action": "accept", "csrf_token": self.csrf()},
        )
        self.assertEqual(self.db["tasks"].count_documents({}), 1)

    def test_the_reader_finds_a_relative_date(self):
        from app.extract import extract

        proposals = extract("Call the pharmacy in 3 days")
        self.assertTrue(proposals)
        self.assertTrue(proposals[0].get("due_at"))

    def test_the_reader_finds_nothing_in_junk(self):
        from app.extract import extract

        self.assertEqual(extract(""), [])


class ChangeAndSearchTests(BaseCase):
    def test_what_changed_counts_recent_work_and_then_resets(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        task = care.add_task(self.db, circle, owner, title="Errand", owner_user_id=owner["_id"])
        care.set_task_status(self.db, circle, owner, task["_id"], "done")

        body = client.get("/changes").get_data(as_text=True)
        # The stat markup is multi-line, so allow whitespace between the value
        # and its label instead of pinning the template's formatting.
        self.assertRegex(
            body,
            r'<span class="stat__value">1</span>\s*'
            r'<span class="stat__label">Tasks completed</span>',
        )

        # After reading, the marker moves forward and the counts fall back to zero.
        again = client.get("/changes").get_data(as_text=True)
        self.assertRegex(
            again,
            r'<span class="stat__value">0</span>\s*'
            r'<span class="stat__label">Tasks completed</span>',
        )

    def test_search_finds_a_medication_and_reports_no_results_clearly(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        care.add_medication(self.db, circle, owner, name="Ramipril", strength="5 mg")

        found = client.get("/search?q=Ramipril").get_data(as_text=True)
        self.assertIn("Ramipril", found)

        missing = client.get("/search?q=zzzz").get_data(as_text=True)
        self.assertIn("Nothing found", missing)

    def test_search_does_not_cross_circles(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        care.add_medication(self.db, circle, owner, name="SecretDrug")

        other = self.app.test_client()
        self.make_account(other, "outsider")
        self.make_circle(other, "Other")
        body = other.get("/search?q=SecretDrug").get_data(as_text=True)
        # The query is echoed back, so the assertion is on the result set.
        self.assertIn("Nothing found", body)
        self.assertNotIn('<h2 class="card__title">Medications</h2>', body)


class CsrfTests(BaseCase):
    def test_a_mutation_without_a_csrf_token_is_refused(self):
        client, _circle = self.signed_in()
        response = client.post("/medications", data={"name": "Sneaky"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.db["medications"].count_documents({}), 0)

    def test_cross_site_registration_is_refused(self):
        response = self.client.post(
            "/register",
            data={
                "username": "sneaky",
                "email": "sneaky@example.com",
                "name": "Sneaky",
                "timezone": "Europe/Berlin",
                "password": PASSWORD,
            },
            headers={"Origin": "https://evil.test"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.db["users"].find_one({"username": "sneaky"}))


class NotificationTests(BaseCase):
    def test_the_actor_is_not_notified_about_their_own_action(self):
        client, circle = self.signed_in()
        client.post(
            "/updates",
            data={"body": "First update", "kind": "note", "csrf_token": self.csrf()},
        )
        self.assertEqual(self.db["notifications"].count_documents({}), 0)

    def test_other_members_are_notified_and_can_clear_it(self):
        _client, circle = self.signed_in()
        priya = self.add_member(circle, "priya", "family")
        priya_client = self.login_as("priya")
        priya_client.post(
            "/updates",
            data={"body": "I visited today", "kind": "note", "csrf_token": self.csrf_for(priya_client)},
        )

        owner_notifications = self.db["notifications"].find_one(
            {"user_id": {"$ne": priya["_id"]}}
        )
        self.assertIsNotNone(owner_notifications)
        self.assertFalse(owner_notifications["read"])

        owner_client = self.login_as("andrej")
        owner_client.post("/notifications/read", data={"csrf_token": self.csrf_for(owner_client)})
        self.assertEqual(
            self.db["notifications"].count_documents({"read": False}), 0
        )

    def test_repeated_assignment_does_not_duplicate_a_notification(self):
        _client, circle = self.signed_in()
        owner = self.db["users"].find_one({"username": "andrej"})
        daniel = self.add_member(circle, "daniel", "family")
        task = care.add_task(self.db, circle, owner, title="Errand")

        care.assign_task(self.db, circle, owner, task["_id"], daniel["_id"])
        care.assign_task(self.db, circle, owner, task["_id"], daniel["_id"])
        self.assertEqual(
            self.db["notifications"].count_documents({"user_id": daniel["_id"]}), 1
        )


class TemplateIntegrityTests(unittest.TestCase):
    """Guards a class of bug the behavioural tests cannot see.

    The app's CSP is script-src 'self', so an inline event handler such as
    onchange="this.form.submit()" never runs in a browser. A control can look
    wired up in the markup and still be dead. That is exactly what happened to
    the circle switcher and the member role select: both endpoints were tested
    by POSTing to them, so the tests passed while the on-screen control did
    nothing. These checks fail the build instead.
    """

    ROOT = Path(__file__).resolve().parents[1]
    TEMPLATES = ROOT / "app" / "templates"
    INLINE_HANDLER = re.compile(r"\son[a-z]{3,}\s*=", re.IGNORECASE)

    def test_templates_use_no_inline_event_handlers(self):
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in self.INLINE_HANDLER.finditer(text):
                offenders.append(
                    f"{path.relative_to(self.TEMPLATES)}: {match.group(0).strip()}"
                )
        self.assertEqual(
            offenders,
            [],
            "inline handlers are blocked by CSP script-src 'self' and never fire: "
            + "; ".join(offenders),
        )

    def test_auto_submit_selects_have_a_no_javascript_fallback(self):
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            if "data-autosubmit" in text and "data-autosubmit-fallback" not in text:
                offenders.append(str(path.relative_to(self.TEMPLATES)))
        self.assertEqual(
            offenders,
            [],
            "a data-autosubmit select must ship a visible submit fallback so the "
            "control still works without JavaScript: " + ", ".join(offenders),
        )

    def test_the_mobile_tab_bar_is_not_pinned_to_five_columns(self):
        css = (self.ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")
        self.assertNotIn(
            "repeat(5, 1fr)",
            css,
            "the tab bar renders 3-5 items depending on role; a fixed 5-column "
            "grid leaves dead space for the neighbour (3) and clinician (4) roles",
        )

    def test_templates_use_no_inline_style_attributes(self):
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\sstyle\s*=", text):
                offenders.append(
                    f"{path.relative_to(self.TEMPLATES)}: {match.group(0).strip()}"
                )
        self.assertEqual(
            offenders,
            [],
            "CSP is style-src 'self' with no unsafe-inline, so a style= attribute "
            "is silently ignored and the markup lies about the spacing; put the "
            "rule in app.css instead: " + "; ".join(offenders),
        )

    def test_the_brand_mark_is_defined_once(self):
        offenders = []
        for path in sorted(self.TEMPLATES.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            if 'viewBox="0 0 32 32"' in text and path.name != "icons.html":
                offenders.append(str(path.relative_to(self.TEMPLATES)))
        self.assertEqual(
            offenders,
            [],
            "the koala mark lives in the icons macro; call brand() rather than "
            "copying the SVG again: " + ", ".join(offenders),
        )


class DesignTokenTests(unittest.TestCase):
    """The app hand-copies the landing page's palette into :root, so the two can
    drift apart.

    A cross-repo test cannot work here: the Docker build context is only
    koalacare-app/, so a test that read ../koalacare-landing would pass locally
    and then fail the image build. Instead this mirrors the landing's colour
    tokens and fails when app.css hardcodes one of those values in a rule
    instead of using var(). That is the --success-line bug: the token was never
    copied over, so .alert--good quietly hardcoded #b6e0c8.
    """

    ROOT = Path(__file__).resolve().parents[1]
    CSS = ROOT / "app" / "static" / "css" / "app.css"

    # Mirrors the colour families in koalacare-landing/app/static/css/tokens.css.
    # Regenerate when the landing palette changes.
    LANDING_COLOURS = {
        "--brand-900": "#052b27", "--brand-800": "#08443d", "--brand-700": "#0d5c53",
        "--brand-600": "#12756a", "--brand-500": "#1a9082", "--brand-300": "#7cc4b8",
        "--brand-200": "#b3ddd4", "--brand-100": "#dff0eb", "--brand-050": "#f0f8f5",
        "--accent-700": "#7a4c07", "--accent-500": "#d08a24",
        "--accent-200": "#f2d9ae", "--accent-100": "#fdf2e0",
        "--ink-900": "#0f2226", "--ink-800": "#173236", "--ink-700": "#21464a",
        "--ink-600": "#35595d", "--ink-500": "#507579", "--ink-400": "#7a9699",
        "--ink-300": "#a7bcbd",
        "--line-300": "#cfdad7", "--line-200": "#dfe7e4", "--line-100": "#edf2f0",
        "--sand-050": "#fcfaf7", "--sand-100": "#f6f3ee", "--sand-200": "#efeae2",
        "--white": "#ffffff",
        "--success-800": "#0f5133", "--success-100": "#e0f3e8", "--success-line": "#b6e0c8",
        "--danger-800": "#8a1f17", "--danger-100": "#fce9e7", "--danger-line": "#f0c4bf",
        "--warn-800": "#6f4506", "--warn-100": "#fdf0da", "--warn-line": "#eed6a8",
    }

    def test_no_rule_hardcodes_a_palette_value(self):
        css = self.CSS.read_text(encoding="utf-8")
        # Drop the leading :root block, where the literals belong.
        start = css.index(":root")
        body = css[css.index("}", start) + 1:].lower()
        offenders = [
            f"{value} (use var({name}))"
            for name, value in self.LANDING_COLOURS.items()
            if value.lower() in body
        ]
        self.assertEqual(
            offenders,
            [],
            "a palette value is hardcoded in a rule instead of using its token, "
            "so it will drift from the landing page: " + "; ".join(offenders),
        )


class ActionFeedbackTests(BaseCase):
    """A write used to end in a bare redirect, so the app never said whether the
    thing you just did actually happened. The notice travels on the redirect as
    ?ok=..., which means it belongs to exactly one page load and nothing has to
    be stored or cleared afterwards."""

    def test_adding_a_medication_confirms_it_on_the_page_it_lands_on(self):
        client, _circle = self.signed_in()
        response = client.post(
            "/medications",
            data={"name": "Ramipril", "times": "08:00", "csrf_token": self.csrf()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Medication added to the record", response.data)

    def test_the_confirmation_does_not_follow_you_around(self):
        client, _circle = self.signed_in()
        client.post(
            "/medications", data={"name": "Ramipril", "csrf_token": self.csrf()}
        )
        body = client.get("/medications").get_data(as_text=True)
        self.assertNotIn("Medication added to the record", body)

    def test_an_invented_notice_key_renders_nothing(self):
        client, _circle = self.signed_in()
        body = client.get("/medications?ok=nonsense").get_data(as_text=True)
        # The key is looked up in a fixed table, never echoed back.
        self.assertNotIn("nonsense", body)
        self.assertNotIn("alert--good", body)

    def test_finishing_a_task_from_today_returns_to_today_and_still_confirms(self):
        client, circle = self.signed_in()
        owner = self.db["users"].find_one({})
        task = care.add_task(self.db, circle, owner, title="Collect prescription")

        response = client.post(
            f"/tasks/{task['_id']}",
            data={"action": "done", "next": "/", "csrf_token": self.csrf()},
        )
        self.assertEqual(response.status_code, 302)
        # The caller's own destination has to survive the notice being added.
        self.assertEqual(response.headers["Location"], "/?ok=task-done")

        followed = client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Task marked done", followed)


class LayoutConsistencyTests(unittest.TestCase):
    """Guards the shared shell and stylesheet, which every page inherits."""

    ROOT = Path(__file__).resolve().parents[1]
    TEMPLATES = ROOT / "app" / "templates"
    CSS = ROOT / "app" / "static" / "css" / "app.css"

    def shell(self) -> str:
        return (self.TEMPLATES / "shell.html").read_text(encoding="utf-8")

    def test_the_page_offers_a_skip_link(self):
        shell = self.shell()
        self.assertIn('class="skip-link"', shell)
        self.assertIn('href="#main"', shell)
        self.assertIn('id="main"', shell)

    def test_the_circle_switcher_exists_outside_the_app_bar(self):
        shell = self.shell()
        # The app bar is display:none from 900px up, so a switcher that only
        # lives inside it is unreachable on a desktop.
        before_app_bar = shell.split('<header class="appbar">')[0]
        self.assertIn(
            "care.switch_circle",
            before_app_bar,
            "a desktop user in more than one circle needs a switcher the app bar "
            "does not hide",
        )

    def test_the_focus_ring_does_not_reshape_what_it_lands_on(self):
        css = self.CSS.read_text(encoding="utf-8")
        start = css.index("\n:focus-visible {")
        block = css[start : css.index("}", start)]
        self.assertNotIn(
            "border-radius",
            block,
            "the ring is a box-shadow and already follows the element's own "
            "radius; declaring one here squares off every round control on focus",
        )

    def test_the_app_ships_a_print_stylesheet(self):
        css = self.CSS.read_text(encoding="utf-8")
        self.assertIn("@media print", css)
        self.assertIn(".tabbar", css.split("@media print")[1])

    def test_hover_states_are_scoped_to_pointer_devices(self):
        css = self.CSS.read_text(encoding="utf-8")
        # A touch device latches :hover on whatever was tapped, which leaves a
        # row looking permanently selected.
        self.assertIn("@media (hover: hover)", css)
        guard = "@media (hover: hover)"
        for selector in (".row:hover {", ".dose:hover {", ".person:hover {"):
            offset = css.index(selector)
            opened_at = css[:offset].rindex(guard)
            # Nothing may close the block between the guard and the rule, or the
            # rule would sit outside it while still finding a guard above.
            between = css[opened_at + len(guard) : offset]
            self.assertNotIn(
                "}",
                between,
                f"{selector} must sit inside a {guard} block",
            )


class SeedPostureTests(BaseCase):
    """Seeding is a deliberate act: a fresh database stays empty unless the
    operator enables it and supplies a password, and the old built-in demo
    password is gone from the repository.
    """

    DEMO_PASSWORD = "seed-posture-test-password"

    def setUp(self):
        super().setUp()
        self.addCleanup(self._clear_seed_env)

    def _clear_seed_env(self):
        os.environ.pop("KOALACARE_SEED_DEMO", None)
        os.environ.pop("KOALACARE_DEMO_PASSWORD", None)

    def boot(self, seed=None, password=None, mongo=None):
        """Boot a fresh app against a fresh in-memory MongoDB."""
        if seed is None:
            os.environ.pop("KOALACARE_SEED_DEMO", None)
        else:
            os.environ["KOALACARE_SEED_DEMO"] = seed
        if password is None:
            os.environ.pop("KOALACARE_DEMO_PASSWORD", None)
        else:
            os.environ["KOALACARE_DEMO_PASSWORD"] = password
        self.mongo = mongo if mongo is not None else mongomock.MongoClient()
        self.app = create_app(mongo_client=self.mongo)
        self.app.config.update(TESTING=True)
        self.db = self.app.extensions["kc_db"]
        self.client = self.app.test_client()
        return self.client

    def assert_login_refused(self, client, password):
        response = client.post(
            "/login", data={"username": "andrej", "password": password}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid username or password", response.data)
        self.assertIsNone(client.get_cookie("kc_app"))

    def test_a_fresh_install_stays_empty_and_claimable(self):
        # The production posture: nothing requested, so nothing is created.
        with self.assertLogs("app.main", level="INFO") as captured:
            client = self.boot()
        self.assertTrue(any("demo seeding is off" in line for line in captured.output))
        self.assertEqual(self.db["users"].count_documents({}), 0)
        self.assertEqual(self.db["circles"].count_documents({}), 0)
        # The old repository default must not open anything on an empty database.
        self.assert_login_refused(client, "koalacare-demo-2026")
        # The first real person can still claim the product.
        self.assertIsNotNone(self.make_account(client))

    def test_seeding_is_refused_without_an_explicit_password(self):
        # Opted in, but no password to create accounts with: fail closed.
        with self.assertLogs("app.main", level="WARNING") as captured:
            client = self.boot(seed="1")
        self.assertTrue(
            any("KOALACARE_DEMO_PASSWORD" in line for line in captured.output)
        )
        self.assertEqual(self.db["users"].count_documents({}), 0)
        self.assert_login_refused(client, "koalacare-demo-2026")

    def test_seeding_builds_the_demo_circle_with_the_configured_password(self):
        with self.assertLogs("app.main", level="INFO") as captured:
            client = self.boot(seed="1", password=self.DEMO_PASSWORD)
        self.assertTrue(
            any("seeded the demo circle" in line for line in captured.output)
        )
        self.assertIsNotNone(self.db["circles"].find_one({"name": "Margaret"}))
        self.assertGreaterEqual(self.db["users"].count_documents({}), 6)
        # The seeded accounts answer to the configured password...
        response = client.post(
            "/login", data={"username": "andrej", "password": self.DEMO_PASSWORD}
        )
        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(client.get_cookie("kc_app"))
        # ...and never to the old repository default.
        self.assert_login_refused(self.app.test_client(), "koalacare-demo-2026")

    def test_seeding_never_touches_a_database_that_already_has_a_real_circle(self):
        client = self.boot()
        self.make_account(client)
        self.make_circle(client, name="Our family")
        with self.assertLogs("app.main", level="INFO") as captured:
            self.boot(seed="1", password=self.DEMO_PASSWORD, mongo=self.mongo)
        self.assertTrue(
            any("already has a real circle" in line for line in captured.output)
        )
        self.assertEqual(self.db["circles"].count_documents({}), 1)
        self.assertEqual(self.db["users"].count_documents({}), 1)

    def test_no_repository_file_ships_a_default_demo_password(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("compose.yaml", "app/main.py", "README.md"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("koalacare-demo-2026", text, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
