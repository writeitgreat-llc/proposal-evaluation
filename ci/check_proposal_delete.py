#!/usr/bin/env python3
"""Deleting a proposal must work on a proposal somebody has actually touched.

Two tables carry a NOT NULL foreign key to `proposal` -- proposal_note and
publisher_proposal -- and this codebase has no ORM cascades at all (`grep -c
'cascade='` over app.py returns 0). SQLAlchemy's default for a relationship with
no cascade is to UPDATE the child's foreign key to NULL, which a NOT NULL column
rejects, so a bare `db.session.delete(proposal)` raises IntegrityError.

WHY THIS FILE EXISTS. Both delete paths were exactly that bare call, so the
admin "Delete" button returned a 500 for any proposal with a note, a status
change, a publisher share -- or an ARCHIVE TOGGLE, which writes a ProposalNote
of its own. That last one is the sharp edge: "archive the junk now, purge it
later" was the workflow that guaranteed the purge would fail, and only a
proposal nobody had ever touched could be deleted at all. Bulk delete had the
same fault plus one of its own: it looped delete() and committed once at the
end, so one proposal with a note aborted the whole batch, removed nothing, and
still flashed "N proposal(s) deleted."

This matters beyond the button. Deleting proposals is the only way to reclaim
space from uploaded files, which live in the database as bytea -- so a broken
delete is also a broken recovery path for the storage risk it belongs to.

The fixture builds a proposal in the state the button actually meets in
production: archived, noted, and shared with a publisher. A leftover child row
is a failure just as much as an exception is -- a delete that orphans
proposal_note rows leaves an audit trail pointing at a proposal that is gone.

Runs against SQLite by default. Set PROPOSAL_DELETE_TEST_DATABASE_URL to a
PostgreSQL DSN (CI does) to run it where the foreign keys are actually enforced
-- SQLite does not enforce them at all unless PRAGMA foreign_keys is on, so a
green SQLite run proves the ordering logic and nothing about the constraints.

Usage:
    python ci/check_proposal_delete.py
    PROPOSAL_DELETE_TEST_DATABASE_URL=postgresql://... python ci/check_proposal_delete.py
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
    "SECRET_KEY": "ci-proposal-delete-key",
    "APP_BASE_URL": "http://localhost:5000",
    "MIGRATE_ON_BOOT": "0",
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
}

# Every table the fixture writes to. All of them must be empty afterwards.
TABLES = ("proposal", "proposal_note", "publisher_proposal")

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""), file=sys.stderr)
        failures.append(f"{label}{': ' + detail if detail else ''}")


def _scenario() -> int:
    """Build two proposals with dependents, then delete both in one call."""
    from datetime import datetime
    from sqlalchemy import text
    import app as application

    db = application.db
    tag = uuid.uuid4().hex[:8]

    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)

        # TWO proposals, not one. Bulk delete is the path that used to abort the
        # entire batch on the first row with a child, so a single-row fixture
        # would pass against the broken version.
        proposals = []
        for n in (1, 2):
            p = application.Proposal(
                submission_id=f"ci-pdel-{tag}-{n}", author_name="CI Delete",
                author_email=f"ci-pdel-{tag}@example.invalid", book_title=f"B{n}",
                proposal_type="nonfiction", submitted_at=datetime.utcnow(),
                # The blob is the point of the storage half of this: the row
                # being deleted must be one that actually holds bytes.
                original_filename="p.pdf", original_file=b"%PDF-1.4 fixture",
            )
            db.session.add(p)
            db.session.commit()
            proposals.append(p)

        # The state the button meets in production. An archive toggle writes one
        # of these on its own, which is what made archived proposals undeletable.
        db.session.add_all([
            application.ProposalNote(proposal_id=proposals[0].id, content="n",
                                     created_at=datetime.utcnow()),
            application.ProposalNote(proposal_id=proposals[0].id, action="status_change",
                                     old_value="Active", new_value="Archived",
                                     created_at=datetime.utcnow()),
            application.ProposalNote(proposal_id=proposals[1].id, content="n2",
                                     created_at=datetime.utcnow()),
        ])
        db.session.commit()

        publisher = application.Publisher(
            name=f"CI Pub {tag}", email=f"ci-pub-{tag}@example.invalid",
            password_hash="x")
        db.session.add(publisher)
        db.session.commit()
        db.session.add(application.PublisherProposal(
            proposal_id=proposals[0].id, publisher_id=publisher.id))
        db.session.commit()

        def counts():
            return {t: db.session.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                    for t in TABLES}

        before = counts()
        print(f"BEFORE={before}")
        # Every table must actually have the row, or the delete proves nothing.
        print(f"FIXTURE_COMPLETE={all(v >= 1 for v in before.values())}")

        try:
            application.delete_proposals_and_dependents([p.id for p in proposals])
            db.session.commit()
            print("DELETE_RAISED=0")
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            print(f"DELETE_RAISED=1 {type(exc).__name__}: {str(exc)[:400]}")

        after = counts()
        print(f"AFTER={after}")
        print(f"LEFTOVERS={ {t: n for t, n in after.items() if n} }")

        # An empty list must be a no-op and not a table-wide delete. The helper
        # builds `IN (...)` filters, and an unguarded empty IN is the kind of
        # thing that reads fine and removes everything.
        db.session.add(application.Proposal(
            submission_id=f"ci-pdel-{tag}-guard", author_name="CI Guard",
            author_email=f"ci-guard-{tag}@example.invalid", book_title="Guard",
            proposal_type="nonfiction", submitted_at=datetime.utcnow()))
        db.session.commit()
        application.delete_proposals_and_dependents([])
        db.session.commit()
        survivors = db.session.execute(text("SELECT count(*) FROM proposal")).scalar()
        print(f"EMPTY_LIST_IS_NOOP={survivors == 1}")
    return 0


def run(backend: str, url: str) -> None:
    print(f"\nProposal delete ({backend}):")
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
    check(f"[{backend}] deleting archived, noted and shared proposals does not raise",
          "DELETE_RAISED=0" in out,
          "a foreign key blocked the delete -- this is the admin Delete button "
          "returning 500, and the only way to reclaim upload storage. Add the "
          "missing table to delete_proposals_and_dependents(), in child-first order")
    check(f"[{backend}] and it leaves nothing behind",
          "LEFTOVERS={}" in out,
          "rows survived the delete and now point at a proposal that is gone")
    check(f"[{backend}] an empty selection deletes nothing",
          "EMPTY_LIST_IS_NOOP=True" in out,
          "delete_proposals_and_dependents([]) removed rows it was not given -- "
          "an unguarded empty IN () clause")


def main() -> int:
    print("=== PROPOSAL DELETE CHECK ===")

    path = Path(tempfile.mkdtemp(prefix="wig-propdel-")) / "check.db"
    run("sqlite", f"sqlite:///{path}")

    pg = os.environ.get("PROPOSAL_DELETE_TEST_DATABASE_URL", "").strip()
    if pg:
        from sqlalchemy import create_engine, text as _text
        schema = f"propdel_{uuid.uuid4().hex[:10]}"
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
        print("\n  NOTE  PROPOSAL_DELETE_TEST_DATABASE_URL is not set, so this ran only "
              "against SQLite,\n        which does not enforce foreign keys. The ordering "
              "was checked; the\n        constraints were not.")

    if failures:
        print("\n=== PROPOSAL DELETE CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n=== PROPOSAL DELETE CHECK OK ===")
    return 0


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        sys.path.insert(0, str(REPO_ROOT))
        sys.exit(_scenario())
    sys.exit(main())
