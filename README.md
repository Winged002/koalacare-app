# KoalaCare — the care coordination app

The working product, served at **https://my.koalacare.app**.

KoalaCare is the operating system for looking after someone. The mental model is a
combination of a family group chat, a medication organiser, a shared calendar, a
task manager, a document vault and a care handover system — built specifically for
care, so the main user is the adult daughter coordinating her mother's care while a
brother, a paid carer and a district nurse each see only what their role allows.

The public marketing site is a **separate application and a separate repository**
(`koalacare-landing`). This repository is the product itself.

## What this is

A server-rendered Flask application on MongoDB, deliberately usable with JavaScript
disabled.

| | |
| --- | --- |
| Language | Python 3.12 |
| Framework | Flask 3 + Jinja templates, no client framework |
| Datastore | MongoDB 7 (named volume) |
| Server | gunicorn, 2 workers × 8 threads |
| Runtime | Docker Compose, `127.0.0.1:8792` → container `8000` |
| Health | `GET /healthz` → `200 {"status": "ok"}` |

## Surfaces

Every route below is member-scoped: each one re-checks membership of the circle it
is about, so a signed-in user can never open another circle's records.

| Route | What it does |
| --- | --- |
| `/` | **Today** — the next 24 hours: doses with tick-to-confirm, today's appointments, what still needs attention, tasks due soon, and anything with nobody's name on it |
| `/updates` | The chronological care feed, filterable, with structured entries rendered as separate lines |
| `/medications` | Name, strength, dose, times, purpose, supply, repeat — plus a permanent change history that is appended to and never edited |
| `/appointments`, `/appointments/<id>` | Location, who is coming, who is driving, questions to ask, documents to bring, and "after" notes that post to Updates automatically |
| `/tasks` | One owner, a due date, a priority, done/reopen/reassign, and per-person load |
| `/documents`, `/documents/<id>/file` | Real uploads to a named volume (8 MB cap, magic-byte checked, executable extensions refused, stored under a generated name) |
| `/people` | The directory of professionals and contacts — GP, specialist, pharmacy |
| `/circle` | Who has access, their role, a plain-language table of what each role can open, the invite flow, and 30-day care load per person |
| `/handover` | End-of-visit handover so the next carer is not told twice |
| `/changes` | Everything that changed, derived from the audit trail |
| `/proposals`, `/proposals/<id>` | Change requests that need the organiser's approval before they take effect |
| `/search` | Bounded search across the circle's records |
| `/notifications`, `/notifications/read` | Persistent in-app notifications with unread state |
| `/settings`, `/structure` | Personal settings, password change, circle structure |
| `/login`, `/register`, `/logout`, `/welcome`, `/invite/<token>` | Accounts, first-run onboarding, invitations |

## Roles

Access is a small, readable capability matrix (`app/domain.py`) rather than a pile of
per-route checks. A neighbour picking someone up on Thursday holds exactly two
capabilities, so no route can accidentally hand them a medical history.

| Role | Label | Reaches |
| --- | --- | --- |
| `owner` | Organiser | Everything, including restricted documents and who else has access |
| `family` | Family | Everything except restricted documents such as powers of attorney, and except changing who is in the circle |
| `carer` | Carer | Today, medications, tasks and the notes needed to do the visit |
| `clinician` | Doctor or clinic | Medication list, medical documents and appointment notes |
| `neighbour` | Helper | Only the tasks assigned to them |

## Running it

```bash
docker compose up --build -d
curl -fsS http://127.0.0.1:8792/healthz
```

The image build runs the full test suite in a build stage, so a failing test fails
the image rather than reaching the deployment.

Without Docker:

```bash
pip install -r requirements.txt -r requirements-dev.txt
mongod --dbpath ./data          # or point MONGODB_URI at a running server
KOALACARE_SECRET=dev-secret python -c "from app.main import create_app; create_app().run(port=8792)"
python -m unittest discover -s tests -v
```

## Configuration

All configuration is environment based.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MONGODB_URI` | `mongodb://127.0.0.1:27017` | Database connection. Compose sets `mongodb://mongo:27017`. |
| `MONGODB_DB` | `koalacare_app` | Database name. |
| `KOALACARE_SECRET` | dev fallback | Application secret. **Set this in production.** |
| `KOALACARE_ENV` | unset | `production` turns on secure cookies and HSTS. |
| `KOALACARE_COOKIE_SECURE` | follows `KOALACARE_ENV` | Override to `false` to reach the app over plain HTTP locally. |
| `KOALACARE_CANONICAL_HOST` | `my.koalacare.app` | The one origin that is served and indexed. |
| `KOALACARE_ALIAS_HOSTS` | empty | Comma-separated hosts that permanently redirect to the canonical host (`301` for `GET`/`HEAD`, `308` otherwise). |
| `KOALACARE_UPLOAD_DIR` | `/data/uploads` | Where uploaded documents are written. |
| `KOALACARE_SEED_DEMO` | `0` (off) | Opt in (`1`) to seed a populated demo circle. Production stays `0`; staging/dev enable it explicitly. |
| `KOALACARE_DEMO_USER` / `KOALACARE_DEMO_PASSWORD` | `andrej` / *(none)* | Credentials for the seeded demo accounts. There is no built-in password: seeding is skipped unless `KOALACARE_DEMO_PASSWORD` is set explicitly. |
| `PORT` | `8000` | Container port. |

## Security posture and known gaps

* Passwords are hashed with Werkzeug's platform default (scrypt) and never logged or
  echoed. Failed sign-ins always spend a hash comparison, so timing does not reveal
  whether an account exists.
* Sessions are server-side: the browser holds an opaque token, MongoDB stores only
  its SHA-256, so a database dump yields nothing a caller can present. 24-hour expiry.
* Every mutation is a `POST` with a per-session CSRF token, plus an origin check and
  `SameSite=Lax` cookies. Sign-in is rate limited (12 attempts / 5 minutes per IP).
* Responses carry a strict CSP (`default-src 'self'`), `X-Frame-Options: DENY`,
  `nosniff`, a referrer policy and a permissions policy. HTML is `no-store`.
* The web container is read-only, `cap_drop: ALL`, `no-new-privileges`, has a 32 MB
  tmpfs for `/tmp`, and never receives the Docker socket. MongoDB is on the Compose
  network and is **not** published to the host.
* **Deliberate gaps in this build:** MongoDB runs without authentication inside the
  Compose network, and invite tokens appear in URL paths and can therefore land in
  access logs. Demo accounts are only created when an operator deliberately sets
  `KOALACARE_SEED_DEMO=1` **and** `KOALACARE_DEMO_PASSWORD`; a fresh production
  database starts empty and is claimed by its first real user.
* `app/_context_snippet.py` is an inert, superseded module kept only because the
  workspace tooling that built this project cannot delete files. It is safe to remove.

## Repository layout

```
app/
  main.py          application factory, seeding, security headers, error pages
  views_auth.py    sign in, sign up, invitations, first-run onboarding
  views_care.py    every care surface
  care.py          the care domain: medications, doses, tasks, documents, handover
  domain.py        circles, membership, the role capability matrix
  accounts.py      users and authentication
  security.py      sessions, CSRF, rate limiting, password policy, audit trail
  extract.py       structured extraction used by the proposals flow
  timing.py        timezone handling and relative dates
  db.py            MongoDB connection and indexes
  templates/       Jinja templates and the shared shell
  static/          app.css, app.js and the favicon
tests/test_app.py  shell, auth, circle, role access, medication and care-record tests
```
