#!/usr/bin/env python3
"""Prove the four properties run_migrations() is required to have.

Each one is a production incident this repo has already had, and each one is the
kind that a passing test suite would not otherwise notice:

  1. A FAILED OPERATION ABORTS THE RELEASE. Every operation used to be wrapped
     in a try/except that printed and carried on, so a column that never applied
     shipped anyway. docs/DEPLOYMENT.md described a release-abort that could not
     happen. This check poisons the schema and asserts that `migrate.py` exits
     NON-ZERO -- the only thing Heroku actually reacts to.
  2. APPLIED WORK IS NOT REDONE. The second run of a migrated database must
     issue ZERO DDL. Counted at the driver, not inferred from log output.
  3. THE LEDGER IS VERIFIED, NOT TRUSTED. A ledger row claiming a column that
     is not in the database must be detected and the operation re-applied.
  4. A COLUMN WITH NO MIGRATION AT ALL IS CAUGHT. Properties 1-3 are statements
     about the operations somebody remembered to write. The mistake none of them
     sees is a db.Column added to a model with no matching `_add()`: on an empty
     database create_all() builds it from the model and everything looks perfect,
     while on the live database the table already exists and the column is simply
     never created. That is the incident fix_schema.py was written for, and until
     schema_drift() existed nothing in this repo could tell you about it.

Property 4 is checked TWICE, on purpose, because the two answer different
questions:

  * `drop-column` proves the mechanism -- take a column that has no `_add()`,
     remove it from the database, and assert `migrate.py` exits NON-ZERO.
  * THE UPGRADE REPLAY proves the actual pull request. It builds a database from
     the models as they exist on the BASE commit -- which is what production has
     -- then runs THIS branch's `migrate.py` against it, exactly as Heroku will.
     A column added to a model without its `_add()` fails here, in the PR, rather
     than by aborting a release after the merge. It is also the only place the
     new ALTER TABLE statements are ever really executed: against a fresh
     database every `_add()` finds its column already made by create_all() and
     adopts it without issuing any DDL at all, so a malformed column definition
     sails through every other check here.

Plus the wiring those properties rest on: importing app.py must not touch the
schema on a dyno, `_add()` keys must be unique, and the Procfile must still have
the release phase that runs any of this.

Runs against SQLite by default. Set MIGRATION_TEST_DATABASE_URL to a PostgreSQL
DSN (CI does) to run the same battery against Postgres -- which matters more
than it looks: the Postgres branch is the one production uses, and it contains
all the code SQLite never executes (information_schema snapshot, SET LOCAL
lock_timeout, SQLSTATE 55P03 handling, pg_index verification).

The replay needs the base commit present in .git, so CI checks out with
`fetch-depth: 0` and passes the base sha in SCHEMA_REPLAY_BASE. Without either
it falls back to HEAD~1, and if that is not resolvable it says so loudly and
skips -- a skipped replay is a loss of EARLINESS, not of protection, because
run_migrations() makes the same check against the real database at release time.

Usage:
    python ci/check_migrations.py
    MIGRATION_TEST_DATABASE_URL=postgresql://... python ci/check_migrations.py
    SCHEMA_REPLAY_BASE=<sha> python ci/check_migrations.py
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
APP_PY = REPO_ROOT / "app.py"

BASE_ENV = {
    "OPENAI_API_KEY": "ci-dummy-openai-credential",
    "SECRET_KEY": "ci-migration-check-key",
    # Keep _is_production False so nothing switches to secure-cookie mode.
    "APP_BASE_URL": "http://localhost:5000",
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
}

failures: list[str] = []


def _warn(message: str) -> None:
    """A GitHub annotation, so a SKIPPED check is visible without reading a log.

    Every path that turns the upgrade replay off still exits 0, deliberately —
    a broken base commit or a shallow clone is not this pull request's fault and
    must not block it. But a check that silently stops checking is the exact
    shape of the bug this file exists to catch, so the skip has to surface
    somewhere a human actually looks.
    """
    print(f"::warning title=Schema drift check::{message}")


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""), file=sys.stderr)
        failures.append(f"{label}{': ' + detail if detail else ''}")


# ===========================================================================
# Static checks -- no database needed
# ===========================================================================

def _add_column_name(node: ast.expr) -> str:
    """The column name from an _add() second argument.

    Two of the calls are f-strings -- app.py interpolates the BYTEA/BLOB type
    for `original_file` and ROLE_MEMBER into the `role` default. The column name
    is always the first token of the leading literal segment, so derive it from
    that rather than skipping those two: they sit between the only pair of
    same-named columns in different tables (proposal.author_id and
    marketing_module_data.author_id), which is exactly what the uniqueness
    assertion below exists to catch.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split()[0]
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str) and head.value.split():
            return head.value.split()[0]
    raise SystemExit(
        f"ci/check_migrations.py: _add() at app.py:{node.lineno} has no literal "
        f"column-name prefix, so no stable ledger key can be derived from it. "
        f"Give the column a literal name."
    )


