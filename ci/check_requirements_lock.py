#!/usr/bin/env python3
"""
check_requirements_lock.py -- prove that requirements.lock actually pins the build.

WHY THIS EXISTS. requirements.txt names the packages this app needs; requirements.lock
names the exact version of EVERY package that ends up installed, including the
transitive ones nobody chose. requirements.txt carries `-c requirements.lock` on its
first line, so pip -- including the pip Heroku's buildpack runs -- constrains every
install to those versions.

THE FAILURE THIS CATCHES, which is not the obvious one. A constraints file is not a
manifest: pip does not require that everything it installs appears there. A package
missing from the lock is not an error, it is simply UNCONSTRAINED -- it installs at
whatever version is newest that day, silently, while the lock file sitting next to it
gives every appearance of having pinned the build. Demonstrated before this was written:

    requirements.txt : -c requirements.lock
                       requests==2.32.5
    requirements.lock: requests==2.32.5          (and nothing else)

    -> resolves 5 packages. certifi, charset-normalizer, idna and urllib3 all escape
       the lock and float free, with no warning from pip at any point.

So the assertion here is COVERAGE, not agreement: every package the resolver actually
produces must appear in the lock. Comparing the resolved versions to the locked ones
would be close to vacuous -- the `-c` line forces them to agree, so the check would be
asking pip to confirm what pip was just told.

WHY THE LOCK IS GENERATED ON A DYNO, NOT A LAPTOP. Dependency resolution is
platform-dependent, and for this stack it genuinely differs:

    SQLAlchemy declares  greenlet>=1 ; platform_machine == "aarch64" or ... "x86_64" ...

Heroku is linux/x86_64, so greenlet IS part of the build. An Apple-silicon laptop is
arm64, so it is NOT. Resolving these apps on a Mac produced 60 packages; resolving the
same requirements.txt on this app's own dyno produced 61 -- the difference being
greenlet, the concurrency substrate underneath SQLAlchemy. A laptop-generated lock
would have silently left exactly that package floating.

Regenerate the lock the same way it was made, on the platform that runs it:

    cat ci/resolve_requirements.py | heroku run --no-tty -a <app> \\
        "cat > /tmp/res.py && python /tmp/res.py"

CI runs on linux/x86_64 too, which is why this check can be trusted to notice a lock
built somewhere else.

Exit codes: 0 = pass, 1 = the lock does not cover the build, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# `name==version`, tolerating a trailing environment marker.
PIN_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*==\s*([^\s;#]+)")


def normalise(name: str) -> str:
    """PEP 503 normalisation: Flask_SQLAlchemy, flask-sqlalchemy and FLASK.SQLALCHEMY
    are one package, and the resolver and the lock file will not agree on spelling."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_lock(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN_RE.match(line)
        if not m:
            raise ValueError(f"{path.name}: cannot parse {line!r} -- every line must be name==version")
        pins[normalise(m.group(1))] = m.group(2)
    return pins


def read_direct_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        m = PIN_RE.match(line)
        if m:
            pins[normalise(m.group(1))] = m.group(2)
    return pins


