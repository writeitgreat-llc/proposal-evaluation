#!/usr/bin/env python3
"""
check_no_secrets.py -- cheap, fast, zero-dependency secret gate.

gitleaks (run separately in the workflow) is the thorough scanner. This script
is the fast pre-filter that runs in a couple of seconds, has no network or
container dependency, and encodes the two mistakes that actually happen on this
team: committing a real .env, and pasting a live API key into source.

It scans the files git tracks (or, with --files, just the ones a PR touched).

Blocking rules
  ENV_FILE        a real .env / .env.local / .env.production is committed
  OPENAI_KEY      sk-... / sk-proj-... OpenAI-style key
  AWS_KEY         AKIA... access key id
  PRIVATE_KEY     -----BEGIN ... PRIVATE KEY-----
  DB_URL_CREDS    a postgres/mysql/mongo/redis DSN with an inline password
  STRIPE_KEY      sk_live_ / rk_live_
  SLACK_TOKEN     xoxb-/xoxp-/xoxa-/xoxr-/xoxs-
  GOOGLE_KEY      AIza...
  GITHUB_TOKEN    ghp_/gho_/ghu_/ghs_/ghr_
  SENDGRID_KEY    SG.xxxx.xxxx
  PRIVATE_KEY_PEM id_rsa / id_ed25519 style key files

Warning rules (reported, do not fail the build)
  WEAK_DEFAULT    os.environ.get("SECRET_KEY", "some-literal") -- a hardcoded
                  fallback signing key. Harmless in dev; if the env var is ever
                  missing in production, anyone can forge a session cookie.

Example / template env files (.env.example, .env.sample, .env.template,
.env.dist) are expected to exist and are exempt from the ENV_FILE rule and from
DB_URL_CREDS -- but a real-looking API key inside one is still blocked, because
placeholders never look like `sk-live...`.

A line containing `# ci-allow-secret` is skipped. Persistent exceptions go in
ci/secret_allowlist.txt as `path::RULE` (or `path::*`).

Usage:
    python ci/check_no_secrets.py
    python ci/check_no_secrets.py --files a.py b.py        # PR-diff mode
    python ci/check_no_secrets.py --root . --allowlist ci/secret_allowlist.txt

Exit codes: 0 = clean, 1 = secret found, 2 = usage error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

INLINE_ALLOW = "ci-allow-secret"

# Example/template env files are legitimate.
ENV_EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".defaults")

BLOCKING_RULES = [
    ("OPENAI_KEY",
     re.compile(r"(?<![A-Za-z0-9_/-])sk-(proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
     "OpenAI-style API key"),
    ("AWS_KEY",
     re.compile(r"(?<![A-Z0-9])(AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])"),
     "AWS access key id"),
    ("PRIVATE_KEY",
     re.compile(r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
     "private key block"),
    ("DB_URL_CREDS",
     re.compile(r"(postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis)://"
                r"[A-Za-z0-9_.-]+:[^\s@'\"$}{]{6,}@"),
     "database URL with embedded credentials"),
    ("STRIPE_KEY",
     re.compile(r"(?<![A-Za-z0-9_])(sk|rk)_live_[A-Za-z0-9]{16,}"),
     "live Stripe secret key"),
    ("SLACK_TOKEN",
     re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
     "Slack token"),
    ("GOOGLE_KEY",
     re.compile(r"(?<![A-Za-z0-9_])AIza[0-9A-Za-z_-]{30,}"),
     "Google API key"),
    ("GITHUB_TOKEN",
     re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{36}"),
     "GitHub token"),
    ("SENDGRID_KEY",
     re.compile(r"(?<![A-Za-z0-9_])SG\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
     "SendGrid API key"),
    ("CLOUDINARY_URL",
     re.compile(r"cloudinary://\d{6,}:[A-Za-z0-9_-]{10,}@"),
     "Cloudinary URL with API secret"),
]

# Rules that still apply inside .env.example and friends: a placeholder never
# looks like a real key, so a hit here is a genuine leak.
RULES_ENFORCED_IN_EXAMPLES = {
    "OPENAI_KEY", "AWS_KEY", "PRIVATE_KEY", "STRIPE_KEY",
    "SLACK_TOKEN", "GOOGLE_KEY", "GITHUB_TOKEN", "SENDGRID_KEY", "CLOUDINARY_URL",
}

WARNING_RULES = [
    ("WEAK_DEFAULT",
     re.compile(r"(?:environ\.get|getenv)\(\s*['\"](SECRET_KEY|FLASK_SECRET|"
                r"JWT_SECRET|ADMIN_PASSWORD)['\"]\s*,\s*['\"][^'\"]{4,}['\"]"),
     "hardcoded fallback for a security-critical setting"),
]

# Files that are never worth scanning.
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip",
    ".gz", ".tgz", ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp3", ".mp4",
    ".webm", ".wav", ".docx", ".xlsx", ".pyc", ".so", ".dylib",
}
SKIP_DIR_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "site-packages", "dist", "build"}

MAX_SCAN_BYTES = 2 * 1024 * 1024  # skip anything bigger; keys are not in 2MB blobs

# This file defines the patterns, so it necessarily contains strings that look
# like secrets. Every scanner excludes its own rule definitions; without this
# the check reports itself and fails on a clean tree.
SELF_PATH = Path(__file__).resolve()


def is_env_file(rel_path: str) -> bool:
    name = Path(rel_path).name
    if not name.startswith(".env"):
        return False
    if name == ".env":
        return True
    # .env.local / .env.production / .env.staging are real; .env.example is not
    return not name.endswith(ENV_EXAMPLE_SUFFIXES)


def is_env_example(rel_path: str) -> bool:
    name = Path(rel_path).name
    return name.startswith(".env") and name.endswith(ENV_EXAMPLE_SUFFIXES)


def tracked_files(root: Path):
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def should_scan(rel_path: str) -> bool:
    p = Path(rel_path)
    if SKIP_DIR_PARTS & set(p.parts):
        return False
    if p.suffix.lower() in SKIP_SUFFIXES:
        return False
    return True


def load_allowlist(path: Path):
    """`path::RULE` or `path::*` per line."""
    allow = set()
    if not path.is_file():
        return allow
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            allow.add(line)
    return allow


def allowed(allow, rel_path, rule_id):
    return f"{rel_path}::{rule_id}" in allow or f"{rel_path}::*" in allow


def redact(match_text: str) -> str:
    """Never echo a live secret into CI logs."""
    if len(match_text) <= 8:
        return match_text[:2] + "***"
    return match_text[:6] + "*" * 8 + match_text[-2:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--files", nargs="*", default=None,
                    help="scan only these paths (PR-diff mode) instead of all tracked files")
    ap.add_argument("--allowlist", default="ci/secret_allowlist.txt")
    ap.add_argument("--warn-only", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    allow = load_allowlist(Path(args.allowlist))

    files = args.files if args.files is not None else tracked_files(root)
    files = [f for f in files if should_scan(f)]

    errors = []
    warnings = []
    scanned = 0

    for rel in sorted(files):
        abs_path = root / rel
        if not abs_path.is_file():
            continue  # deleted in this PR
        if abs_path.resolve() == SELF_PATH:
            continue  # never scan the rule definitions themselves

        # Rule ENV_FILE works on the name alone.
        if is_env_file(rel) and not allowed(allow, rel, "ENV_FILE"):
            errors.append((rel, 0, "ENV_FILE",
                           "environment file is committed",
                           "(file name)"))
            continue

        try:
            if abs_path.stat().st_size > MAX_SCAN_BYTES:
                continue
            text = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        scanned += 1

        example = is_env_example(rel)

        for lineno, line in enumerate(text.splitlines(), 1):
            if INLINE_ALLOW in line:
                continue
            for rule_id, rx, desc in BLOCKING_RULES:
                if example and rule_id not in RULES_ENFORCED_IN_EXAMPLES:
                    continue
                m = rx.search(line)
                if m and not allowed(allow, rel, rule_id):
                    errors.append((rel, lineno, rule_id, desc, redact(m.group(0))))
            for rule_id, rx, desc in WARNING_RULES:
                m = rx.search(line)
                if m and not allowed(allow, rel, rule_id):
                    warnings.append((rel, lineno, rule_id, desc, line.strip()[:100]))

    print("=== secret scan ===")
    print("files scanned : %d" % scanned)
    print("blocking hits : %d" % len(errors))
    print("warnings      : %d" % len(warnings))
    print()

    if warnings:
        print("--- warnings (not blocking) ---")
        for rel, lineno, rule_id, desc, ctx in warnings:
            print("  WARN  %s:%d  [%s] %s" % (rel, lineno, rule_id, desc))
            print("        %s" % ctx)
        print()

    if errors:
        print("--- SECRETS DETECTED ---")
        for rel, lineno, rule_id, desc, sample in errors:
            loc = "%s:%d" % (rel, lineno) if lineno else rel
            print("  FAIL  %s  [%s] %s" % (loc, rule_id, desc))
            print("        match: %s" % sample)
        print()
        print("If a real credential was committed, rotate it FIRST -- removing the")
        print("commit does not un-leak it. Then purge it from history.")
        print("False positive? Add `# %s` on the line, or add" % INLINE_ALLOW)
        print("`<path>::<RULE>` to %s" % args.allowlist)
        if args.warn_only:
            print("\n(--warn-only: not failing the build)")
            return 0
        return 1

    print("OK: no secrets found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