def static_checks() -> None:
    print("\nStatic checks (app.py, Procfile):")
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))

    keys: dict[str, int] = {}
    dupes: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_add" and len(node.args) == 2):
            table = node.args[0]
            if not (isinstance(table, ast.Constant) and isinstance(table.value, str)):
                raise SystemExit(f"_add() at app.py:{node.lineno}: table name is not a literal")
            key = f"col:{table.value}.{_add_column_name(node.args[1])}"
            if key in keys:
                dupes.append(f"{key} (app.py:{keys[key]} and app.py:{node.lineno})")
            keys[key] = node.lineno

    check(f"{len(keys)} _add() ledger keys, all unique", not dupes, "; ".join(dupes))
    # A floor, not an equality: adding a column should not fail this check. It
    # exists to catch a suite that silently stops seeing the calls at all.
    check("_add() call sites still discoverable (>= 60)", len(keys) >= 60, f"found {len(keys)}")

    # The forgotten-migration scenarios remove UNMIGRATED_COLUMN and assert the
    # release aborts. The day somebody gives that column an `_add()`, the
    # migration runner puts it straight back and those scenarios quietly start
    # proving nothing at all -- while still passing. Pin it here instead.
    unmigrated_key = f"col:{UNMIGRATED_COLUMN[0]}.{UNMIGRATED_COLUMN[1]}"
    check(f"{unmigrated_key} still has no _add(), so the drift scenarios still test something",
          unmigrated_key not in keys,
          f"{UNMIGRATED_COLUMN[0]}.{UNMIGRATED_COLUMN[1]} now has a migration, so dropping "
          f"it no longer simulates a forgotten one -- point UNMIGRATED_COLUMN at another "
          f"model column that has no _add()")

    src = APP_PY.read_text(encoding="utf-8")

    # SET LOCAL, never a bare SET: a session-scoped SET survives the connection
    # pool's rollback-on-return and silently reconfigures every later checkout.
    bare_set = re.findall(r"""["']\s*SET\s+(?!LOCAL)\w*\s*lock_timeout""", src, re.I)
    check("lock_timeout is set with SET LOCAL, never a bare SET", not bare_set, str(bare_set))

    # There is deliberately NO static check that SET LOCAL sits inside an
    # `is_pg` branch. SET LOCAL is Postgres-only syntax and SQLite raises
    # `near "SET": syntax error` on it, so an unguarded one would fail every
    # operation in the SQLite battery below. A behavioural test that cannot
    # pass while the bug exists beats a regex over source text.

    procfile = (REPO_ROOT / "Procfile").read_text(encoding="utf-8")
    check("Procfile still has a release phase",
          re.search(r"^release:", procfile, re.M) is not None,
          "without `release:` nothing applies the schema, because boot no longer does")
    check("the release phase runs migrate.py",
          re.search(r"^release:.*migrate\.py", procfile, re.M) is not None)

    # migrate.py must disable boot migration BEFORE importing app, or the
    # non-strict import-time pass runs first, swallows the failure, and the
    # strict pass finds nothing left to do and reports success.
    mig = (REPO_ROOT / "migrate.py").read_text(encoding="utf-8")
    set_at = mig.find("MIGRATE_ON_BOOT")
    import_at = mig.find("from app import")
    check("migrate.py sets MIGRATE_ON_BOOT before importing app",
          0 <= set_at < import_at,
          "otherwise the import-time pass runs first and neuters strict mode")
    check("migrate.py asks for strict=True", "strict=True" in mig)


# ===========================================================================
# Child scenarios -- each runs in its own interpreter
# ===========================================================================

def run_child(scenario: str, env: dict, expect_rc: int | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--scenario", scenario],
        env={**BASE_ENV, **env},
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    if expect_rc is not None and proc.returncode != expect_rc:
        print(out[-3000:], file=sys.stderr)
    return proc.returncode, out


def sqlite_url() -> tuple[str, Path]:
    path = Path(tempfile.mkdtemp(prefix="wig-migcheck-")) / "check.db"
    return f"sqlite:///{path}", path


_pg_schemas: list[tuple[str, str]] = []


def fresh_pg(base: str) -> str:
    """A private schema on the test database, for one scenario.

    Never touches `public`, so pointing MIGRATION_TEST_DATABASE_URL at a
    database that matters cannot destroy anything. It also exercises the
    `WHERE table_schema = current_schema()` filter in run_migrations(): with
    everything in `public` a missing filter would look identical to a correct
    one, and the failure mode it guards against (matching a same-named column in
    another schema, and so reporting an unapplied migration as applied) is
    exactly the kind this check exists to notice.
    """
    from sqlalchemy import create_engine, text

    schema = f"migcheck_{uuid.uuid4().hex[:10]}"
    engine = create_engine(base)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine.dispose()
    _pg_schemas.append((base, schema))
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-csearch_path%3D{schema}"


def drop_pg_schemas() -> None:
    from sqlalchemy import create_engine, text

    for base, schema in _pg_schemas:
        try:
            engine = create_engine(base)
            with engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            print(f"  note  could not drop test schema {schema}: {exc}")


# --- scenario bodies (executed in the child) -------------------------------

def _scenario_boot_gate() -> int:
    """Importing app.py must not touch the schema when the release phase owns it."""
    import app as application  # noqa: F401
    from sqlalchemy import inspect
    with application.app.app_context():
        tables = inspect(application.db.engine).get_table_names()
    print(f"TABLES={len(tables)}")
    return 0


def _scenario_import_app() -> int:
    """Import app.py and say so. Deliberately never touches the database.

    Two of its callers run with NO DATABASE_URL, where the fallback is a
    relative sqlite:/// path that Flask-SQLAlchemy 3.x resolves against
    app.instance_path -- so connecting would create (or read) instance/
    proposals.db inside whatever checkout this is running in, including a
    developer's. Whether the import happened at all is the whole assertion.
    """
    import app  # noqa: F401
    print("IMPORTED_OK")
    return 0


def _scenario_battery() -> int:
    """Idempotence, ledger verification, and the strict/non-strict split."""
    import collections
    import re as _re
    from sqlalchemy import event, text
    from sqlalchemy.engine import Engine

    seen = collections.Counter()

    @event.listens_for(Engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):
        s = " ".join(statement.split())
        if _re.match(r"^(ALTER TABLE|CREATE (UNIQUE )?INDEX)\b", s, _re.I):
            seen[s[:80]] += 1

    import app as application
    db = application.db

    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)          # pass 1
        seen.clear()
        application.run_migrations(strict=True)          # pass 2 -- must be silent
        print(f"DDL_ON_SECOND_RUN={sum(seen.values())} {list(seen)}")

        # --- ledger verification: claim a column that is not there -----------
        with db.engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO {application.MIGRATION_LEDGER} (migration_key) "
                f"VALUES ('col:proposal.__ci_ghost_column__')"
            ))
        seen.clear()
        application.run_migrations(strict=True)
        with db.engine.connect() as conn:
            still = conn.execute(text(
                f"SELECT count(*) FROM {application.MIGRATION_LEDGER} "
                f"WHERE migration_key = 'col:proposal.__ci_ghost_column__'"
            )).scalar()
        print(f"GHOST_LEDGER_ROW_SURVIVED={still}")
    return 0


