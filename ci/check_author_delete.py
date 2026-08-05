#!/usr/bin/env python3
"""Deleting an author must not be blocked by the foreign keys that protect them.

Six tables carry a foreign key to `author`, and three of those carry children of
their own. PostgreSQL refuses to delete a row while anything still points at it,
and this codebase has no ORM cascades at all -- `grep -c 'cascade='` over app.py
returns 0 -- so the delete order is hand-written in delete_author_and_dependents()
and nothing but a test keeps it honest.

WHY THIS FILE EXISTS. Two of those six foreign keys did not exist in production
until the change that added this check: `proposal.author_id` and
`marketing_module_data.author_id` were declared on the models and absent from the
database, because `_add()` emits ADD COLUMN and nothing else. Adding them makes
the database enforce something it never used to, so an admin delete that quietly
left rows behind becomes an admin delete that 500s. Verified on a real PostgreSQL
before the fix was written: an author with a social_strategy row already could not
be deleted -- that foreign key was live and the delete path never cleaned up after
it -- and marketing_module_data would have joined it.

So this is not a test of the migration. It is the test that says the migration did
not break a button.

The fixture puts a row in EVERY table that references the author, directly or
through a child, and then deletes them. A leftover row is a failure just as much
as an exception is: a delete that silently orphans rows is how you get a database
full of records belonging to nobody.

Runs against SQLite by default. Set AUTHOR_DELETE_TEST_DATABASE_URL to a
PostgreSQL DSN (CI does) to run it where the foreign keys are actually enforced
-- SQLite does not enforce them at all unless PRAGMA foreign_keys is on, so a
green SQLite run proves the ordering logic and nothing about the constraints.

Usage:
    python ci/check_author_delete.py
    AUTHOR_DELETE_TEST_DATABASE_URL=postgresql://... python ci/check_author_delete.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])

BASE_ENV = {
    "OPENAI_API_KEY": "ci-dummy-openai-credential",
    "SECRET_KEY": "ci-author-delete-key",
    "APP_BASE_URL": "http://localhost:5000",
    "MIGRATE_ON_BOOT": "0",
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
}

# Every table the fixture writes to. All of them must be empty afterwards.
TABLES = (
    "author", "proposal", "proposal_note", "coaching_enrollment",
    "marketing_module_data", "social_strategy", "one_pager_submission",
    "one_pager_feedback", "author_engagement_email", "coaching_chat_message",
    "homework_submission", "coaching_module_content", "author_module_progress",
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""), file=sys.stderr)
        failures.append(f"{label}{': ' + detail if detail else ''}")


def _scenario() -> int:
    """Build one author with every possible dependent, then delete them."""
    from datetime import datetime
    from sqlalchemy import text
    import app as application

    db = application.db
    tag = uuid.uuid4().hex[:8]

    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)

        author = application.Author(
            email=f"ci-delete-{tag}@example.invalid", name="CI Delete",
            password_hash="x", created_at=datetime.utcnow())
        db.session.add(author)
        db.session.commit()

        enrollment = application.CoachingEnrollment(author_id=author.id, status="active")
        db.session.add(enrollment)
        db.session.commit()

        db.session.add_all([
            application.CoachingChatMessage(enrollment_id=enrollment.id, module_order=1,
                                            role="user", content="hi"),
            application.HomeworkSubmission(enrollment_id=enrollment.id, module_order=1,
                                           content="hw"),
            application.CoachingModuleContent(enrollment_id=enrollment.id, module_order=1),
            application.AuthorModuleProgress(enrollment_id=enrollment.id, module_order=1,
                                             status="locked"),
            # Both parents at once: this row names the author AND the enrollment,
            # which is why the delete has to match on both columns.
            application.MarketingModuleData(enrollment_id=enrollment.id, author_id=author.id),
            application.AuthorEngagementEmail(author_id=author.id, email_type="welcome"),
        ])
        db.session.commit()

        one_pager = application.OnePagerSubmission(
            author_id=author.id, book_title="B", created_at=datetime.utcnow())
        db.session.add(one_pager)
        db.session.commit()

        db.session.add_all([
            application.OnePagerFeedback(submission_id=one_pager.id, feedback_type="text",
                                         feedback_text="x", created_at=datetime.utcnow()),
            # Also two parents: author and one_pager_submission.
            application.SocialStrategy(author_id=author.id, one_pager_id=one_pager.id,
                                       created_at=datetime.utcnow(),
                                       share_token=uuid.uuid4().hex),
        ])
        db.session.commit()

        proposal = application.Proposal(
            submission_id=f"ci-{tag}", author_id=author.id, author_name="CI Delete",
            author_email=f"ci-delete-{tag}@example.invalid", book_title="B",
            proposal_type="nonfiction", submitted_at=datetime.utcnow())
        db.session.add(proposal)
        db.session.commit()
        db.session.add(application.ProposalNote(proposal_id=proposal.id, content="n",
                                                created_at=datetime.utcnow()))
        db.session.commit()

        def counts():
            return {t: db.session.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                    for t in TABLES}

        before = counts()
        print(f"BEFORE={before}")
        # Every table must actually have the row, or the delete proves nothing.
        print(f"FIXTURE_COMPLETE={all(v >= 1 for v in before.values())}")

        try:
            application.delete_author_and_dependents(author)
            print("DELETE_RAISED=0")
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            print(f"DELETE_RAISED=1 {type(exc).__name__}: {str(exc)[:400]}")

        after = counts()
        print(f"AFTER={after}")
        print(f"LEFTOVERS={ {t: n for t, n in after.items() if n} }")
    return 0


def run(backend: str, url: str) -> None:
    print(f"\nAuthor delete ({backend}):")
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--scenario"],
        env={**BASE_ENV, "DATABASE_URL": url},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        print(out[-3000:], file=sys.stderr)

    check(f"[{backend}] the fixture built a row in all {len(TABLES)} tables",
          "FIXTURE_COMPLETE=True" in out,
          f"the delete would prove nothing against an empty table -- {out[-400:]!r}")
    check(f"[{backend}] deleting an author with every dependent does not raise",
          "DELETE_RAISED=0" in out,
          "a foreign key blocked the delete -- this is an admin button returning 500. "
          "Add the missing table to delete_author_and_dependents(), in child-first order")
    check(f"[{backend}] and it leaves nothing behind",
          "LEFTOVERS={}" in out,
          "rows survived the delete and now belong to nobody")


def main() -> int:
    print("=== AUTHOR DELETE CHECK ===")

    path = Path(tempfile.mkdtemp(prefix="wig-authdel-")) / "check.db"
    run("sqlite", f"sqlite:///{path}")

    pg = os.environ.get("AUTHOR_DELETE_TEST_DATABASE_URL", "").strip()
    if pg:
        from sqlalchemy import create_engine, text as _text
        schema = f"authdel_{uuid.uuid4().hex[:10]}"
        engine = create_engine(pg)
        with engine.begin() as conn:
            conn.execute(_text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()
        sep = "&" if "?" in pg else "?"
        try:
            run("postgresql", f"{pg}{sep}options=-csearch_path%3D{schema}")
        finally:
            engine = create_engine(pg)
            with engine.begin() as conn:
                conn.execute(_text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            engine.dispose()
    else:
        # Say it loudly. SQLite does not enforce foreign keys by default, so the
        # SQLite leg alone cannot fail for the reason this check exists.
        print("\n  NOTE  AUTHOR_DELETE_TEST_DATABASE_URL is not set, so this ran only "
              "against SQLite,\n        which does not enforce foreign keys. The ordering "
              "was checked; the\n        constraints were not.")

    if failures:
        print("\n=== AUTHOR DELETE CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n=== AUTHOR DELETE CHECK OK ===")
    return 0


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        sys.path.insert(0, str(REPO_ROOT))
        sys.exit(_scenario())
    sys.exit(main())
