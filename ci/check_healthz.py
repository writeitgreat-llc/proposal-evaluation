#!/usr/bin/env python3
"""check_healthz.py -- /healthz reports an EMPTY database as down, and nothing else.

WHY THIS EXISTS. Until 2026-08-05 this endpoint ran `SELECT 1`, which any
database answers -- including a brand-new empty one. On 2026-08-04 the Postgres
attachment was briefly detached, config fell through to the local SQLite
fallback, and /healthz reported {"ok":true,"db":"ok"} while every sign-in 500ed.
The uptime monitor stayed green through the whole thing.

WHAT IS ACTUALLY HARD HERE, and why this file is longer than the fix. A probe
strict enough to catch that is one line; a probe that does not cause MORE
outages than it prevents is the work. Three ways the obvious version misfires,
all of them proven before this was written:

  1. It must PASS on a correct-but-empty database. ci/smoke_app.py builds a
     fresh schema with zero rows and demands 200 from this route, and so does
     every laptop before migrations run. A probe that 503s there is deleted
     within a week rather than fixed.
  2. It must PASS while a migration holds the table. The schema probe reads a
     real table, so unlike `SELECT 1` it takes a lock. Every Heroku release
     migrates while the OLD dynos still serve, so an ALTER on `author` queued
     behind any slow reader blocks this probe -- and the site itself is fine,
     because anonymous pages never touch `author`. Measured: 503 from the
     probe, 200 from every real page. That is a 3am page caused by the check.
  3. It must not fail a DEPLOY. Both deploy smoke tests assert 200 and
     `"ok":true` on this route, so a false 503 fails the deploy of an
     already-merged main.

So half of the checks below assert the probe FIRES and half assert it does NOT.
The second half is the load-bearing half.

Runs against SQLite by default. Set HEALTHZ_TEST_DATABASE_URL to a PostgreSQL
server to exercise the lock and blank-database scenarios, which are the ones
SQLite cannot express -- CI does this.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_ENV = {
    "OPENAI_API_KEY": "ci-dummy-openai-credential",
    "SECRET_KEY": "ci-healthz-check-key",
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


PROBE = r"""
import json, os, sys, time
import app as A
c = A.app.test_client()
# Time the REQUEST, not the process. Importing app.py costs a second or more and
# varies with the runner, which would swamp the thing being measured -- the
# probe's own wait. See the lock scenario, where 250ms and 5s are the whole
# difference between the fix being present and absent.
_t = time.monotonic()
r = c.get('/healthz')
_ms = (time.monotonic() - _t) * 1000
print("HEALTHZ_RESULT=" + json.dumps(
    {"status": r.status_code, "body": r.get_json(), "ms": _ms}))