def _poison(db, application) -> None:
    """Make one real migration impossible, without removing the table's name.

    Shadowing `publisher` with a view is the cleanest cross-backend poison:
    create_all() sees the name and leaves it alone, the inspector does not list
    it as a table, so the four publisher columns are judged missing and every
    ALTER against it fails for real.
    """
    from sqlalchemy import text
    cascade = " CASCADE" if "postgresql" in str(db.engine.url) else ""

    # Table first, then view: both backends refuse DROP VIEW on a table, and
    # whichever `publisher` currently is, only one of these is the right one.
    for stmt in (f"DROP TABLE IF EXISTS publisher{cascade}",
                 f"DROP VIEW IF EXISTS publisher{cascade}"):
        try:
            with db.engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception:
            pass

    with db.engine.begin() as conn:
        conn.execute(text("CREATE VIEW publisher AS SELECT 1 AS id"))
    with db.engine.begin() as conn:
        conn.execute(text(
            f"DELETE FROM {application.MIGRATION_LEDGER} "
            f"WHERE migration_key LIKE 'col:publisher.%'"
        ))


def _scenario_strict_split() -> int:
    """strict=False reports; strict=True raises. The whole point of the change."""
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        _poison(db, application)

        try:
            application.run_migrations(strict=False)
            print("NONSTRICT_RAISED=0")
        except application.MigrationError:
            print("NONSTRICT_RAISED=1")

        try:
            application.run_migrations(strict=True)
            print("STRICT_RAISED=0")
        except application.MigrationError as e:
            print(f"STRICT_RAISED=1 NAMED={'col:publisher.bio' in str(e)}")
    return 0


def _scenario_poison_only() -> int:
    """Bring the schema up, then break it, so migrate.py meets a real failure."""
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        _poison(db, application)
    return 0


# The column the forgotten-migration scenarios remove. It must be one with NO
# `_add()`, or run_migrations() would simply put it back and prove nothing --
# which is the whole difference between "an operation failed" and "there is no
# operation". static_checks() asserts it stays that way.
UNMIGRATED_COLUMN = ("proposal", "book_title")


def _scenario_forget_add() -> int:
    """Simulate the one mistake the operation list cannot see.

    Bring the schema up, then drop a column that no `_add()` covers. The
    database is now exactly what production looks like when somebody adds a
    db.Column and writes no migration for it: every table present, every
    migration applied, one column short of what the code expects.
    """
    from sqlalchemy import text
    import app as application
    db = application.db
    table, column = UNMIGRATED_COLUMN
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        with db.engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        print(f"DROPPED={table}.{column}")
        print(f"DRIFT={application.schema_drift()}")
    return 0


def _scenario_drift_tolerates_extras() -> int:
    """A column the database has and no model declares must NOT fail a release.

    Production carries four of these today (author_module_progress.approved_at,
    .reminder_sent_at, .started_at, homework_submission.reviewed_at). If the
    check were symmetric, deleting a model column -- ordinary cleanup -- would
    block every deploy until somebody hand-dropped it from the live table, and
    the check would be switched off within a week.
    """
    from sqlalchemy import text
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE proposal ADD COLUMN ci_legacy_leftover TEXT"))
        application.run_migrations(strict=True)      # must not raise
        print(f"EXTRAS_TOLERATED=1 DRIFT={application.schema_drift()}")
    return 0


def _sample_value(column):
    """Something legal to put in a NOT NULL column, chosen by type."""
    import datetime
    import uuid as _uuid

    kind = column.type.__class__.__name__.upper()
    if column.foreign_keys:
        # sorted_tables puts parents first and ids start at 1, so 1 resolves.
        return 1
    if "BOOL" in kind:
        return False
    if "INT" in kind:
        return 1
    if any(k in kind for k in ("FLOAT", "NUMERIC", "DECIMAL", "REAL")):
        return 0
    if "DATETIME" in kind or "TIMESTAMP" in kind:
        return datetime.datetime(2020, 1, 1)
    if kind == "DATE":
        return datetime.date(2020, 1, 1)
    if "BINARY" in kind or "BLOB" in kind or "BYTEA" in kind:
        return b"x"
    if "JSON" in kind:
        return {}
    value = f"ci-{column.table.name[:12]}-{_uuid.uuid4().hex[:8]}"
    length = getattr(column.type, "length", None)
    return value[:length] if length else value


