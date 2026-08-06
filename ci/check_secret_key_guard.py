#!/usr/bin/env python3
"""The signing-key guard fires on a dyno and stays out of everyone else's way.

app.py refuses to start on a Heroku dyno when SECRET_KEY is missing, because
the fallback it would otherwise use is a placeholder printed in this PUBLIC
repository — and a session signed with a public key is a session anyone can
mint. This file proves both halves, the same way ci/check_migrations.py proves
the DATABASE_URL guard beside it:

  * the guard FIRES on every dyno type when SECRET_KEY is absent or blank,
    and the refusal says how to fix it;
  * the guard does NOT fire on a laptop, and does NOT fire on a dyno that has
    a real key — a safety catch that goes off at the wrong moment gets
    removed within a week.

It also pins the fallback itself: the dev placeholder must never grow a
lookalike that reads as strong, and no OTHER read of SECRET_KEY may
reintroduce a default behind the guard's back.

Run from the repo root: python ci/check_secret_key_guard.py
"""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
APP_PY = REPO_ROOT / "app.py"

# Mirrors ci/check_migrations.py BASE_ENV, minus SECRET_KEY — its absence is
# the thing under test, so it must be genuinely absent rather than inherited
# from the caller's shell.
BASE_ENV = {
    "OPENAI_API_KEY": "ci-dummy-openai-credential",
    "APP_BASE_URL": "http://localhost:5000",
    # Import must never touch a database: MIGRATE_ON_BOOT=0 keeps the boot
    # gate quiet, and an explicit sqlite URL keeps the DATABASE_URL guard —
    # which sits BELOW the key guard — from firing first and masking it.
    "MIGRATE_ON_BOOT": "0",
    "DATABASE_URL": "sqlite:///ci-secret-key-guard.db",
    "PYTHONPATH": str(REPO_ROOT),
    "PATH": os.environ.get("PATH", ""),
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok ' if ok else 'FAIL'} {label}")
    if not ok:
        failures.append(f"{label}{(' -- ' + detail) if detail else ''}")


def import_app(env: dict) -> tuple[int, str]:
    """Import app.py in a child interpreter and report whether it survived."""
    proc = subprocess.run(
        [sys.executable, "-c", "import app; print('IMPORTED_OK')"],
        env={**BASE_ENV, **env},
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    print("== the guard fires on a dyno with no SECRET_KEY ==")
    # All three dyno types: a guard narrowed to web.* would leave the release
    # phase and `heroku run` happily signing with the public placeholder.
    for dyno in ("web.1", "release.1234", "run.5678"):
        rc, out = import_app({"DYNO": dyno})
        check(
            f"refuses to import on DYNO={dyno}",
            rc != 0 and "IMPORTED_OK" not in out and "SECRET_KEY is not set" in out,
            out[-1500:],
        )
    rc, out = import_app({"DYNO": "web.1", "SECRET_KEY": "   "})
    check(
        "a whitespace-only key counts as missing",
        rc != 0 and "SECRET_KEY is not set" in out,
        out[-1500:],
    )
    rc, out = import_app({"DYNO": "web.1"})
    check(
        "and the refusal says how to fix it",
        "config:set" in out and "token_hex" in out,
        "the message is read at 3am in `heroku logs --tail`; keep it actionable",
    )

    print("== and stays out of the way everywhere else ==")
    rc, out = import_app({})
    check(
        "a laptop with no SECRET_KEY still imports (dev fallback)",
        rc == 0 and "IMPORTED_OK" in out,
        out[-1500:],
    )
    rc, out = import_app({"DYNO": "web.1", "SECRET_KEY": "ci-strong-enough-for-a-test"})
    check(
        "a dyno WITH a key imports normally",
        rc == 0 and "IMPORTED_OK" in out,
        out[-1500:],
    )

    print("== the release phase refuses too, outside the strict-mode bypass ==")
    # app.py's guard raises inside migrate.py's try, which lands in _abort(),
    # which honours MIGRATIONS_STRICT=0 — so migrate.py carries its own copy
    # BEFORE the import. The postgres:// URL below gets past the database
    # backend test so the key check is the one being exercised; the key check
    # runs before anything connects, so the unreachable host never matters.
    proc = subprocess.run(
        [sys.executable, "migrate.py"],
        env={
            **{k: v for k, v in BASE_ENV.items()},
            "DYNO": "release.1",
            "MIGRATIONS_STRICT": "0",
            "DATABASE_URL": "postgres://u:p@127.0.0.1:1/nonexistent-db-for-ci",
        },
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    check(
        "a release dyno with no SECRET_KEY aborts even at MIGRATIONS_STRICT=0",
        proc.returncode != 0 and "SECRET_KEY IS NOT SET" in out,
        out[-1500:],
    )
    scratch = Path(tempfile.mkdtemp(prefix="wig-secretkey-")) / "check.db"
    proc = subprocess.run(
        [sys.executable, "migrate.py"],
        env={**BASE_ENV, "DATABASE_URL": f"sqlite:///{scratch}"},
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    out = proc.stdout + proc.stderr
    check(
        "off a dyno, migrate.py still completes with no SECRET_KEY",
        proc.returncode == 0 and "Migration complete." in out,
        out[-1500:],
    )

    print("== the fallback cannot quietly change shape ==")
    src = APP_PY.read_text(encoding="utf-8")
    reads = re.findall(r"environ(?:\.get)?\(\s*['\"]SECRET_KEY['\"]([^)]*)\)", src)
    check(
        "app.py reads SECRET_KEY in exactly two places (guard + assignment)",
        len(reads) == 2,
        f"found {len(reads)} reads -- a third read with its own default would "
        "reintroduce a fallback the guard cannot see",
    )
    # A read's "default" is whatever follows the comma. The guard's own read
    # defaults to '' (so a missing key stays falsy); the assignment's default
    # must be the one known dev placeholder and nothing else — a second,
    # stronger-looking default is how the guard gets bypassed while reading as
    # an improvement.
    real_defaults = [
        r for r in reads
        if "," in r and re.sub(r"[,\s'\"]", "", r) not in ("", "strip()")
    ]
    check(
        "the only default is the known dev placeholder",
        len(real_defaults) == 1
        and "dev-secret-key-change-in-production" in real_defaults[0],
        f"non-empty defaults found: {real_defaults!r}",
    )

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f.splitlines()[0][:200]}")
        return 1
    print("All secret-key guard checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
