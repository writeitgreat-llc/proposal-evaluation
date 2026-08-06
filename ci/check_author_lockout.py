#!/usr/bin/env python3
"""check_author_lockout.py -- the sign-in lockout holds on all three account types.

The author and publisher login forms accepted unlimited password guesses from
anyone on the internet, and the staff lockout that did exist had two faults
worth pinning forever:

  * its counter never reset after a lockout expired, so the very next mistype
    re-locked the account -- a door that never actually reopens, it just
    pauses between punishments;
  * it cleared the counter the moment the PASSWORD matched, before the TOTP
    step, so anyone holding the password could reset the strikes between code
    guesses by re-POSTing the login form -- five free 2FA guesses per
    re-entry, forever.

All three models now share LoginLockoutMixin. What this file pins:

  1. five wrong passwords cost five generic errors; the sixth is refused as
     LOCKED, not as wrong; the CORRECT password is also refused while locked;
     and the strikes live in the database, not process memory;
  1b. every lockout timestamp is written NAIVE UTC. This is the assertion
     that means something on the database the check actually runs against:
     an aware value expires a lock instantly on a non-UTC Postgres session
     while behaving perfectly on SQLite, so asserting the WRITE is the only
     form of this check that cannot pass while production fails open;
  2. an expired lock grants a fresh five (counter restarts at 1, no instant
     re-lock) while the ladder rung is KEPT -- and 12 quiet hours forget both
     the strikes and the rung;
  2b. the second lockout climbs the ladder (30 minutes, not 15);
  3. a burst of stragglers against an already-locked account moves neither
     counter and cannot climb the ladder;
  4. the per-caller login throttle refuses after its allowance and the peer
     bucket is being counted;
  5. staff: a correct password does NOT clear the counter while 2FA is
     pending, a wrong TOTP code costs a strike, and only a COMPLETED 2FA
     login clears everything;
  6. publisher: locked after five, correct password refused while locked.

Plain SQLite on purpose -- the FOR UPDATE row lock is a documented no-op here;
what SQLite can prove is the model arithmetic and the route wiring, and 1b is
what keeps the Postgres-only failure mode covered.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

CHECK_DB = REPO_ROOT / "ci-lockout.db"
if CHECK_DB.exists():
    CHECK_DB.unlink()

os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ["DATABASE_URL"] = "sqlite:///" + str(CHECK_DB)
os.environ.setdefault("SECRET_KEY", "ci-lockout-test-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["MIGRATE_ON_BOOT"] = "0"

import pyotp  # noqa: E402

import app as appmod  # noqa: E402

failures: list[str] = []
total = 0

GENERIC = "Invalid email or password."
LOCKED_FRAGMENT = "This account is locked"
THROTTLED_FRAGMENT = "Too many sign-in attempts from this connection."
ADMIN_LOCKED_FRAGMENT = "Account locked due to too many failed attempts"

AUTHOR_EMAIL = "lockout.author@example.test"
AUTHOR_PASSWORD = "correct-horse-author"
STAFF_EMAIL = "lockout.staff@example.test"
STAFF_PASSWORD = "correct-horse-staff"
PUB_EMAIL = "lockout.publisher@example.test"
PUB_PASSWORD = "correct-horse-publisher"


def check(condition: bool, message: str) -> None:
    global total
    total += 1
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def clear_login_buckets() -> None:
    appmod._login_rate.clear()
    appmod._login_peer_rate.clear()


def body(resp) -> str:
    return resp.get_data(as_text=True)


def fresh(model, row_id):
    appmod.db.session.expire_all()
    return appmod.db.session.get(model, row_id)


with appmod.app.app_context():
    appmod.db.create_all()

    author = appmod.Author(name="Lockout Author", email=AUTHOR_EMAIL)
    author.set_password(AUTHOR_PASSWORD)
    author.email_verified_at = datetime.utcnow()
    appmod.db.session.add(author)

    staff = appmod.AdminUser(name="Lockout Staff", email=STAFF_EMAIL,
                             role=appmod.ROLE_MEMBER, is_active_account=True,
                             totp_enabled=True, totp_secret=pyotp.random_base32())
    staff.set_password(STAFF_PASSWORD)
    appmod.db.session.add(staff)

    pub = appmod.Publisher(name="Lockout Publisher", email=PUB_EMAIL,
                           company="Example House", is_approved=True,
                           is_active_account=True)
    pub.set_password(PUB_PASSWORD)
    appmod.db.session.add(pub)
    appmod.db.session.commit()
    author_id, staff_id, pub_id = author.id, staff.id, pub.id

    client = appmod.app.test_client()

    def post_author(email, password):
        return client.post("/author/login", data={"email": email, "password": password})

    print("\n1. Five wrong passwords, then the door is locked to everyone")
    clear_login_buckets()
    generic_seen = 0
    for _ in range(5):
        if GENERIC in body(post_author(AUTHOR_EMAIL, "wrong-password")):
            generic_seen += 1
    check(generic_seen == 5, f"five wrong passwords -> five generic errors (got {generic_seen})")

    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 5,
          f"strikes persisted to the database, not memory (got {author.failed_login_attempts})")
    check(author.locked_until is not None, "fifth strike sets locked_until")
    check(author.lockout_count == 1, f"first lockout is rung 1 (got {author.lockout_count})")

    page = body(post_author(AUTHOR_EMAIL, "wrong-password"))
    check(LOCKED_FRAGMENT in page and GENERIC not in page,
          "sixth attempt refused as locked, not as wrong")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 5,
          "an attempt against a locked account moves nothing")

    page = body(post_author(AUTHOR_EMAIL, AUTHOR_PASSWORD))
    check(LOCKED_FRAGMENT in page,
          "the CORRECT password is also refused while locked")
    check(client.get("/author/dashboard").status_code == 302,
          "and it did not sign anyone in")
    author = fresh(appmod.Author, author_id)
    check(author.lockout_count == 1, "a refused correct password does not climb the ladder")

    print("\n1b. Lockout timestamps are stored naive UTC (the Postgres trap)")
    for name, value in (("locked_until", author.locked_until),
                        ("last_failed_login_at", author.last_failed_login_at)):
        check(value is not None and value.tzinfo is None,
              f"{name} is stored naive (an aware value expires the lock instantly on Postgres)")
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    drift = abs((author.last_failed_login_at - now_utc).total_seconds())
    check(drift < 60, f"...and it really is UTC, not local time (drift {drift:.0f}s)")
    lock_drift = abs((author.locked_until - (now_utc + timedelta(minutes=15))).total_seconds())
    check(lock_drift < 60, f"first lock is the 15-minute rung (drift {lock_drift:.0f}s)")

    print("\n2. An expired lock grants a fresh five; long quiet forgets the ladder")
    author.locked_until = datetime.utcnow() - timedelta(seconds=1)
    appmod.db.session.commit()
    clear_login_buckets()
    page = body(post_author(AUTHOR_EMAIL, "wrong-password"))
    check(GENERIC in page and LOCKED_FRAGMENT not in page,
          "first mistype after expiry is a plain wrong password, not a re-lock")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 1,
          f"counter restarted at 1 (the permanent-purgatory bug) (got {author.failed_login_attempts})")
    check(author.locked_until is None, "expired lock is cleared, not left in the past")
    check(author.lockout_count == 1, "but the ladder rung is KEPT across expiry")

    print("\n2b. The second lockout climbs the ladder")
    for _ in range(4):
        post_author(AUTHOR_EMAIL, "wrong-password")
    author = fresh(appmod.Author, author_id)
    check(author.lockout_count == 2, f"second lockout is rung 2 (got {author.lockout_count})")
    minutes = author.lock_minutes_remaining()
    check(15 < minutes <= 30, f"and it is the 30-minute rung, not 15 (got {minutes})")

    print("\n3. Twelve quiet hours forget the strikes AND the rung")
    author.locked_until = None
    author.failed_login_attempts = 4
    author.lockout_count = 2
    author.last_failed_login_at = datetime.utcnow() - timedelta(hours=13)
    appmod.db.session.commit()
    clear_login_buckets()
    post_author(AUTHOR_EMAIL, "wrong-password")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 1,
          f"strikes decay after a long quiet spell (got {author.failed_login_attempts})")
    check(author.lockout_count == 0,
          f"the escalation ladder decays with them (got {author.lockout_count})")

    print("\n4. A burst against a locked account cannot climb the ladder")
    author.failed_login_attempts = 5
    author.lockout_count = 1
    author.locked_until = datetime.utcnow() + timedelta(minutes=15)
    author.last_failed_login_at = datetime.utcnow()
    appmod.db.session.commit()
    stragglers = [author.record_failed_login() for _ in range(7)]
    check(not any(stragglers), "seven stragglers all report not-locked-by-me")
    check(author.failed_login_attempts == 5,
          f"the strike counter does not move (got {author.failed_login_attempts})")
    check(author.lockout_count == 1,
          f"the ladder does not climb (got {author.lockout_count})")
    check(author.lock_minutes_remaining() <= 15,
          "the lock stays at its rung, not escalated to 60 by the burst")
    appmod.db.session.rollback()

    print("\n5. A completed sign-in clears all four fields")
    author = fresh(appmod.Author, author_id)
    author.locked_until = None
    appmod.db.session.commit()
    clear_login_buckets()
    resp = post_author(AUTHOR_EMAIL, AUTHOR_PASSWORD)
    check(resp.status_code == 302 and "/author/" in resp.headers.get("Location", ""),
          f"correct password signs in (got {resp.status_code})")
    author = fresh(appmod.Author, author_id)
    check(author.failed_login_attempts == 0 and author.locked_until is None
          and author.last_failed_login_at is None and author.lockout_count == 0,
          "success clears strikes, lock, last-failure stamp and ladder rung")
    client.get("/author/logout")

    print("\n6. The per-caller throttle refuses after its allowance")
    clear_login_buckets()
    refused_early = False
    for _ in range(appmod.LOGIN_PER_QUARTER_HOUR):
        if THROTTLED_FRAGMENT in body(post_author("nobody@example.test", "x")):
            refused_early = True
    check(not refused_early,
          f"{appmod.LOGIN_PER_QUARTER_HOUR} attempts pass the throttle")
    page = body(post_author("nobody@example.test", "x"))
    check(THROTTLED_FRAGMENT in page,
          f"attempt {appmod.LOGIN_PER_QUARTER_HOUR + 1} is refused by the throttle")
    check("127.0.0.1" in appmod._login_peer_rate,
          "the loose peer bucket is being counted as well")

    print("\n7. Staff: the counter survives the password step and clears only after 2FA")
    staff = fresh(appmod.AdminUser, staff_id)
    staff.failed_login_attempts = 3
    appmod.db.session.commit()
    secret = staff.totp_secret
    totp = pyotp.TOTP(secret)

    staff_client = appmod.app.test_client()
    resp = staff_client.post("/admin/login",
                             data={"email": STAFF_EMAIL, "password": STAFF_PASSWORD})
    check(resp.status_code == 302 and "verify-2fa" in resp.headers.get("Location", ""),
          "correct password redirects to the TOTP step")
    staff = fresh(appmod.AdminUser, staff_id)
    check(staff.failed_login_attempts == 3,
          f"password-correct-but-2FA-pending does NOT clear the counter (got {staff.failed_login_attempts})")

    # A code that cannot be valid in any window verify_totp will consider,
    # even if the 30-second step ticks between here and the POST.
    now = datetime.utcnow()
    valid = {totp.at(now, offset) for offset in range(-3, 5)}
    wrong_code = next(c for c in ("000000", "111111", "222222") if c not in valid)
    resp = staff_client.post("/admin/verify-2fa", data={"totp_code": wrong_code})
    check("Invalid code" in body(resp), "wrong TOTP code is rejected")
    staff = fresh(appmod.AdminUser, staff_id)
    check(staff.failed_login_attempts == 4,
          f"and it costs a strike on the SAME counter (got {staff.failed_login_attempts})")

    resp = staff_client.post("/admin/verify-2fa", data={"totp_code": totp.now()})
    check(resp.status_code == 302 and resp.headers.get("Location", "").endswith("/admin"),
          "correct TOTP completes the sign-in")
    staff = fresh(appmod.AdminUser, staff_id)
    check(staff.failed_login_attempts == 0 and staff.locked_until is None
          and staff.last_failed_login_at is None and staff.lockout_count == 0,
          "a COMPLETED 2FA login is what clears the counters")

    print("\n8. Publisher: same door, same lock")
    clear_login_buckets()
    pub_client = appmod.app.test_client()
    for _ in range(5):
        pub_client.post("/publisher/login",
                        data={"email": PUB_EMAIL, "password": "wrong-password"})
    pub = fresh(appmod.Publisher, pub_id)
    check(pub.failed_login_attempts == 5 and pub.locked_until is not None,
          f"five wrong passwords lock the publisher (got {pub.failed_login_attempts})")
    page = body(pub_client.post("/publisher/login",
                                data={"email": PUB_EMAIL, "password": PUB_PASSWORD}))
    check(LOCKED_FRAGMENT in page, "the correct password is refused while locked")
    check(pub_client.get("/publisher/dashboard").status_code == 302,
          "and it did not sign anyone in")

    print("\n9. Resetting the password clears the lock — the recovery path a "
          "locked-out user is sent to")
    # The lock is what sends a forgetful author to "Forgot password?" in the
    # first place, so a reset that left the lock standing would refuse the new
    # correct password on exactly that path until the timer expired. A valid
    # single-use token proves ownership, so the reset clears it.
    clear_login_buckets()
    reset_client = appmod.app.test_client()
    for _ in range(5):
        reset_client.post("/author/login",
                          data={"email": AUTHOR_EMAIL, "password": "wrong-password"})
    author = fresh(appmod.Author, author_id)
    check(author.is_locked() and author.failed_login_attempts == 5,
          "author is locked after five wrong passwords")
    # We are already inside the module-level app context, so mint the token on
    # the live session rather than a nested context whose commit would not be
    # what the reset request reads back.
    author.generate_reset_token()
    reset_token = author.password_reset_token
    appmod.db.session.commit()
    NEW_PASSWORD = "brand-new-passphrase-9"
    reset_client.post(f"/author/reset-password/{reset_token}",
                      data={"password": NEW_PASSWORD, "confirm_password": NEW_PASSWORD})
    author = fresh(appmod.Author, author_id)
    check(not author.is_locked() and author.failed_login_attempts == 0
          and author.locked_until is None and author.lockout_count == 0,
          "the reset cleared every lockout field")
    resp = reset_client.post("/author/login",
                            data={"email": AUTHOR_EMAIL, "password": NEW_PASSWORD})
    check(resp.status_code == 302 and "/login" not in resp.headers.get("Location", ""),
          "and the new correct password now signs the author straight in")

    print("\n10. The post-login redirect target cannot be pointed off-site")
    # _safe_next guards every "?next=" redirect on the sign-in pages. A real
    # relative path is honoured; anything that could send a just-signed-in
    # author to another host is dropped back to None (the caller then uses the
    # dashboard default).
    check(appmod._safe_next("/author/coaching") == "/author/coaching",
          "a genuine root-relative path is kept")
    for hostile in ("https://evil.com/x", "//evil.com", "/\\evil.com",
                    "\\/\\/evil.com", "http:evil.com", "evil.com/x"):
        check(appmod._safe_next(hostile) is None,
              f"an off-site target is refused: {hostile!r}")

if CHECK_DB.exists():
    CHECK_DB.unlink()

print()
print(f"{total} checks, {len(failures)} failed")
if failures:
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All sign-in lockout checks passed.")