def _seed_one_row_per_table(application) -> int:
    """Put a row in every table before the replay upgrades it.

    THE POINT: `ALTER TABLE t ADD COLUMN c TEXT NOT NULL` SUCCEEDS on an empty
    table and fails on a populated one with `contains null values`. Production's
    author and proposal tables are not empty. An empty replay database would
    hand back a confident all-clear on the single most likely way a new column
    breaks a live table -- the one it is here to catch.

    Best-effort by design. A table that will not seed is skipped and counted,
    never fatal: a partly-seeded replay is weaker than a fully-seeded one and
    still far stronger than an empty one, and a seeding quirk in some corner
    table must not be able to fail an unrelated pull request.
    """
    db = application.db
    seeded, skipped = 0, []
    for table in db.metadata.sorted_tables:
        values = {}
        for column in table.columns:
            # Let the database assign a serial primary key -- but ONLY an
            # integer one. SQLAlchemy leaves autoincrement at "auto" on every
            # column, so testing that alone silently skips a string primary key
            # (consumed_sso_token.jti) and the whole row fails on NOT NULL.
            if (column.primary_key and column.autoincrement in (True, "auto")
                    and "INT" in column.type.__class__.__name__.upper()):
                continue
            if column.nullable or column.default is not None or column.server_default is not None:
                continue
            values[column.name] = _sample_value(column)
        try:
            with db.engine.begin() as conn:
                conn.execute(table.insert().values(**values))
            seeded += 1
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{table.name}({type(exc).__name__})")
    print(f"SEEDED={seeded} SKIPPED={len(skipped)} {';'.join(skipped[:8])}")
    return seeded


def _scenario_missing_index_is_not_fatal() -> int:
    """A declared index that the database lacks must NOT stop a release.

    Production has four of these right now, every one a column added by `_add()`
    — which emits ADD COLUMN and nothing else. If this were fatal it would block
    every deploy from the day it shipped, and the honest response to that is not
    to soften it later but to never make it fatal in the first place. Columns
    and constraints get different treatment on purpose, and this asserts the
    difference rather than trusting a comment about it.
    """
    from sqlalchemy import text
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_proposal_results_token"))
        gaps = application.schema_constraint_gaps()
        print(f"GAPS={gaps}")
        application.run_migrations(strict=True)      # must not raise
        print("GAP_DID_NOT_BLOCK=1")
    return 0


def _scenario_constraints_from_production() -> int:
    """Put the schema in the state PRODUCTION was in, then migrate it forward.

    create_all() builds indexes and foreign keys at CREATE TABLE time, so a
    fresh database has all four of these and can never exercise the operations
    that add them. Production could not: `_add()` emits ADD COLUMN and nothing
    else, so every index/unique/FK declared on a later-added column was missing.
    Removing them here — and their ledger rows, or the run would skip — is the
    only way to make this test the thing it claims to test.
    """
    from sqlalchemy import text
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        is_pg = "postgresql" in str(db.engine.url)
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_social_strategy_share_token"))
            conn.execute(text("DROP INDEX ix_proposal_content_hash"))
            if is_pg:
                # SQLite cannot DROP CONSTRAINT, and cannot ADD one either --
                # which is exactly why _foreign_key() is a no-op there.
                conn.execute(text("ALTER TABLE proposal "
                                  "DROP CONSTRAINT proposal_author_id_fkey"))
                conn.execute(text("ALTER TABLE marketing_module_data "
                                  "DROP CONSTRAINT marketing_module_data_author_id_fkey"))
            conn.execute(text("DELETE FROM schema_migrations "
                              "WHERE migration_key LIKE 'idx:%' OR migration_key LIKE 'fk:%'"))
        print(f"GAPS_BEFORE={application.schema_constraint_gaps()}")
        application.run_migrations(strict=True)
        print(f"GAPS_AFTER={application.schema_constraint_gaps()}")
    return 0


def _scenario_wrong_shape_is_fatal() -> int:
    """An index of the right NAME and the wrong shape must not be adopted.

    `CREATE UNIQUE INDEX IF NOT EXISTS` matches the name only, so a non-unique
    index called ix_social_strategy_share_token would make the statement succeed
    while the uniqueness it exists to provide does not. Recording that as done
    is worse than never having run: the ledger would then say the constraint is
    there forever.
    """
    from sqlalchemy import text
    import app as application
    db = application.db
    with application.app.app_context():
        db.create_all()
        application.run_migrations(strict=True)
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX ix_social_strategy_share_token"))
            conn.execute(text("CREATE INDEX ix_social_strategy_share_token "
                              "ON social_strategy (share_token)"))   # NOT unique
            conn.execute(text("DELETE FROM schema_migrations "
                              "WHERE migration_key = 'idx:ix_social_strategy_share_token'"))
        try:
            application.run_migrations(strict=True)
            print("WRONG_SHAPE_RAISED=0")
        except application.MigrationError as exc:
            print(f"WRONG_SHAPE_RAISED=1 NAMED={'ix_social_strategy_share_token' in str(exc)}")
    return 0


def _scenario_build_base_schema() -> int:
    """Build the schema from the tree this runs in, and put a row in every table.

    Runs inside the exported base tree, so `application` here is the BASE
    commit's app.py. Deliberately tolerant of an older signature: the point is
    to reproduce the schema the base commit ships, not to assert anything
    about it.
    """
    import app as application
    with application.app.app_context():
        application.db.create_all()
        try:
            application.run_migrations(strict=True)
        except TypeError:
            application.run_migrations()
        _seed_one_row_per_table(application)
    print("BASE_SCHEMA_BUILT")
    return 0


def _scenario_independent_drift() -> int:
    """Diff the models against the live catalog WITHOUT calling schema_drift().

    Re-implemented here on purpose. If this CI gate called the same helper the
    release phase uses, then deleting that helper would delete the gate too, and
    the build would go green on the one change that removes the guarantee. It
    also reads the catalog a different way -- SQLAlchemy's inspector goes to
    pg_catalog, while schema_drift() reads information_schema -- so the two
    disagree if the app's role can only see part of its own schema.
    """
    from sqlalchemy import inspect as _insp
    import app as application

    with application.app.app_context():
        insp = _insp(application.db.engine)
        live = {(t, c["name"]) for t in insp.get_table_names() for c in insp.get_columns(t)}
        missing = sorted(
            f"{t.name}.{c.name}"
            for t in application.db.metadata.sorted_tables
            for c in t.columns
            if (t.name, c.name) not in live
        )
    print(f"INDEPENDENT_DRIFT={missing}")
    return 0


