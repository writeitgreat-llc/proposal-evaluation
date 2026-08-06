#!/usr/bin/env python3
"""check_money_paths.py -- sign-up, sign-in, 2FA, submission and status still work.

This app has ~109 routes and, until this file, not one behavioural test on the
paths that make money. The campaign points every reply at
/author/register; an author who registers, signs in, uploads a proposal and
watches its status is the entire funnel. Any of those breaking is a revenue
outage that nothing in CI would notice -- smoke_app.py renders pages but never
POSTs, and every other check pins one subsystem, not the journey.

Everything here drives the REAL app the way a browser does, with CSRF fully
enabled: fetch the page, pull the rendered token out of it (hidden input for
form POSTs, the base.html <meta> for fetch-style POSTs), and submit with it.
No csrf.exempt, no WTF_CSRF_ENABLED=False. That makes this file double as the
proof that app-wide CSRF did not break the campaign funnel -- and the two
negative probes (a form POST with no token, an /api POST with no header)
prove the protection is actually on, so the token-carrying requests are
passing BECAUSE of the token and not because nothing is checking.

The five paths:

  1. registration -- the exact flow authors.writeitgreat.com/author/register
     serves; asserts a DURABLE author row (fresh query, hashed password);
  2. sign-in -- wrong password first, asserting the v230 lockout counter
     takes the strike; then the correct password, asserting the counters
     clear and the session works;
  3. two-factor -- ONLY the team (AdminUser) carries TOTP; authors and
     publishers have no second factor, so nothing is invented for them.
     Wrong code refused and billed a strike, right code completes;
  4. proposal submission -- a real multipart upload of a real PDF through
     /api/evaluate, asserting the row, the stored bytes (byte-identical),
     the extracted text, and that scoring was dispatched;
  5. status -- /api/status answers by results key (and refuses the guessable
     submission_id), and the author's dashboard, detail page and results
     page all reflect the row just created.

The OpenAI scoring thread is the one thing replaced: a CI check must not
depend on api.openai.com, so process_evaluation_background is swapped for a
recorder BEFORE the upload and the check asserts the dispatch happened with
the right submission_id. The synchronous half -- parse, insert, respond -- is
entirely real; the scoring internals have ci/check_ai_timeouts.py.

FAULT DRILL (2026-08-06): each of these was planted alone, confirmed to fail
this check by name, and reverted -- so a pass means the assertions can lose:
  * app.py author_register: `db.session.add(author)` removed
      -> FAIL "registration left a durable author row..."
  * templates/author_register.html: the csrf_token hidden input removed
      -> FAIL "no csrf_token input rendered on /author/register"
  * app.py author_dashboard: proposals queried with author_id=-1
      -> FAIL "the dashboard lists the new proposal by title"
  * app.py api_evaluate: Proposal(original_file=None)
      -> FAIL "the stored file is byte-identical to the upload"

Plain SQLite on purpose, like every check that imports the app: what this
file pins is route wiring and row durability, not database semantics.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

CHECK_DB = REPO_ROOT / "ci-money-paths.db"
if CHECK_DB.exists():
    CHECK_DB.unlink()

os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ["DATABASE_URL"] = "sqlite:///" + str(CHECK_DB)
os.environ.setdefault("SECRET_KEY", "ci-money-paths-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["MIGRATE_ON_BOOT"] = "0"
# A laptop's shell must not change what this file tests: real Turnstile keys
# would make registration demand a token nothing here can mint, SMTP creds
# would make the register thread send real mail, and a funnel token would
# queue real webhooks. CI never sets any of these; unset them so a local run
# behaves like CI instead of like production.
for _leak in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY",
              "SMTP_USER", "SMTP_PASSWORD", "FUNNEL_EVENTS_TOKEN"):
    os.environ.pop(_leak, None)

import pyotp  # noqa: E402
from reportlab.lib.pagesizes import letter  # noqa: E402
from reportlab.pdfgen import canvas as pdf_canvas  # noqa: E402

import app as appmod  # noqa: E402

failures: list[str] = []
total = 0

AUTHOR_NAME = "Money Path Author"
AUTHOR_EMAIL = "money.path@example.test"
AUTHOR_PASSWORD = "correct-horse-money"
STAFF_EMAIL = "money.staff@example.test"
STAFF_PASSWORD = "correct-horse-staff"
BOOK_TITLE = "The Money Path Method"
# A phrase the PDF carries and the extracted proposal_text must still carry.
SENTINEL = "MONEYPATH-SENTINEL-7f3a"

GENERIC = "Invalid email or password."


def check(condition: bool, message: str) -> None:
    global total
    total += 1
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def bail(message: str) -> None:
    """A precondition every later assertion leans on. Fail loudly and stop --
    running on would bury the cause under thirty consequences."""
    check(False, message)
    finish()


def body(resp) -> str:
    return resp.get_data(as_text=True)


# Same trap and same cure as ci/check_author_lockout.py: one app context wraps
# this whole file, so per-request caches on `g` outlive every simulated
# request. Two of them bite here. flask_wtf caches the CSRF token -- after a
# view clears the session (login and registration both do), the cached token
# matches no session and every later POST would 400. flask_login caches the
# loaded user as g._login_user -- with two clients in play, the author
# client's requests would see whichever account logged in LAST, on either
# client. A real request gets a fresh `g`; give each simulated one the same.
_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def _fresh_g():
    from flask import g
    g.pop("csrf_token", None)
    g.pop("_login_user", None)


def visit(c, path):
    """GET like a real request: no state smuggled in on `g`."""
    _fresh_g()
    return c.get(path)


def _fresh_token(c, page_path, pattern):
    page = visit(c, page_path)
    m = pattern.search(page.get_data(as_text=True))
    if not m:
        bail(f"no csrf_token input rendered on {page_path}")
    return m.group(1)


def form_post(c, page_path, post_path, data):
    """POST a form the way a browser would: fetch the page, carry its token."""
    token = _fresh_token(c, page_path, _CSRF_INPUT_RE)
    return c.post(post_path, data={**data, "csrf_token": token})


def fetch_post(c, post_path, data):
    """POST the way templates/index.html's fetch() does: multipart body plus
    the X-CSRFToken header read from base.html's <meta name="csrf-token">.
    No explicit content_type: the file tuple makes werkzeug build the
    multipart envelope itself, and overriding it would drop the boundary."""
    token = _fresh_token(c, "/", _CSRF_META_RE)
    return c.post(post_path, data=data, headers={"X-CSRFToken": token})


def fresh(model, row_id):
    appmod.db.session.expire_all()
    return appmod.db.session.get(model, row_id)


def build_pdf() -> bytes:
    """A real PDF with enough extractable text to clear the 500-char gate --
    the same shape a campaign author uploads, produced in memory."""
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=letter)
    text = c.beginText(72, 720)
    text.textLine(f"{BOOK_TITLE} -- a book proposal. {SENTINEL}")
    for i in range(14):
        text.textLine(
            f"Chapter {i + 1}: the argument develops, the platform numbers hold, "
            f"and the comparable titles stay honest about their sales.")
    c.drawText(text)
    c.showPage()
    c.save()
    return buf.getvalue()


def finish() -> None:
    if CHECK_DB.exists():
        CHECK_DB.unlink()
    print()
    print(f"{total} checks, {len(failures)} failed")
    if failures:
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All money-path checks passed.")
    sys.exit(0)


# The scoring thread is the one seam: it calls OpenAI, which CI must never
# depend on. Record the dispatch instead -- api_evaluate resolves this name
# from module globals at call time, so the swap takes effect without touching
# the route. Everything up to and including the Thread(...).start() is real.
_dispatched: list[str] = []


def _record_dispatch(app_obj, submission_id, proposal_text, proposal_type,
                     author_name='', book_title='', platform_data=None):
    _dispatched.append(submission_id)


appmod.process_evaluation_background = _record_dispatch


with appmod.app.app_context():
    appmod.db.create_all()

    client = appmod.app.test_client()

    # ── 1. Registration: the page the campaign points every reply at ────────
    print("\n1. Registration creates a durable author account")

    resp = visit(client, "/author/register")
    check(resp.status_code == 200, f"/author/register serves the form (got {resp.status_code})")

    reg_data = {"name": AUTHOR_NAME, "email": AUTHOR_EMAIL,
                "password": AUTHOR_PASSWORD, "confirm_password": AUTHOR_PASSWORD,
                "_gotcha": ""}

    # Token withheld on purpose: proves CSRF is ON for this form, so the
    # token-carrying POST below passes because of its token.
    _fresh_g()
    resp = client.post("/author/register", data=dict(reg_data))
    check(resp.status_code == 302,
          f"a register POST without a token is refused with a retry redirect (got {resp.status_code})")
    check(appmod.Author.query.filter_by(email=AUTHOR_EMAIL).first() is None,
          "...and it created no account")

    resp = form_post(client, "/author/register", "/author/register", reg_data)
    check(resp.status_code == 302 and "/author/" in resp.headers.get("Location", ""),
          f"registering with the rendered token redirects into the portal (got {resp.status_code})")

    appmod.db.session.expire_all()
    author = appmod.Author.query.filter_by(email=AUTHOR_EMAIL).first()
    if author is None:
        bail("registration left a durable author row (fresh query by email)")
    author_id = author.id
    check(True, "registration left a durable author row (fresh query by email)")
    check(author.check_password(AUTHOR_PASSWORD)
          and author.password_hash != AUTHOR_PASSWORD,
          "the password is stored hashed and verifies")
    check(author.email_verified_at is None and author.email_verify_token is not None,
          "the address starts unverified, holding a confirm token")
    check(visit(client, "/author/dashboard").status_code == 200,
          "the new author is signed in and the dashboard answers")

    # ── 2. Sign-in: wrong password bills the lockout, right one clears it ───
    print("\n2. Sign-in: the wrong password costs a strike, the right one works")

    visit(client, "/author/logout")
    check(visit(client, "/author/dashboard").status_code == 302,
          "logout really ends the session")

    resp = form_post(client, "/author/login", "/author/login",
                     {"email": AUTHOR_EMAIL, "password": "wrong-password"})
    check(GENERIC in body(resp), "a wrong password gets the generic error")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 1,
          f"...and the lockout counter took the strike (got {author.failed_login_attempts})")

    resp = form_post(client, "/author/login", "/author/login",
                     {"email": AUTHOR_EMAIL, "password": AUTHOR_PASSWORD})
    check(resp.status_code == 302 and "/login" not in resp.headers.get("Location", ""),
          f"the correct password signs in (got {resp.status_code})")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 0 and author.locked_until is None,
          "a completed sign-in clears the strike")
    check(visit(client, "/author/dashboard").status_code == 200,
          "the session works: the dashboard answers")

    # ── 3. Two-factor: the team is the only account type that carries it ────
    print("\n3. Two-factor on the team login (authors and publishers have none)")

    staff = appmod.AdminUser(name="Money Staff", email=STAFF_EMAIL,
                             role=appmod.ROLE_MEMBER, is_active_account=True,
                             totp_enabled=True, totp_secret=pyotp.random_base32())
    staff.set_password(STAFF_PASSWORD)
    appmod.db.session.add(staff)
    appmod.db.session.commit()
    staff_id = staff.id
    totp = pyotp.TOTP(staff.totp_secret)

    staff_client = appmod.app.test_client()
    resp = form_post(staff_client, "/admin/login", "/admin/login",
                     {"email": STAFF_EMAIL, "password": STAFF_PASSWORD})
    check(resp.status_code == 302 and "verify-2fa" in resp.headers.get("Location", ""),
          "the correct password alone only reaches the TOTP step")
    check(visit(staff_client, "/admin").status_code == 302,
          "...and is NOT signed in yet")

    # A code that cannot be valid in any window verify_totp will consider,
    # even if the 30-second step ticks between here and the POST.
    now = datetime.now(timezone.utc)
    valid = {totp.at(now, offset) for offset in range(-3, 5)}
    wrong_code = next(c for c in ("000000", "111111", "222222") if c not in valid)
    resp = form_post(staff_client, "/admin/verify-2fa", "/admin/verify-2fa",
                     {"totp_code": wrong_code})
    check("Invalid code" in body(resp), "a wrong TOTP code is refused")
    staff = fresh(appmod.AdminUser, staff_id)
    check(staff.failed_login_attempts == 1,
          f"...and costs a strike (got {staff.failed_login_attempts})")

    resp = form_post(staff_client, "/admin/verify-2fa", "/admin/verify-2fa",
                     {"totp_code": totp.now()})
    check(resp.status_code == 302 and resp.headers.get("Location", "").endswith("/admin"),
          "the right TOTP code completes the sign-in")
    check(visit(staff_client, "/admin").status_code == 200,
          "...and the admin dashboard answers")
    staff = fresh(appmod.AdminUser, staff_id)
    check(staff.failed_login_attempts == 0,
          "a completed 2FA login clears the strike")

    # ── 4. Proposal submission: a real multipart PDF through /api/evaluate ──
    print("\n4. Proposal submission stores the row AND the bytes")

    pdf_bytes = build_pdf()
    check(len(pdf_bytes) > 500, f"built a real PDF to upload ({len(pdf_bytes)} bytes)")

    def submission_form():
        # A fresh stream per POST -- werkzeug consumes it.
        return {
            "book_title": BOOK_TITLE,
            "proposal_type": "full",
            "platform_data": json.dumps({"email_list": 12000}),
            "marketing_strategy": "Podcast tour and a launch to the list.",
            "confidentiality_acknowledged": "on",
            "proposal_file": (io.BytesIO(pdf_bytes), "proposal.pdf"),
        }

    # Header withheld on purpose -- the /api/ shape of the same CSRF proof.
    # Identical to the accepted POST below in everything but the header.
    _fresh_g()
    resp = client.post("/api/evaluate", data=submission_form())
    check(resp.status_code == 400 and resp.get_json().get("success") is False,
          f"an upload without the X-CSRFToken header is refused as JSON 400 (got {resp.status_code})")
    check(appmod.Proposal.query.count() == 0, "...and stored nothing")

    resp = fetch_post(client, "/api/evaluate", submission_form())
    js = resp.get_json() or {}
    if not js.get("success"):
        bail(f"the upload was accepted (got {resp.status_code}: {js.get('error')!r})")
    check(bool(js.get("proposal_id") and js.get("results_url") and js.get("status_url")),
          f"the response carries proposal_id, results_url and status_url ({sorted(js)})")

    appmod.db.session.expire_all()
    prop = appmod.Proposal.query.filter_by(submission_id=js["proposal_id"]).first()
    if prop is None:
        bail("the proposal row exists under the returned submission_id")
    check(True, "the proposal row exists under the returned submission_id")
    check(prop.author_id == author_id, "the row is linked to the signed-in author")
    check(prop.book_title == BOOK_TITLE and prop.status == "processing",
          f"title and processing status are stored (got {prop.book_title!r}, {prop.status!r})")
    check(prop.original_filename == "proposal.pdf",
          f"the filename survived (got {prop.original_filename!r})")
    stored = bytes(prop.original_file or b"")
    check(stored == pdf_bytes,
          f"the stored file is byte-identical to the upload ({len(stored)} of {len(pdf_bytes)} bytes)")
    check(SENTINEL in (prop.proposal_text or ""),
          "the PDF text was extracted into proposal_text")
    check(bool(prop.results_token), "the row carries an unguessable results token")
    check(prop.confidentiality_acknowledged_at is not None,
          "the confidentiality acknowledgement was stamped")

    deadline = time.monotonic() + 5
    while not _dispatched and time.monotonic() < deadline:
        time.sleep(0.05)
    check(_dispatched == [prop.submission_id],
          f"scoring was dispatched exactly once for this row (got {_dispatched})")

    # ── 5. Status: every page an author checks reflects the new row ─────────
    print("\n5. Status is visible everywhere the author will look")

    resp = visit(client, js["status_url"])
    st = resp.get_json() or {}
    check(resp.status_code == 200 and st.get("status") == "processing"
          and st.get("ready") is False,
          f"the status poll reports the row as processing (got {st})")
    resp = visit(client, f"/api/status/{prop.submission_id}")
    check(resp.status_code == 404,
          "the guessable submission_id is NOT a status credential for a token-bearing row")

    page = body(visit(client, "/author/dashboard"))
    check(BOOK_TITLE in page, "the dashboard lists the new proposal by title")
    check(appmod.AUTHOR_STATUS_LABELS["processing"] in page,
          "...with the author-friendly status label")

    resp = visit(client, f"/author/proposal/{prop.submission_id}")
    check(resp.status_code == 200 and BOOK_TITLE in body(resp),
          "the author's proposal detail page shows the row")

    resp = visit(client, js["results_url"])
    check(resp.status_code == 200 and "processing-card" in body(resp),
          "the results page answers on the token URL, in its processing state")


finish()