"""


def probe(env: dict, timeout: int = 120) -> tuple[int, dict, float, str]:
    """Boot app.py in a child process and GET /healthz.

    Returns (status, body, request_ms, raw_output).
    """
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        env={**BASE_ENV, **env},
        capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=timeout,
    )
    raw = proc.stdout + proc.stderr
    for line in proc.stdout.splitlines():
        if line.startswith("HEALTHZ_RESULT="):
            payload = json.loads(line.split("=", 1)[1])
            return payload["status"], payload["body"] or {}, payload["ms"], raw
    return -1, {}, -1.0, raw


def sqlite_url() -> str:
    return "sqlite:///" + str(Path(tempfile.mkdtemp(prefix="wig-healthz-")) / "check.db")


def sqlite_checks() -> None:
    print("\nDoes NOT misfire (the half that keeps this check alive):")

    # The exact shape ci/smoke_app.py produces: real schema, zero rows, no dyno.
    st, body, _ms, raw = probe({"DATABASE_URL": sqlite_url(), "MIGRATE_ON_BOOT": "1"})
    check("off a dyno, a correct-but-EMPTY database is healthy",
          st == 200 and body.get("ok") is True,
          f"status={st} body={body} -- ci/smoke_app.py demands 200 here, and a "
          f"laptop before `flask db upgrade` looks identical")
    check("and it still reports the schema as readable",
          body.get("schema") == "ok", f"schema={body.get('schema')!r}")

    # A laptop with nothing built at all. Reported, but NOT a failure off a dyno.
    st, body, _ms, raw = probe({"DATABASE_URL": sqlite_url(), "MIGRATE_ON_BOOT": "0"})
    check("off a dyno, a database with no tables at all is still healthy",
          st == 200 and body.get("ok") is True,
          f"status={st} body={body} -- a contributor's first `flask run` must "
          f"not be told the site is down")
    check("and the facts are reported honestly anyway",
          body.get("schema") == "unreadable" and body.get("reason") is None,
          f"schema={body.get('schema')!r} reason={body.get('reason')!r}")

    print("\nDoes fire (the 2026-08-04 shape):")

    st, body, _ms, raw = probe({"DATABASE_URL": sqlite_url(), "MIGRATE_ON_BOOT": "1",
                           "DYNO": "web.1"})
    check("on a dyno, a SQLite backend is reported DOWN even with a full schema",
          st == 503 and body.get("ok") is False,
          f"status={st} body={body} -- this is exactly what answered 200 on "
          f"2026-08-04 while every sign-in was failing")
    check("and it names which check failed",
          body.get("reason") == "backend_not_postgres",
          f"reason={body.get('reason')!r} -- the token is what makes the log "
          f"greppable at 3am")

    print("\nThe response contract the fleet monitor depends on:")
    st, body, _ms, _raw = probe({"DATABASE_URL": sqlite_url(), "MIGRATE_ON_BOOT": "1"})
    # Four Keyword monitors across two vendors match the literal `"ok":true`,
    # and both deploy smokes parse `release`. Dropping or renaming one of these
    # does not break this app -- it silently breaks monitoring for all three.
    for key in ("ok", "db", "release", "time", "backend", "schema", "reason"):
        check(f"the body still carries {key!r}", key in body, f"body={body}")


def postgres_checks(base_url: str) -> None:
    from sqlalchemy import create_engine, text

    print("\nPostgreSQL-only scenarios (SQLite cannot express these):")

    # --- a BLANK Postgres: the restored-wrong / newly-provisioned shape -------
    blank = f"healthz_blank_{uuid.uuid4().hex[:10]}"
    engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
    with engine.begin() as conn:
        conn.execute(text(f'CREATE DATABASE "{blank}"'))
    engine.dispose()
    sep = "&" if "?" in base_url else "?"
    blank_url = base_url.rsplit("/", 1)[0] + "/" + blank

    try:
        st, body, _ms, raw = probe({"DATABASE_URL": blank_url, "MIGRATE_ON_BOOT": "0",
                               "DYNO": "web.1"})
        check("on a dyno, a real Postgres with NO SCHEMA is reported DOWN",
              st == 503 and body.get("reason") == "schema_unreadable",
              f"status={st} body={body} -- the connection works, so `SELECT 1` "
              f"is happy; only reading a real table catches this")
        check("and it does not blame the connection",
              body.get("db") == "ok",
              f"db={body.get('db')!r} -- saying the database is down sends the "
              f"reader to Postgres instead of to the attachment")

        # --- the migrated, healthy database ---------------------------------
        st, body, _ms, raw = probe({"DATABASE_URL": blank_url, "MIGRATE_ON_BOOT": "1",
                               "DYNO": "web.1"})
        check("on a dyno, a migrated Postgres with zero rows is healthy",
              st == 200 and body.get("ok") is True and body.get("schema") == "ok",
              f"status={st} body={body} -- a first deploy has exactly this shape")

        # --- THE LOCK SCENARIO ------------------------------------------------
        # The regression this file exists to prevent. Hold ACCESS EXCLUSIVE on
        # `author` -- what an ALTER TABLE does, and what every release phase
        # does while the old dynos still serve -- and the probe must report the
        # table as LOCKED (healthy, it demonstrably exists) rather than as
        # unreadable (down). Without the lock_timeout + SQLSTATE classification
        # this returns 503 and pages someone for a site that is serving fine.
        holder_up = threading.Event()
        release = threading.Event()

        def hold_lock():
            eng = create_engine(blank_url)
            with eng.begin() as conn:
                conn.execute(text("LOCK TABLE author IN ACCESS EXCLUSIVE MODE"))
                holder_up.set()
                release.wait(timeout=90)
            eng.dispose()

        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        if not holder_up.wait(timeout=30):
            check("could take an ACCESS EXCLUSIVE lock on author", False,
                  "the lock holder never started; the lock scenario did NOT run")
        else:
            st, body, req_ms, raw = probe({"DATABASE_URL": blank_url,
                                           "MIGRATE_ON_BOOT": "0",
                                           "DYNO": "web.1"})
            release.set(); t.join(timeout=30)

            check("a migration holding the table does NOT report the site down",
                  st == 200 and body.get("ok") is True,
                  f"status={st} body={body} -- a queued ALTER TABLE blocks this "
                  f"probe while every real page still serves; reporting that as "
                  f"an outage is a 3am page caused by the check itself")
            check("and it says the table is locked, not missing",
                  body.get("schema") == "locked",
                  f"schema={body.get('schema')!r} -- 'unreadable' here would mean "
                  f"the SQLSTATE classification stopped distinguishing 55P03/57014 "
                  f"from a genuinely absent table")
            # The probe must refuse to QUEUE, not merely survive queuing: a 5s
            # wait per poll, once a minute, from several regions, pins request
            # threads during the busiest moment of a deploy.
            # THE ASSERTION THAT CATCHES A MISSING lock_timeout. Without it the
            # probe still ends up reporting "locked" -- statement_timeout fires
            # at 5s and raises 57014, which classifies the same way -- so the
            # verdict alone cannot tell the two apart. Only the WAIT can. A
            # 5s stall per poll, once a minute, from several monitoring
            # regions, during the deploy window when the app is busiest, is the
            # cost this bounds. 2s is far above the 250ms ceiling and far below
            # the 5s statement_timeout, so it is not flaky on a slow runner.
            check("and it gives up in milliseconds rather than waiting out the lock",
                  0 <= req_ms < 2000,
                  f"the /healthz request itself took {req_ms:.0f}ms -- lock_timeout "
                  f"should cap the wait at 250ms. Near 5000ms means lock_timeout "
                  f"is gone and statement_timeout is doing the job instead, which "
                  f"pins a request thread for every poll")
    finally:
        engine = create_engine(base_url, isolation_level="AUTOCOMMIT")
        with engine.begin() as conn:
            conn.execute(text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{blank}' AND pid <> pg_backend_pid()"))
            conn.execute(text(f'DROP DATABASE IF EXISTS "{blank}"'))
        engine.dispose()


def main() -> int:
    print("=== /healthz CHECK ===")
    sqlite_checks()

    pg = os.environ.get("HEALTHZ_TEST_DATABASE_URL", "").strip()
    if pg:
        postgres_checks(pg)
    else:
        print("\n  NOTE  HEALTHZ_TEST_DATABASE_URL is not set, so the blank-database\n"
              "        and table-lock scenarios did NOT run. The lock one is the\n"
              "        regression guard for the false alarm this probe can cause;\n"
              "        SQLite has no lock_timeout and no SQLSTATEs, so it cannot\n"
              "        stand in. CI sets this.")

    if failures:
        print("\n=== /healthz CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n=== /healthz CHECK OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