SCENARIOS = {
    "boot-gate": _scenario_boot_gate,
    "import-app": _scenario_import_app,
    "battery": _scenario_battery,
    "strict-split": _scenario_strict_split,
    "poison-only": _scenario_poison_only,
    "forget-add": _scenario_forget_add,
    "drift-extras": _scenario_drift_tolerates_extras,
    "build-base-schema": _scenario_build_base_schema,
    "independent-drift": _scenario_independent_drift,
    "missing-index": _scenario_missing_index_is_not_fatal,
    "constraints-from-production": _scenario_constraints_from_production,
    "wrong-shape": _scenario_wrong_shape_is_fatal,
}


# ===========================================================================
# Database-backed checks -- run once per available backend
# ===========================================================================

def db_checks(backend: str, base_url: str) -> None:
    print(f"\nDatabase checks ({backend}):")

    def fresh() -> str:
        """A clean database for one scenario -- these must not share state."""
        return sqlite_url()[0] if backend == "sqlite" else fresh_pg(base_url)

    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}

    rc, out = run_child("battery", env, expect_rc=0)
    check(f"[{backend}] migration battery ran", rc == 0, out[-600:] if rc else "")

    ddl = re.search(r"DDL_ON_SECOND_RUN=(\d+)", out)
    check(f"[{backend}] second run of a migrated database issues ZERO DDL",
          ddl is not None and ddl.group(1) == "0",
          f"issued {ddl.group(1) if ddl else '?'} statement(s) -- "
          f"every one takes a table lock in production")

    ghost = re.search(r"GHOST_LEDGER_ROW_SURVIVED=(\d+)", out)
    check(f"[{backend}] a ledger row claiming a missing column is deleted",
          ghost is not None and ghost.group(1) == "0",
          "the ledger must be verified against the database, never trusted")

    # --- the property the whole change exists for --------------------------
    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("strict-split", env, expect_rc=0)
    check(f"[{backend}] strict split scenario ran", rc == 0, out[-600:] if rc else "")
    check(f"[{backend}] strict=False reports failures without raising",
          "NONSTRICT_RAISED=0" in out,
          "a web dyno must still boot when it cannot migrate")
    check(f"[{backend}] strict=True RAISES on a failed operation",
          "STRICT_RAISED=1" in out,
          "FAILURES ARE BEING SWALLOWED -- this is the bug this check exists for")
    check(f"[{backend}] the error names the operation that failed",
          "NAMED=True" in out,
          "one broken operation must not hide behind the ones that worked")

    # --- and the contract Heroku actually reacts to ------------------------
    penv = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, _ = run_child("poison-only", penv, expect_rc=0)
    check(f"[{backend}] poisoned a database for the release test", rc == 0)

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, **penv}, capture_output=True, text=True,
        cwd=str(REPO_ROOT), timeout=300,
    )
    check(f"[{backend}] migrate.py EXITS NON-ZERO when a migration fails",
          proc.returncode != 0,
          f"exited {proc.returncode} -- Heroku would promote a broken schema. "
          f"This is the exact claim docs/DEPLOYMENT.md used to make falsely.")
    check(f"[{backend}] the aborted release says why",
          "RELEASE ABORTED" in (proc.stdout + proc.stderr))

    # MIGRATIONS_STRICT=0 is the documented bypass; it must ship AND re-enable
    # boot migration, or it strands the schema with nothing able to repair it.
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, **penv, "MIGRATIONS_STRICT": "0"},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    check(f"[{backend}] MIGRATIONS_STRICT=0 lets the release through",
          proc.returncode == 0, f"exited {proc.returncode}")


def drift_checks(backend: str, base_url: str) -> None:
    """Property 4: a model column with no migration at all must stop the release."""
    print(f"\nForgotten-migration checks ({backend}):")

    def fresh() -> str:
        return sqlite_url()[0] if backend == "sqlite" else fresh_pg(base_url)

    table, column = UNMIGRATED_COLUMN

    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("forget-add", env, expect_rc=0)
    check(f"[{backend}] built a database missing an unmigrated column", rc == 0, out[-600:] if rc else "")
    check(f"[{backend}] schema_drift() names {table}.{column}",
          f"'{table}.{column}'" in out,
          f"the drift check did not notice a column the models declare -- "
          f"output was {out[-300:]!r}")

    # The contract Heroku reacts to. run_migrations() has no operation for this
    # column, so nothing here can repair it -- the only correct outcome is to
    # refuse to promote the slug.
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, **env}, capture_output=True, text=True,
        cwd=str(REPO_ROOT), timeout=300,
    )
    combined = proc.stdout + proc.stderr
    check(f"[{backend}] migrate.py EXITS NON-ZERO for a column with no migration",
          proc.returncode != 0,
          f"exited {proc.returncode} -- a schema the code cannot run on would ship, "
          f"and every request touching {table} would 500")
    check(f"[{backend}] and the aborted release names the column",
          f"{table}.{column}" in combined,
          "an abort that does not say which column is a puzzle, not a message")

    # Asymmetry. Without this the check would block ordinary model cleanup --
    # and it is also the rollback property: an older, NARROWER slug meeting the
    # wider database it left behind must still release.
    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("drift-extras", env, expect_rc=0)
    check(f"[{backend}] a column the database has and no model declares is NOT a failure",
          rc == 0 and "EXTRAS_TOLERATED=1" in out,
          "production carries four of these; failing on them would make every "
          "model cleanup a blocked deploy, and would break `releases:rollback`")

    # The four declarations _add() could never carry. A fresh database has them
    # all from create_all(), so this is the only shape of test that can see the
    # operations at all -- it removes them first, as production had them removed
    # by never having had them.
    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("constraints-from-production", env, expect_rc=0)
    before = re.search(r"GAPS_BEFORE=(\[.*?\])", out)
    after = re.search(r"GAPS_AFTER=(\[.*?\])", out)
    expected = 4 if backend == "postgresql" else 2      # SQLite cannot drop a FK
    check(f"[{backend}] the production-shaped schema really was missing {expected}",
          before is not None and before.group(1).count("'") // 2 == expected,
          f"got {before.group(1) if before else out[-300:]} -- if this is empty the "
          f"test removed nothing and proves nothing")
    check(f"[{backend}] run_migrations() adds every declared index and foreign key",
          after is not None and after.group(1) == "[]",
          f"still missing after the migration: {after.group(1) if after else out[-300:]}")

    # A name collision must FAIL, not be adopted: IF NOT EXISTS matches the name
    # and not the shape, so adopting would record a uniqueness that is not there.
    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("wrong-shape", env, expect_rc=0)
    check(f"[{backend}] an index with the right name and wrong shape is a FAILURE",
          "WRONG_SHAPE_RAISED=1" in out,
          "a non-unique index of the same name was adopted as if it enforced "
          "uniqueness -- the ledger would then claim it forever")
    check(f"[{backend}] and the failure names the index",
          "NAMED=True" in out)

    # Columns are fatal, constraints are not. Getting this backwards would block
    # every deploy on any gap that has not been closed yet.
    env = {"DATABASE_URL": fresh(), "MIGRATE_ON_BOOT": "0"}
    rc, out = run_child("missing-index", env, expect_rc=0)
    check(f"[{backend}] a declared index the database lacks is REPORTED",
          "results_token" in out and "GAPS=[]" not in out,
          f"schema_constraint_gaps() did not notice a dropped index -- got {out[-300:]!r}")
    check(f"[{backend}] and reporting it does NOT fail the release",
          rc == 0 and "GAP_DID_NOT_BLOCK=1" in out,
          "production has four of these; making them fatal blocks every deploy, "
          "and building them here would take a lock on a live table")


