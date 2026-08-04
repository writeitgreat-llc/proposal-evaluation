#!/usr/bin/env python3
"""
check_sticky_offsets.py -- no `position: sticky` element may hard-code the
header's height.

WHY THIS IS A SEPARATE, STATIC CHECK
====================================
ci/responsive/audit.js already checks this at runtime, and checks it better: it
measures the real header and the real element. But it can only check pages it
can reach, and it cannot reach /admin -- team login is 2FA-mandatory and a TOTP
dance in CI is a flake source we deliberately did not take on.

That gap is not theoretical. `.bulk-bar` on the admin dashboard sat at
`top: 60px` for months, 13px behind even the shortest header this app renders
and much further behind the 237px one an admin gets at 320px. The runtime audit
would never have seen it. This would.

So: the audit proves the rule holds where it can look, and this proves the rule
is *written* correctly everywhere, including where it cannot.

THE RULE
========
A rule block containing `position: sticky` may set `top` to:
  - 0 / 0px / auto                 -- pinned to the viewport top, or not pinned
  - anything mentioning --header-h -- e.g. `var(--header-h)`,
                                      `calc(var(--header-h) + 1rem)`
Anything else is a literal standing in for the header's height, and the header
does not have one height: it is 57px on a public phone page, 105px on the author
dashboard, 237px on the admin dashboard at 320px. Every literal ever written for
it has been wrong at some width.

Genuine exceptions -- a sticky table header inside its own scroll container, say
-- go in ci/sticky_offset_allowlist.txt with a reason.

Usage:
    python ci/check_sticky_offsets.py
    python ci/check_sticky_offsets.py --allowlist ci/sticky_offset_allowlist.txt

Exit codes: 0 = clean, 1 = a hard-coded offset, 2 = usage error.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Files worth scanning: the shared stylesheet, plus templates, which carry
# page-scoped CSS in {% block extra_css %}.
SCAN_GLOBS = ("static/css/*.css", "templates/*.html")

# A CSS-ish rule block. Non-greedy body, no nested braces -- enough for this
# codebase's flat CSS, and a nested block (@media) just gets scanned as its own
# inner blocks, which is what we want anyway.
BLOCK_RE = re.compile(r"\{([^{}]*)\}", re.DOTALL)
STICKY_RE = re.compile(r"position\s*:\s*sticky", re.IGNORECASE)
TOP_RE = re.compile(r"(?<![\w-])top\s*:\s*([^;}\n]+)", re.IGNORECASE)

OK_TOP_RE = re.compile(r"^(0(px|rem|em|%)?|auto)$", re.IGNORECASE)

# Comments are stripped before parsing. Not optional: the comment explaining
# *why* a rule no longer says `top: 90px` contains the string `top: 90px`, so a
# scanner that reads comments flags the very fix it is describing. Jinja
# comments go too -- templates carry their CSS inside {% block extra_css %} and
# the prose around it discusses these values constantly.
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)


def strip_comments(text: str) -> str:
    return JINJA_COMMENT_RE.sub(" ", CSS_COMMENT_RE.sub(" ", text))


def load_allowlist(path: pathlib.Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    entries = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            entries.add(line)
    return entries


def offending_blocks(text: str):
    """Yield (top_value, snippet) for sticky blocks with a hard-coded top."""
    text = strip_comments(text)
    for match in BLOCK_RE.finditer(text):
        body = match.group(1)
        if not STICKY_RE.search(body):
            continue
        top_match = TOP_RE.search(body)
        if not top_match:
            continue  # sticky with no top: not pinned to the top edge
        value = top_match.group(1).strip()
        if "--header-h" in value:
            continue
        if OK_TOP_RE.match(value):
            continue
        snippet = " ".join(body.split())[:120]
        yield value, snippet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--allowlist", default=str(REPO_ROOT / "ci" / "sticky_offset_allowlist.txt"))
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    allowlist = load_allowlist(pathlib.Path(args.allowlist))

    failures = []
    scanned = 0

    for glob in SCAN_GLOBS:
        for path in sorted(root.glob(glob)):
            scanned += 1
            rel = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                print(f"  skip {rel}: {exc}")
                continue
            for value, snippet in offending_blocks(text):
                if rel in allowlist:
                    print(f"  allowed  {rel}: top: {value}")
                    continue
                failures.append(
                    f"{rel}: `position: sticky` with `top: {value}`.\n"
                    f"      The header's height is not a constant (57px public phone, "
                    f"105px author dashboard, 237px admin at 320px).\n"
                    f"      Use `top: var(--header-h)` or "
                    f"`calc(var(--header-h) + <gap>)`.\n"
                    f"      in: {snippet}"
                )

    print(f"Scanned {scanned} file(s) for hard-coded sticky offsets.")

    if failures:
        print("\n=== HARD-CODED STICKY OFFSETS ===", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nIf one of these is genuinely pinned to something other than the page "
            "header\n(a sticky row inside its own scroll container, say), add its path to\n"
            "ci/sticky_offset_allowlist.txt with the reason.",
            file=sys.stderr,
        )
        return 1

    print("=== STICKY OFFSETS OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
