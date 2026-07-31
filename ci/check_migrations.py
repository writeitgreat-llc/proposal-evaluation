#!/usr/bin/env python3
"""Prove the three properties run_migrations() is required to have.

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

Plus the wiring those properties rest on: importing app.py must not touch the
schema on a dyno, `_add()` keys must be unique, and the Procfile must still have
the release phase that runs any of this.

Runs against SQLite by default. Set MIGRATION_TEST_DATABASE_URL to a PostgreSQL
DSN (CI does) to run the same battery against Postgres -- which matters more
than it looks: the Postgres branch is the one production uses, and it contains
all the code SQLite never executes (information_schema snapshot, SET LOCAL
lock_timeout, SQLSTATE 55P03 handling, pg_index verification).

Usage:
    python ci/check_migrations.py
    MIGRATION_TEST_DATABASE_URL=postgresql://... python ci/check_migrations.py
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
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


SCENARIOS = {
    "boot-gate": _scenario_boot_gate,
    "battery": _scenario_battery,
    "strict-split": _scenario_strict_split,
    "poison-only": _scenario_poison_only,
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


def main() -> int:
    print("=== MIGRATION SAFETY CHECK ===")
    static_checks()
    gate_checks()

    db_checks("sqlite", "")

    pg = os.environ.get("MIGRATION_TEST_DATABASE_URL", "").strip()
    if pg:
        try:
            db_checks("postgresql", pg)
        finally:
            drop_pg_schemas()
    else:
        # Not a failure -- but say it loudly. The Postgres branch is the one
        # production runs, and it is the half SQLite never executes.
        print("\n  NOTE  MIGRATION_TEST_DATABASE_URL is not set, so the PostgreSQL "
              "branch\n        (information_schema snapshot, SET LOCAL lock_timeout, "
              "SQLSTATE 55P03,\n        pg_index verification) was NOT exercised.")

    if failures:
        print("\n=== MIGRATION SAFETY CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n=== MIGRATION SAFETY CHECK OK ===")
    return 0


if __name__ == "__main__":
    if "--scenario" in sys.argv:
        name = sys.argv[sys.argv.index("--scenario") + 1]
        sys.path.insert(0, str(REPO_ROOT))
        sys.exit(SCENARIOS[name]())
    sys.exit(main())
