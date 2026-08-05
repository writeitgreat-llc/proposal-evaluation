#!/usr/bin/env python3
"""check_one_pager_reminders.py -- the reminder job must not bombard a reviewer.

check_one_pager_reminders() never ran between 16 April and 5 August 2026: it
had no application context, so its first ORM query raised and its bare `except`
printed and swallowed. Fixing the context is what makes this job send its first
email ever -- against a database in which EVERY assigned one-pager is months
past both reminder points and has no reminder recorded.

Run through the original branches, that backlog costs the assignee TWO emails
per submission within two hours:

    if hours >= 48 and not r1:   send; stamp r1     # pass 1
    elif hours >= 96 and not r2: send; stamp r2     # pass 2, one hour later

which is a burst of months-old chases for work nobody was ever told about.

WHY THIS FILE EXISTS AT ALL. Every other check in this directory builds an
EMPTY database, and against an empty database this job is a no-op that passes
whatever it does. The bug is a property of pre-existing rows, so the only way
to see it is to seed rows shaped like the real ones -- which is exactly the
lesson from the author-signup safeguard that silently re-ran on every release
and was caught by a test that imitated the real database rather than a fresh
one.

What it pins:
  1. a months-old backlog produces ONE digest email per assignee, not 2N;
  2. running the job again sends NOTHING -- the digest is not re-sent hourly;
  3. a live submission still gets the normal sequence, one reminder at a time,
     and never both in consecutive passes;
  4. a FAILED send does not stamp the rows. send_email() returns False and
     raises nothing, so the original `send(...); stamp; commit` recorded
     reminders that never left the building. For the digest that is
     unrecoverable -- the rows would read as reminded and the backlog would
     never be mentioned to anybody again.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

CHECK_DB = REPO_ROOT / "ci-reminders.db"
if CHECK_DB.exists():
    CHECK_DB.unlink()

os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ["DATABASE_URL"] = "sqlite:///" + str(CHECK_DB)
os.environ.setdefault("SECRET_KEY", "ci-reminder-test-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["MIGRATE_ON_BOOT"] = "0"

import app as appmod  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


# ---------------------------------------------------------------- fixtures --

sent: list[tuple[str, str]] = []
send_succeeds = True


def fake_send_email(to_email, subject, html_content, attachments=None):
    """Stand in for SMTP, honouring the real contract: False on failure, never raises."""
    if not send_succeeds:
        return False
    sent.append((to_email, subject))
    return True


appmod.send_email = fake_send_email

# The job resolves recipients through this map; pin it so the check does not
# depend on TEAM_ROSTER in the environment.
appmod.TEAM_MEMBER_EMAILS = {"Andy": "andy@example.test"}

ASSIGNEE = "Andy"


def seed(assigned_hours_ago: float, r1=None, r2=None, name="Test Author"):
    """Create one assigned, unreviewed one-pager that is `assigned_hours_ago` old."""
    author = appmod.Author(
        name=name,
        email=f"{name.replace(' ', '.').lower()}.{assigned_hours_ago}@example.test",
        password_hash="x",
    )
    appmod.db.session.add(author)
    appmod.db.session.flush()
    sub = appmod.OnePagerSubmission(
        author_id=author.id,
        status="submitted",
        assigned_to=ASSIGNEE,
        assigned_at=datetime.utcnow() - timedelta(hours=assigned_hours_ago),
        reminder_1_sent_at=r1,
        reminder_2_sent_at=r2,
    )
    appmod.db.session.add(sub)
    appmod.db.session.commit()
    return sub


with appmod.app.app_context():
    appmod.db.create_all()

    print("\n1. A months-old backlog produces one digest, not two emails per row")
    backlog = [seed(24 * 110, name="Jane Okafor"),
               seed(24 * 100, name="Marcus Reed"),
               seed(24 * 95, name="Priya Nair")]
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 1,
          f"3 stale submissions -> 1 email (got {len(sent)}: {[s for _, s in sent]})")
    check(sent and "3 one-pagers are waiting" in sent[0][1],
          f"digest subject names the count (got {sent[0][1] if sent else 'nothing'})")
    check(all(s.reminder_1_sent_at and s.reminder_2_sent_at for s in backlog),
          "backlog rows are stamped so they cannot be chased again")

    print("\n2. Running again sends nothing (the digest is not hourly)")
    sent.clear()
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 0, f"second pass sends nothing (got {len(sent)})")

    print("\n3. A live submission still gets the normal sequence, one step per pass")
    sent.clear()
    live = seed(50, name="Fresh Author")            # past 48h, not past 96h
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 1, f"reminder 1 at 50h (got {len(sent)})")
    check(live.reminder_1_sent_at is not None and live.reminder_2_sent_at is None,
          "only reminder 1 is stamped at 50h")

    sent.clear()
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 0, "no second email while still under 96h")

    # Age it past the second point, exactly as the clock would.
    live.assigned_at = datetime.utcnow() - timedelta(hours=100)
    appmod.db.session.commit()
    sent.clear()
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 1, f"reminder 2 once past 96h (got {len(sent)})")
    check(live.reminder_2_sent_at is not None, "reminder 2 is stamped")

    sent.clear()
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 0, "nothing further after both reminders")

    print("\n4. A failed send must not stamp the rows")
    sent.clear()
    send_succeeds = False
    lost = seed(24 * 120, name="Unlucky Author")
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(lost.reminder_1_sent_at is None and lost.reminder_2_sent_at is None,
          "digest that failed to send leaves the rows unstamped")

    send_succeeds = True
    sent.clear()
    appmod.check_one_pager_reminders()
    appmod.db.session.expire_all()   # job commits in its own context; re-read from the DB
    check(len(sent) == 1, f"and it is retried on the next pass (got {len(sent)})")
    check(lost.reminder_1_sent_at is not None, "stamped once the send succeeds")

if CHECK_DB.exists():
    CHECK_DB.unlink()

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All one-pager reminder checks passed.")