# ===========================================================================
# The upgrade replay -- the only check that runs against a database it did not
# build from the current models
# ===========================================================================

def base_commit() -> tuple[str, str]:
    """(sha, note). sha is '' when there is nothing usable to replay from."""
    requested = os.environ.get("SCHEMA_REPLAY_BASE", "").strip()
    # github.event.before is all-zeros on a branch's first push, and the
    # pull_request base sha is empty on a `push` run. Both mean "no base".
    if requested and set(requested) == {"0"}:
        requested = ""
    # It arrives from a workflow expression, so refuse anything that is not a
    # bare hex sha. Nothing plausible sets it to a git option, but the cost of
    # being sure is one regex and the alternative is passing an attacker-shaped
    # string to `git` as an argument.
    if requested and not re.fullmatch(r"[0-9a-fA-F]{7,64}", requested):
        return "", (f"SCHEMA_REPLAY_BASE={requested!r} is not a commit sha, so it was "
                    f"not passed to git")

    candidates = [requested] if requested else ["HEAD~1"]
    for ref in candidates:
        proc = subprocess.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                              capture_output=True, text=True, cwd=str(REPO_ROOT))
        if proc.returncode == 0:
            return proc.stdout.strip(), ""
        detail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "not found"
        return "", (f"base commit {ref!r} is not in this clone ({detail}). On CI that "
                    f"means actions/checkout needs `fetch-depth: 0`.")
    return "", "no base commit to replay from"


def replay_checks(backend: str, base_url: str, tree: str, sha: str) -> None:
    """Apply THIS branch's release phase to the schema the BASE commit produces.

    This is the shape of every real deploy: an existing database, built by the
    code that is running now, meeting the code that is about to. Nothing else in
    this file gets within reach of it -- every other scenario starts from
    create_all() against the models under test, which cannot be missing anything
    by construction.
    """
    print(f"\nUpgrade replay ({backend}, base {sha[:12]}):")

    url = sqlite_url()[0] if backend == "sqlite" else fresh_pg(base_url)

    # Build the OLD schema from the OLD models. REPO_ROOT and PYTHONPATH both
    # point at the worktree so the scenario dispatcher imports the base tree's
    # app.py and not the one under test -- getting this wrong would silently
    # make the replay compare the branch with itself and always pass.
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--scenario", "build-base-schema"],
        env={**BASE_ENV, "REPO_ROOT": tree, "PYTHONPATH": tree,
             "DATABASE_URL": url, "MIGRATE_ON_BOOT": "0"},
        capture_output=True, text=True, cwd=tree, timeout=300,
    )
    base_out = proc.stdout + proc.stderr
    if proc.returncode != 0 or "BASE_SCHEMA_BUILT" not in base_out:
        # The base commit failing to build its own schema says nothing about
        # this branch -- it is main's problem, or a dependency this PR adds that
        # the old tree cannot import. Loud note, not a failure.
        tail = base_out.strip().splitlines()[-3:]
        print(f"  NOTE  the base tree could not build its own schema, so there is "
              f"nothing to replay against:\n        " + "\n        ".join(tail))
        _warn(f"upgrade replay skipped on {backend}: the base commit {sha[:12]} could "
              f"not build its own schema, so this PR was not compared against the "
              f"schema it will meet. The release-phase check still guards it.")
        return

    seeded = re.search(r"SEEDED=(\d+) SKIPPED=(\d+)(.*)", base_out)
    if seeded:
        # Say it out loud. An ADD COLUMN NOT NULL passes on an empty table and
        # fails on a populated one, so how many tables carry a row is the
        # difference between a real rehearsal and a reassuring one.
        note = f"  note  seeded {seeded.group(1)} table(s), {seeded.group(2)} skipped"
        if seeded.group(2) != "0":
            note += (f" -- an ADD COLUMN NOT NULL against {seeded.group(3).strip()} "
                     f"is not rehearsed")
        print(note)

    # Now the real release command, unmodified, against that database.
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, "DATABASE_URL": url},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        print(out[-3000:], file=sys.stderr)
    check(f"[{backend}] this branch's migrate.py upgrades the base schema cleanly",
          proc.returncode == 0,
          "THIS IS THE DEPLOY. Whatever failed here would abort the release after "
          "merge, with main already containing it")

    applied = re.search(r"(\d+) applied now", out)
    if applied:
        print(f"  note  the replay applied {applied.group(1)} operation(s) for real -- "
              f"the only place in this file that ever executes them")

    # Then read the RESULT, with this script's own comparison rather than
    # app.schema_drift() and rather than a string from run_migrations()'s
    # output. Both of those would make the gate an echo of the thing it is
    # gating: delete schema_drift(), or rename its print, and the check goes on
    # passing while asserting nothing.
    rc, drift_out = run_child("independent-drift",
                              {"DATABASE_URL": url, "MIGRATE_ON_BOOT": "0"}, expect_rc=0)
    found = re.search(r"INDEPENDENT_DRIFT=(\[.*\])", drift_out)
    check(f"[{backend}] the upgraded schema has every column the models declare",
          rc == 0 and found is not None and found.group(1) == "[]",
          f"missing after the upgrade: {found.group(1) if found else drift_out[-300:]}")