def resolve(requirements: Path, timeout: float) -> dict[str, str]:
    """What pip would actually install. Never installs anything."""
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--ignore-installed",
               "--quiet", "--report", str(report), "-r", str(requirements)]
        print("$ " + " ".join(cmd[:6]) + " ... -r " + str(requirements), flush=True)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            print(proc.stdout[-2000:], file=sys.stderr)
            print(proc.stderr[-3000:], file=sys.stderr)
            print("\nERROR: pip could not resolve %s." % requirements, file=sys.stderr)
            print("If the message above mentions conflicting dependencies, requirements.txt and",
                  file=sys.stderr)
            print("requirements.lock disagree about a version. That is a HARD failure by design:",
                  file=sys.stderr)
            print("it stops the build rather than quietly installing the wrong thing.", file=sys.stderr)
            raise SystemExit(2)
        data = json.loads(report.read_text(encoding="utf-8"))
    return {normalise(i["metadata"]["name"]): i["metadata"]["version"]
            for i in data.get("install", [])}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--requirements", default="requirements.txt")
    ap.add_argument("--lock", default="requirements.lock")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    req_path, lock_path = Path(args.requirements), Path(args.lock)
    for p in (req_path, lock_path):
        if not p.is_file():
            print(f"ERROR: {p} not found", file=sys.stderr)
            return 2

    header = "requirements lock" + (" -- " + args.label if args.label else "")
    print("=== %s ===" % header)

    # ---- 1. The lock must actually be wired in ---------------------------------
    # Without this line the lock is a text file nobody reads, and every check below
    # would still pass: the resolution would simply happen to match a lock that is
    # not constraining anything. This is the assertion that stops the whole thing
    # being decorative.
    req_text = req_path.read_text(encoding="utf-8")
    wired = re.search(r"^\s*-c\s+%s\s*$" % re.escape(lock_path.name), req_text, re.M)
    if not wired:
        print(f"FAIL: {req_path} does not carry `-c {lock_path.name}`.", file=sys.stderr)
        print("      Without it pip never reads the lock and the pins do nothing.", file=sys.stderr)
        return 1
    print(f"  ok    {req_path} carries `-c {lock_path.name}`")

    lock = read_lock(lock_path)
    print(f"  ok    {lock_path} parses: {len(lock)} pinned package(s)")

    # ---- 2. Every direct pin must agree with the lock --------------------------
    # pip would error on a conflict anyway, but this names the offender instead of
    # leaving you to read a resolver backtrace.
    direct = read_direct_pins(req_path)
    disagreements = [(n, v, lock[n]) for n, v in direct.items() if n in lock and lock[n] != v]
    if disagreements:
        print("\nFAIL: requirements.txt and requirements.lock disagree:", file=sys.stderr)
        for n, rv, lv in disagreements:
            print(f"  - {n}: requirements.txt says {rv}, lock says {lv}", file=sys.stderr)
        return 1

    # ---- 3. THE REAL CHECK: nothing escapes the lock ---------------------------
    resolved = resolve(req_path, args.timeout)
    print(f"  ok    resolver produced {len(resolved)} package(s)")

    escaped = sorted(n for n in resolved if n not in lock)
    unused = sorted(n for n in lock if n not in resolved)

    if unused:
        # WARN, never fail. A lock generated on linux/x86_64 legitimately contains
        # packages that do not resolve on an arm64 laptop -- greenlet is exactly
        # that -- so failing here would make the check unusable locally, which is
        # where people run it first.
        print(f"\n  note  {len(unused)} locked package(s) not needed by this resolution:")
        for n in unused:
            print(f"          {n}=={lock[n]}")
        print("        Expected when running on a different platform than the lock was")
        print("        built for. If you see this in CI, the lock has dead entries.")

    if escaped:
        print("\nFAIL: %d package(s) install WITHOUT a pin:" % len(escaped), file=sys.stderr)
        for n in escaped:
            print(f"  - {n}=={resolved[n]}  (not in {lock_path.name})", file=sys.stderr)
        print("\nThese are unconstrained: the next build may install a different version", file=sys.stderr)
        print("with no change to any file here. A constraints file does not require that", file=sys.stderr)
        print("everything installed appears in it, so pip reports nothing -- which is the", file=sys.stderr)
        print("whole reason this check exists.", file=sys.stderr)
        print("\nRegenerate the lock on the platform that runs it:", file=sys.stderr)
        print("  cat ci/resolve_requirements.py | heroku run --no-tty -a <app> \\", file=sys.stderr)
        print('      "cat > /tmp/res.py && python /tmp/res.py"', file=sys.stderr)
        return 1

    print("\nOK: every one of the %d resolved package(s) is pinned in %s."
          % (len(resolved), lock_path.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
