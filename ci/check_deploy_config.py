#!/usr/bin/env python3
"""Check that the Heroku deployment config in this repo is coherent.

Hard failures (these break the deploy or make CI a lie):
  * no Procfile, or no `web:` process in it
  * no runtime.txt
  * runtime.txt pins a Python version that CI is not actually testing on

Warnings (emitted as GitHub annotations, do NOT fail the build):
  * a migrations/ directory or a migration script exists but the Procfile has no
    `release:` line -- see the note at the bottom of this file

Usage:
    python3 ci/check_deploy_config.py
    python3 ci/check_deploy_config.py --strict   # turn warnings into failures
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])


def annotate(level: str, message: str, file: str | None = None) -> None:
    """Print a GitHub Actions annotation (and a readable line for humans)."""
    location = f" file={file}::" if file else "::"
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}{location}{message}")
    else:
        print(f"[{level.upper()}] {message}")


def main() -> int:
    strict = "--strict" in sys.argv
    errors: list[str] = []
    warnings: list[str] = []

    # --- Procfile -----------------------------------------------------------
    procfile = REPO_ROOT / "Procfile"
    if not procfile.is_file():
        errors.append("Procfile is missing -- Heroku will not know how to boot the app.")
        procfile_text = ""
    else:
        procfile_text = procfile.read_text(encoding="utf-8")
        processes = {}
        for line in procfile_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                name, command = line.split(":", 1)
                processes[name.strip()] = command.strip()

        if "web" not in processes:
            errors.append("Procfile has no `web:` process -- Heroku would boot nothing.")
        else:
            print(f"  ok  Procfile web:     {processes['web']}")

        if "release" in processes:
            print(f"  ok  Procfile release: {processes['release']}")
        else:
            has_migrations = (REPO_ROOT / "migrations").is_dir()
            has_migrate_script = (REPO_ROOT / "migrate.py").is_file()
            if has_migrations or has_migrate_script:
                what = "a migrations/ directory" if has_migrations else "a migrate.py script"
                warnings.append(
                    f"Procfile has NO `release:` phase, but this repo has {what}. "
                    f"Schema changes will not be applied on deploy, and when they "
                    f"are applied by hand the app can boot against a schema it does "
                    f"not match. Recommended fix: add a release line to the Procfile "
                    f"(see the note in ci/check_deploy_config.py)."
                )

    # --- runtime.txt --------------------------------------------------------
    runtime = REPO_ROOT / "runtime.txt"
    if not runtime.is_file():
        errors.append(
            "runtime.txt is missing -- Heroku picks its own default Python, which "
            "may not be the version CI tested on."
        )
    else:
        pinned = runtime.read_text(encoding="utf-8").strip()
        match = re.match(r"^python-(\d+)\.(\d+)\.(\d+)$", pinned)
        if not match:
            errors.append(f"runtime.txt is malformed: {pinned!r} (expected python-X.Y.Z)")
        else:
            major, minor = match.group(1), match.group(2)
            print(f"  ok  runtime.txt:      {pinned}")
            running = f"{sys.version_info.major}.{sys.version_info.minor}"
            if running != f"{major}.{minor}":
                errors.append(
                    f"runtime.txt pins Python {major}.{minor} but this check is "
                    f"running on Python {running}. CI must test on the version "
                    f"Heroku will actually run, or CI proves nothing. Fix the "
                    f"python-version in .github/workflows/ci.yml (or runtime.txt)."
                )

    # --- report -------------------------------------------------------------
    for warning in warnings:
        annotate("warning", warning, file="Procfile")

    if errors:
        print("\n=== DEPLOY CONFIG CHECK FAILED ===", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    if warnings and strict:
        print("\n=== DEPLOY CONFIG CHECK FAILED (--strict) ===", file=sys.stderr)
        return 1

    suffix = f" ({len(warnings)} warning(s))" if warnings else ""
    print(f"\n=== DEPLOY CONFIG OK ==={suffix}")
    return 0


# ---------------------------------------------------------------------------
# NOTE ON THE RELEASE PHASE
#
# proposal-evaluation already has one:
#     release: python migrate.py
#     web: gunicorn app:app --timeout 120 --workers 1 --threads 4
#
# The marketing site does NOT, even though it uses Flask-Migrate and has
# migrations/versions/*.py. The recommended line is:
#
#     release: flask db upgrade
#     web: gunicorn "app:create_app()" --bind 0.0.0.0:$PORT
#
# (FLASK_APP="app:create_app()" must be set as a Heroku config var for that to
# resolve, or use `python -m flask db upgrade`.)
#
# WHY IT MATTERS: with a release phase, a failed migration ABORTS THE RELEASE --
# Heroku keeps the previous dynos running and the old code stays live. Without
# one, the new code boots against the old schema and every page that touches the
# new column 500s until someone notices and runs the migration by hand.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.exit(main())