def gate_checks() -> None:
    """The boot gate, which everything else depends on."""
    print("\nBoot gate:")
    url_off, _ = sqlite_url()
    rc, out = run_child("boot-gate", {"DATABASE_URL": url_off, "DYNO": "web.1"}, expect_rc=0)
    tables = re.search(r"TABLES=(\d+)", out)
    check("on a dyno, importing app.py creates no tables",
          rc == 0 and tables is not None and tables.group(1) == "0",
          f"created {tables.group(1) if tables else '?'} -- "
          f"the release phase owns the schema, not the web dyno")
    check("and it says so in the log", "Boot migration skipped" in out)

    url_on, _ = sqlite_url()
    rc, out = run_child("boot-gate", {"DATABASE_URL": url_on}, expect_rc=0)
    tables = re.search(r"TABLES=(\d+)", out)
    check("off a dyno (laptop, CI), importing app.py still migrates",
          rc == 0 and tables is not None and int(tables.group(1)) > 10,
          f"created {tables.group(1) if tables else '?'} tables -- "
          f"there is no release phase here, so boot has to do it")

    # MIGRATIONS_STRICT=0 means the release phase was told to ship a schema it
    # could not apply. Boot migration must come back on, or nothing ever can.
    url_soft, _ = sqlite_url()
    rc, out = run_child("boot-gate",
                        {"DATABASE_URL": url_soft, "DYNO": "web.1", "MIGRATIONS_STRICT": "0"},
                        expect_rc=0)
    tables = re.search(r"TABLES=(\d+)", out)
    check("MIGRATIONS_STRICT=0 hands boot migration back its old job",
          rc == 0 and tables is not None and int(tables.group(1)) > 10,
          "otherwise the escape hatch strands the schema with nothing able to repair it")


def db_url_guard_checks() -> None:
    """A dyno must never fall back to the local SQLite file.

    That fallback is right for a laptop and catastrophic on a dyno because it
    SUCCEEDS: it builds a complete, EMPTY schema that migrate.py blesses, that
    schema_drift() sees nothing wrong with, and that /healthz calls "ok". A
    release recorded as successful did exactly that on 2026-08-04, when the
    database attachment was briefly detached.

    BASE_ENV carries no DATABASE_URL, so "no DATABASE_URL" below means genuinely
    ABSENT -- the same shape a detached add-on produces -- rather than a value
    someone remembered to clear.

    Half of these prove the guard fires. The other half matter just as much: a
    guard that fires on a laptop, or on a healthy release, gets reverted within
    a week. ("On a dyno WITH DATABASE_URL, the import still works" is already
    asserted by the first check in gate_checks() above, so it is not repeated.)
    """
    print("\nDATABASE_URL guard:")

    # Every dyno type, not just web.1. Narrowing app.py's predicate to
    # DYNO.startswith('web.') is a plausible tidy-up that would leave the
    # release phase and `heroku run` back on the blank file, and a check
    # hardcoded to web.1 would stay green through it.
    for dyno in ("web.1", "release.1234", "run.5678"):
        rc, out = run_child("import-app", {"DYNO": dyno}, expect_rc=1)
        check(f"on dyno {dyno} with no DATABASE_URL, importing app.py refuses to start",
              rc != 0 and "IMPORTED_OK" not in out and "DATABASE_URL is not set" in out,
              f"rc={rc} -- it fell back to a blank SQLite file instead, which is the "
              f"whole failure this guards")
    check("and the refusal says how to fix it", "addons:attach" in out,
          "the message is read at 3am in `heroku logs --tail`; keep it actionable")

    # MIGRATE_ON_BOOT=0 only so this does not run create_all() against whatever
    # instance/proposals.db a developer happens to have. The import is the test.
    rc, out = run_child("import-app", {"MIGRATE_ON_BOOT": "0"}, expect_rc=0)
    check("off a dyno with no DATABASE_URL, app.py still imports (local dev)",
          rc == 0 and "IMPORTED_OK" in out,
          f"rc={rc} -- `python app.py` on a laptop must keep working with "
          f"nothing configured")

    # The release phase makes the same call BEFORE importing app, so that this
    # one exit stays closed when the migration bypass is open. If the check ever
    # moves after the import it lands in _abort(), and this goes red.
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, "DYNO": "release.1", "MIGRATIONS_STRICT": "0"},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    out = proc.stdout + proc.stderr
    check("the release phase aborts with no DATABASE_URL, even at MIGRATIONS_STRICT=0",
          proc.returncode != 0 and "RELEASE ABORTED: DATABASE_URL IS NOT SET" in out,
          f"rc={proc.returncode} -- exit 0 here promotes a slug whose dynos have "
          f"nothing to talk to, while the old ones are already gone")

    # The other half of the same fault: the variable is SET, but to SQLite. That
    # migrates a file on the release dyno's throwaway disk and exits 0 today.
    url, _ = sqlite_url()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, "DYNO": "release.1", "DATABASE_URL": url,
             "MIGRATIONS_STRICT": "0"},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    out = proc.stdout + proc.stderr
    check("the release phase refuses to migrate SQLite on a dyno",
          proc.returncode != 0 and "NOT A POSTGRES DATABASE" in out,
          f"rc={proc.returncode} -- a green release against a throwaway file is "
          f"the 2026-08-04 fault with the variable present instead of absent")

    # And the line that could block a good deploy. Off a dyno -- the shape
    # ci.yml's responsive audit uses -- a sqlite release must still complete, or
    # the guard has broken the deploys it was added to protect.
    url, _ = sqlite_url()
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, "DATABASE_URL": url},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300,
    )
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        print(out[-2000:], file=sys.stderr)
    check("off a dyno, a sqlite release still completes (ci.yml's audit does this)",
          proc.returncode == 0 and "Migration complete." in out,
          f"rc={proc.returncode} -- the guard is blocking the deploys it was "
          f"added to protect")

    # The live DATABASE_URL still uses Heroku's legacy postgres:// scheme, which
    # app.py rewrites. A backend assertion written against the RAW value would
    # pass CI and abort the very next real deploy, so prove the rewrite is
    # applied first: this must get PAST the scheme test and fail later, on the
    # connection to a host that does not exist.
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migrate.py")],
        env={**BASE_ENV, "DYNO": "release.1",
             "DATABASE_URL": "postgres://u:p@127.0.0.1:1/nonexistent-db-for-ci"},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=120,
    )
    out = proc.stdout + proc.stderr
    check("a legacy postgres:// URL passes the backend test (it is what prod sends)",
          "NOT A POSTGRES DATABASE" not in out,
          "the scheme test ran before the postgres:// -> postgresql:// rewrite, "
          "which would abort the next real deploy")


