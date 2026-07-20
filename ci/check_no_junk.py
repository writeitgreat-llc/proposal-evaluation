#!/usr/bin/env python3
"""
check_no_junk.py -- keep build artefacts, local databases and giant binaries
out of git.

Two independent checks:

  1. JUNK PATTERNS -- applied to every tracked file in the repo.
     *.pyc, *.pyo, __pycache__/, *.db / *.sqlite / *.sqlite3, .env,
     .DS_Store, *.log, instance/, node_modules/, *.pem / *.key.
     Both repos are clean today, so this is a hard gate from day one.

  2. FILE SIZE -- applied ONLY to files added or modified by the pull request.
     This is deliberate. writeitgreat-website already tracks
     _brief/mockup_rendered.png at ~16 MB; checking the whole tree would fail
     every single build for a file nobody is touching. Scoping to the diff
     means the existing blob is grandfathered in while a new 20 MB PNG is
     stopped at the door. Files that legitimately need to be large go in
     ci/large_files_allowed.txt.

Diff base resolution, in order:
  --base REF                     explicit (the workflow passes the PR base SHA)
  $GITHUB_BASE_REF               pull_request events
  $GITHUB_EVENT_BEFORE / HEAD^   push events
  none                           size check is skipped with a notice, never
                                 failed -- a check that cannot run must not
                                 block a deploy

Usage:
    python ci/check_no_junk.py
    python ci/check_no_junk.py --base origin/main --max-mb 5
    python ci/check_no_junk.py --all-files          # size-check the whole tree

Exit codes: 0 = clean, 1 = junk or oversized file, 2 = usage error.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_MAX_MB = 5.0

JUNK_RULES = [
    (re.compile(r"(^|/)__pycache__/"),            "compiled-Python cache directory"),
    (re.compile(r"\.py[co]$"),                    "compiled Python file"),
    (re.compile(r"\.(db|sqlite|sqlite3)$"),       "local database file"),
    (re.compile(r"(^|/)instance/"),               "Flask instance folder (local config/db)"),
    (re.compile(r"(^|/)\.env$"),                  "environment file"),
    (re.compile(r"(^|/)\.env\.(local|prod|production|staging|dev|development)$"),
                                                  "environment file"),
    (re.compile(r"(^|/)\.DS_Store$"),             "macOS Finder metadata"),
    (re.compile(r"(^|/)node_modules/"),           "vendored node_modules"),
    (re.compile(r"\.log$"),                       "log file"),
    (re.compile(r"\.(pem|key|p12|pfx|keystore)$"),"key material"),
    (re.compile(r"(^|/)\.coverage$|(^|/)\.pytest_cache/"), "test run artefact"),
    (re.compile(r"\.(egg-info)/"),                "packaging artefact"),
]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def tracked_files(root: Path):
    r = run(["git", "-C", str(root), "ls-files", "-z"])
    if r.returncode != 0:
        print("ERROR: not a git repository: %s" % root, file=sys.stderr)
        raise SystemExit(2)
    return [p for p in r.stdout.split("\0") if p]


def resolve_base(root: Path, explicit):
    """Return a ref to diff against, or None."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    base_ref = os.environ.get("GITHUB_BASE_REF")
    if base_ref:
        candidates += ["origin/" + base_ref, base_ref]
    before = os.environ.get("GITHUB_EVENT_BEFORE") or os.environ.get("EVENT_BEFORE")
    if before and set(before) != {"0"}:
        candidates.append(before)
    candidates.append("HEAD^")

    for ref in candidates:
        if run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
                ref + "^{commit}"]).returncode == 0:
            return ref
    return None


def changed_files(root: Path, base):
    """Files added or modified between base and HEAD."""
    r = run(["git", "-C", str(root), "diff", "--name-only", "-z",
             "--diff-filter=AM", base + "...HEAD"])
    if r.returncode != 0:
        # base and HEAD may have no merge base (shallow clone); fall back to
        # a two-dot diff before giving up.
        r = run(["git", "-C", str(root), "diff", "--name-only", "-z",
                 "--diff-filter=AM", base, "HEAD"])
    if r.returncode != 0:
        return None
    return [p for p in r.stdout.split("\0") if p]


def load_allowlist(path: Path):
    allow = {}
    if not path.is_file():
        return allow
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p, _, reason = line.partition("#")
        p = p.strip()
        if p:
            allow[p] = reason.strip() or "(no reason given)"
    return allow


def human(nbytes):
    return "%.1f MB" % (nbytes / 1024.0 / 1024.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", default=None, help="git ref to diff against")
    ap.add_argument("--max-mb", type=float, default=DEFAULT_MAX_MB)
    ap.add_argument("--all-files", action="store_true",
                    help="size-check every tracked file, not just the diff")
    ap.add_argument("--allowlist", default="ci/large_files_allowed.txt")
    ap.add_argument("--warn-only", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    limit = int(args.max_mb * 1024 * 1024)
    allow = load_allowlist(Path(args.allowlist))
    tracked = tracked_files(root)

    print("=== junk / bloat gate ===")
    print("tracked files : %d" % len(tracked))
    print("size limit    : %.1f MB" % args.max_mb)

    # --- 1. junk patterns, whole tree ---------------------------------------
    junk = []
    for rel in tracked:
        for rx, desc in JUNK_RULES:
            if rx.search(rel):
                junk.append((rel, desc))
                break

    # --- 2. size, diff-scoped ----------------------------------------------
    if args.all_files:
        size_scope = tracked
        scope_label = "every tracked file (--all-files)"
    else:
        base = resolve_base(root, args.base)
        if base is None:
            size_scope = None
            scope_label = "SKIPPED (no diff base available)"
        else:
            size_scope = changed_files(root, base)
            if size_scope is None:
                scope_label = "SKIPPED (could not compute diff against %s)" % base
            else:
                scope_label = "%d file(s) added/modified vs %s" % (len(size_scope), base)
    print("size scope    : %s" % scope_label)
    print()

    oversized = []
    if size_scope:
        for rel in size_scope:
            p = root / rel
            if not p.is_file():
                continue
            size = p.stat().st_size
            if size > limit:
                oversized.append((rel, size, rel in allow))

    grandfathered = [(r, s) for r, s, ok in oversized if ok]
    oversized = [(r, s) for r, s, ok in oversized if not ok]

    if grandfathered:
        print("--- large files, explicitly allowed ---")
        for rel, size in grandfathered:
            print("  ALLOW %s (%s)  reason: %s" % (rel, human(size), allow[rel]))
        print()

    failed = False

    if junk:
        print("--- JUNK FILES TRACKED BY GIT ---")
        for rel, desc in junk:
            print("  FAIL  %s  -- %s" % (rel, desc))
        print()
        print("Remove with:  git rm --cached <path>   then add it to .gitignore.")
        print()
        failed = True

    if oversized:
        print("--- OVERSIZED FILES ADDED/MODIFIED BY THIS CHANGE ---")
        for rel, size in oversized:
            print("  FAIL  %s  (%s, limit %.1f MB)" % (rel, human(size), args.max_mb))
        print()
        print("Large binaries make every future clone slower, forever, and git")
        print("never forgets them. Put images through an optimiser, host them on")
        print("Cloudinary, or -- if it genuinely has to live in the repo -- add the")
        print("path to %s with a reason." % args.allowlist)
        print()
        failed = True

    if failed:
        if args.warn_only:
            print("(--warn-only: not failing the build)")
            return 0
        return 1

    print("OK: no junk files, nothing oversized in this change.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
