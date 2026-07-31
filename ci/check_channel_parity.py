#!/usr/bin/env python3
"""
check_channel_parity.py -- the acquisition-channel rules are the SAME rules on
both sites.

SHARED ARTIFACT. This script is byte-identical in writeitgreat-llc/website and
writeitgreat-llc/proposal-evaluation, alongside the two files it checks:

    rules     website app/analytics_channel_rules.py
              proposal-evaluation analytics_channel_rules.py
    fixture   ci/analytics_channel_fixture.json   (both repos, same path)

It figures out which repo it is running in rather than being configured, which
is what lets all three files be copied across without editing.

WHY THIS EXISTS
---------------
Both sites answer "where did this visitor come from" and both answers land in
the same wig-dashboard tables, so "which channel brings traffic" and "which
channel brings leads" are only comparable if the two apps agree. They used to
agree by prose: a comment saying "verbatim port -- change one, change both."
That rule was followed in good faith and still produced a matcher that filed
every referral from writeitgreat.com under social, because the port was done by
hand. This is the mechanical version of that comment.

WHAT IT GUARANTEES, HONESTLY
----------------------------
The repos are separate -- one public, one private -- with no shared package and
no network in CI. So NOTHING here can turn this repo red because the OTHER repo
changed. What it does buy:

  * Touching the rules in either repo turns THAT repo red until the rules
    module, the fixture and the digest move together. That red is the reminder
    to port, at the moment of the edit rather than a month later.
  * The port is a copy of whole files, so `diff` audits it and nobody
    re-transcribes a domain list by hand again.
  * 78 golden vectors, which on the proposal-evaluation side is the first real
    coverage this function has ever had.

The only cross-repo detector with teeth is the runtime one: both collectors
report `rules_version` on the analytics ingest envelope, so wig-dashboard sees
the two sites disagreeing within minutes of a one-sided deploy. This script
cannot replace it and does not try to.

Usage:
    python ci/check_channel_parity.py          # from the repo root
Exit codes: 0 = fine, 1 = a check failed.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

# (path to the vendored rules module, its importable name, the repo it means,
# the app wrapper that must delegate to it). One layout exists in each repo;
# whichever is found on disk identifies the repo, which is what lets this file
# be copied across without editing.
LAYOUTS = (
    ("app/analytics_channel_rules.py", "app.analytics_channel_rules",
     "website", "app.source_capture"),
    ("analytics_channel_rules.py", "analytics_channel_rules",
     "proposal-evaluation", "analytics_collect"),
)
FIXTURE = REPO_ROOT / "ci" / "analytics_channel_fixture.json"

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  ok    {label}")
    else:
        failures.append(f"{label}{' -- ' + detail if detail else ''}")
        print(f"  FAIL  {label}{' -- ' + detail if detail else ''}")


def main() -> int:
    rules_path = None
    rules_name = repo = wrapper_name = None
    for rel, mod, name, wrapper in LAYOUTS:
        candidate = REPO_ROOT / rel
        if candidate.exists():
            rules_path, rules_name, repo, wrapper_name = candidate, mod, name, wrapper
            break

    if rules_path is None:
        print("FAIL  no vendored rules module found. Expected one of:", file=sys.stderr)
        for rel, _mod, name, _w in LAYOUTS:
            print(f"        {rel}   ({name})", file=sys.stderr)
        return 1
    if not FIXTURE.exists():
        print(f"FAIL  {FIXTURE} is missing.", file=sys.stderr)
        return 1

    print(f"Repo: {repo}")
    print(f"Rules: {rules_path.relative_to(REPO_ROOT)}")

    # Imported by its real name, not loaded off the path: the delegation checks
    # below compare module attributes by IDENTITY, and a path-load would build a
    # second, distinct module object that could never be `is` anything the app
    # imported.
    rules = importlib.import_module(rules_name)
    fixture = json.loads(FIXTURE.read_text())

    # ----------------------------------------------------------------------
    print("\nThe fixture pins THIS rules module")
    digest = hashlib.sha256(rules_path.read_bytes()).hexdigest()
    declared = fixture.get("rules_sha256")
    matched = digest == declared
    check("rules module digest matches the fixture", matched,
          f"computed {digest}, fixture says {declared}")
    if not matched:
        print("\n  The rules module changed. That is a SHARED file, so this is the")
        print("  moment to port it. Do all of this, in both repos:")
        print(f"    1. copy the whole {rules_path.name} across")
        print("    2. copy the whole ci/analytics_channel_fixture.json across")
        print(f'    3. set "rules_sha256": "{digest}" in both fixtures')
        print("    4. bump rules_version and add a vector proving what changed\n")

    check("fixture rules_version matches the module's RULES_VERSION",
          fixture.get("rules_version") == rules.RULES_VERSION,
          f"fixture {fixture.get('rules_version')!r}, module {rules.RULES_VERSION!r}")
    check("fixture channel vocabulary matches the module's CHANNELS",
          list(fixture.get("channels", [])) == list(rules.CHANNELS),
          f"fixture {fixture.get('channels')}, module {list(rules.CHANNELS)}")

    # ----------------------------------------------------------------------
    # The wrapper must DELEGATE, not carry its own copy. Identity, not equality:
    # a second definition that happens to agree today is exactly how the two
    # repos drifted in the first place.
    print("\nThe app delegates to the shared module rather than redefining it")
    try:
        wrapper = importlib.import_module(wrapper_name)
    except Exception as exc:  # pragma: no cover - reported, not raised
        check(f"{wrapper_name} imports", False, f"{type(exc).__name__}: {exc}")
        wrapper = None
    if wrapper is not None:
        check(f"{wrapper_name}.classify IS the shared classify",
              getattr(wrapper, "classify", None) is rules.classify,
              "the app defines its own classify instead of importing the shared one")
        for name in ("CHANNELS", "CAMPAIGN_KEYS"):
            if hasattr(wrapper, name):
                check(f"{wrapper_name}.{name} IS the shared {name}",
                      getattr(wrapper, name) is getattr(rules, name))
        if hasattr(wrapper, "channel_for"):
            check(f"{wrapper_name}.channel_for agrees with the shared classify",
                  wrapper.channel_for("https://t.co/x", {}) == "social"
                  and wrapper.channel_for("https://writeitgreat.com/x", {}) == "referral")

    # ----------------------------------------------------------------------
    print("\nGolden vectors -- referrer URLs")
    for vector in fixture["vectors"]:
        referrer, expect = vector["referrer"], vector["expect"]
        got = rules.classify({"referrer": referrer})
        check(f"{referrer[:52]:52} -> {expect}", got == expect, f"got {got!r}")

    print("\nGolden vectors -- campaign tags with no referrer")
    for vector in fixture["utm_vectors"]:
        source, expect = dict(vector["source"]), vector["expect"]
        got = rules.classify(dict(source))
        check(f"{json.dumps(source, sort_keys=True)[:52]:52} -> {expect}",
              got == expect, f"got {got!r}")

    # Every table stores referrer_host, never the referrer, so a backfill only
    # ever has a bare host. Handing that to classify() returns "direct" for
    # everything and fails silently -- these vectors are what stops a backfill
    # being written the obvious, wrong way.
    print("\nGolden vectors -- stored hosts, the backfill path")
    for vector in fixture["host_vectors"]:
        host, expect = vector["referrer_host"], vector["expect"]
        got = rules.classify_stored_host(host)
        check(f"{host[:52]:52} -> {expect}", got == expect, f"got {got!r}")

    check("classify_stored_host on a bare host does NOT collapse to 'direct'",
          rules.classify_stored_host("linkedin.com") == "social",
          "urlparse('linkedin.com').hostname is None -- the scheme must be synthesised")
    check("classify() on a bare host DOES collapse, which is why the helper exists",
          rules.classify({"referrer": "linkedin.com"}) == "direct")

    # ----------------------------------------------------------------------
    print(f"\n{checks - len(failures)}/{checks} checks passed.")
    if failures:
        print("\n=== CHANNEL PARITY CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"OK: {repo} classifies exactly as the shared rules "
          f"({rules.RULES_VERSION}) say it must.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