def main() -> int:
    print("=== MIGRATION SAFETY CHECK ===")
    static_checks()
    gate_checks()
    db_url_guard_checks()

    sha, why_not = base_commit()
    tree = ""
    if sha:
        # `git archive`, NOT `git worktree add`. A worktree registers state under
        # .git/worktrees that only an explicit `worktree remove` clears, so any
        # run killed between the two leaks an entry into the developer's real
        # repository -- there are five such leaks in this clone right now, from
        # other tooling. An archive is a plain directory: nothing to unregister,
        # and a killed run leaves a temp dir the OS reaps.
        tree = tempfile.mkdtemp(prefix="wig-migreplay-")
        tar_path = os.path.join(tree, "_base.tar")
        proc = subprocess.run(["git", "archive", "--format=tar", "-o", tar_path, sha],
                              capture_output=True, text=True, cwd=str(REPO_ROOT))
        if proc.returncode != 0:
            print(f"\n  NOTE  could not export base commit {sha[:12]} for the upgrade "
                  f"replay: {proc.stderr.strip()[-300:]}")
            tree = ""
        else:
            # filter="data" is not optional and has no fallback. It refuses
            # absolute paths, `..` traversal, links out of the tree and device
            # files, and it has been in every Python since 3.11.4 -- runtime.txt
            # pins 3.11.7 and CI installs 3.11.x, so there is nothing here to be
            # tolerant of. An unfiltered extractall would also fail the bandit
            # gate (B202), which is the correct reaction to writing one.
            with tarfile.open(tar_path) as tf:
                tf.extractall(tree, filter="data")
            os.remove(tar_path)
    else:
        print(f"\n  NOTE  UPGRADE REPLAY SKIPPED -- {why_not}\n"
              f"        Nothing here compared this branch against the schema it will "
              f"actually meet.\n"
              f"        run_migrations() still makes the same check against the real "
              f"database at release\n        time, so this is a loss of earliness, not "
              f"of protection.")
        _warn(f"upgrade replay skipped: {why_not}")

    try:
        db_checks("sqlite", "")
        drift_checks("sqlite", "")
        if tree:
            replay_checks("sqlite", "", tree, sha)

        pg = os.environ.get("MIGRATION_TEST_DATABASE_URL", "").strip()
        if pg:
            try:
                db_checks("postgresql", pg)
                drift_checks("postgresql", pg)
                if tree:
                    replay_checks("postgresql", pg, tree, sha)
            finally:
                drop_pg_schemas()
        else:
            _no_postgres_note()
    finally:
        if tree:
            shutil.rmtree(tree, ignore_errors=True)

    if failures:
        print("\n=== MIGRATION SAFETY CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n=== MIGRATION SAFETY CHECK OK ===")
    return 0


def _no_postgres_note() -> None:
    # Not a failure -- but say it loudly. The Postgres branch is the one
    # production runs, and it is the half SQLite never executes.
    print("\n  NOTE  MIGRATION_TEST_DATABASE_URL is not set, so the PostgreSQL "
          "branch\n        (information_schema snapshot, SET LOCAL lock_timeout, "
          "SQLSTATE 55P03,\n        pg_index verification) was NOT exercised, and "
          "neither was the\n        upgrade replay against Postgres -- which is the "
          "only place a new\n        ALTER TABLE is ever really executed.")


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        name = sys.argv[sys.argv.index("--scenario") + 1]
        sys.path.insert(0, str(REPO_ROOT))
        sys.exit(SCENARIOS[name]())
    sys.exit(main())
