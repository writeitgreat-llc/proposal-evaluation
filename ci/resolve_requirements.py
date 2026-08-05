#!/usr/bin/env python3
"""
resolve_requirements.py -- regenerate requirements.lock on the platform that runs it.

Prints a complete, sorted `name==version` list of everything `pip install -r
requirements.txt` would install, WITHOUT installing anything and WITHOUT reading the
existing lock (it strips the `-c` line first, so the answer is a fresh resolution
rather than a restatement of what is already pinned).

RUN IT ON A DYNO, NOT YOUR LAPTOP:

    cat ci/resolve_requirements.py | heroku run --no-tty -a <app> \\
        "cat > /tmp/res.py && python /tmp/res.py"

then paste the block between LOCKSTART and LOCKEND into requirements.lock, keeping the
comment header.

Why a dyno. Resolution depends on the platform, and for this stack it really differs:
SQLAlchemy declares `greenlet>=1 ; platform_machine == "x86_64" or ...`, so greenlet is
part of the build on Heroku (linux/x86_64) and absent on an Apple-silicon laptop
(arm64). Resolving on a Mac produced 60 packages where the dyno produced 61. A lock
built in the wrong place silently leaves real packages unpinned, which is the exact
failure ci/check_requirements_lock.py exists to catch -- so it would fail CI, but the
five minutes are better spent not making the mistake.

The header this prints records WHICH platform and interpreter produced the list, so a
lock can always be traced back to the machine that made it.
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    req = Path("requirements.txt")
    if not req.is_file():
        print("ERROR: run this from the repo root (no requirements.txt here)", file=sys.stderr)
        return 2

    # Strip `-c requirements.lock` so this is a FRESH resolution. Left in, pip would
    # simply hand back the versions the lock already names, and regenerating could
    # never pick up a legitimately-updated transitive dependency.
    stripped = "\n".join(
        line for line in req.read_text(encoding="utf-8").splitlines()
        if not re.match(r"^\s*-c\s+", line)
    )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_req = Path(tmp) / "requirements.txt"
        tmp_req.write_text(stripped, encoding="utf-8")
        report = Path(tmp) / "report.json"
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
             "--quiet", "--report", str(report), "-r", str(tmp_req)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print(proc.stderr[-3000:], file=sys.stderr)
            print("RESOLVE_FAILED", proc.returncode, file=sys.stderr)
            return 1
        data = json.loads(report.read_text(encoding="utf-8"))

    rows = sorted((i["metadata"]["name"].lower(), i["metadata"]["version"])
                  for i in data.get("install", []))

    print("PLATFORM %s  PYTHON %s  PACKAGES %d"
          % (platform.machine(), ".".join(map(str, sys.version_info[:3])), len(rows)))
    print("LOCKSTART")
    for name, version in rows:
        print(f"{name}=={version}")
    print("LOCKEND")
    return 0


if __name__ == "__main__":
    sys.exit(main())
