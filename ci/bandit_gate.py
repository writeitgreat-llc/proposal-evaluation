#!/usr/bin/env python3
"""
bandit_gate.py -- make bandit usable on a 7,900-line single-file Flask app.

Plain `bandit -r .` on proposal-evaluation reports 20 issues, 13 of which are
`try/except/pass`. Failing on that count teaches everyone to ignore the job.
This wrapper gates on what actually matters and keeps the rest visible:

  * B101 (assert) is skipped -- there are no asserts used as guards here.
  * Findings below the severity/confidence floor are printed, never fatal.
  * At and above the floor, the gate compares the count per bandit test id
    against ci/bandit_allowance.json.

Counting per test id, rather than diffing whole findings, is deliberate:
app.py moves constantly, so anything keyed on line numbers goes stale in a
week and starts crying wolf. A brand-new HIGH test id, or one more instance of
an existing one, fails the build; refactoring that shifts every line does not.

Usage:
    python ci/bandit_gate.py --paths app.py
    python ci/bandit_gate.py --paths app config.py run.py --allowance ci/bandit_allowance.json
    python ci/bandit_gate.py --paths app.py --min-severity MEDIUM   # tighten
    python ci/bandit_gate.py --paths app.py --warn-only

Exit codes: 0 = pass, 1 = new/extra finding at or above the floor, 2 = tool error.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

# Skipped globally. B101 = assert_used: bandit flags every `assert`, and none of
# these are security checks that -O would strip.
DEFAULT_SKIPS = ["B101"]


def run_bandit(paths, skips, extra):
    cmd = [sys.executable, "-m", "bandit", "-q", "-r", "-f", "json"]
    if skips:
        cmd += ["--skip", ",".join(skips)]
    cmd += list(extra or [])
    cmd += list(paths)
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if not proc.stdout.strip():
        print(proc.stderr[-4000:], file=sys.stderr)
        print("ERROR: bandit produced no JSON output", file=sys.stderr)
        raise SystemExit(2)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(proc.stdout[:2000], file=sys.stderr)
        print("ERROR: could not parse bandit JSON: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


def emit_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--allowance", default="ci/bandit_allowance.json")
    ap.add_argument("--input", default=None, help="existing bandit JSON report")
    ap.add_argument("--min-severity", default="HIGH", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--min-confidence", default="MEDIUM", choices=["LOW", "MEDIUM", "HIGH"])
    ap.add_argument("--skip", default=",".join(DEFAULT_SKIPS),
                    help="comma-separated bandit test ids to skip entirely")
    ap.add_argument("--exclude", default=None,
                    help="comma-separated paths passed to bandit -x")
    ap.add_argument("--warn-only", action="store_true")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    skips = [s.strip() for s in args.skip.split(",") if s.strip()]
    extra = ["-x", args.exclude] if args.exclude else []

    if args.input:
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        report = run_bandit(args.paths, skips, extra)

    sev_floor = RANK[args.min_severity]
    conf_floor = RANK[args.min_confidence]

    results = report.get("results", [])
    gated, below = [], []
    for r in results:
        s = RANK.get(r.get("issue_severity", "LOW").upper(), 1)
        c = RANK.get(r.get("issue_confidence", "LOW").upper(), 1)
        (gated if (s >= sev_floor and c >= conf_floor) else below).append(r)

    allowance_path = Path(args.allowance)
    allowance = {}
    reasons = {}
    if allowance_path.is_file():
        raw = json.loads(allowance_path.read_text(encoding="utf-8"))
        for test_id, value in raw.items():
            if test_id.startswith("_"):
                continue  # "_comment" keys
            if isinstance(value, dict):
                allowance[test_id] = int(value.get("max", 0))
                reasons[test_id] = value.get("reason", "")
            else:
                allowance[test_id] = int(value)

    counts = Counter(r["test_id"] for r in gated)

    header = "bandit" + (" -- " + args.label if args.label else "")
    print("=== %s ===" % header)
    print("floor           : severity >= %s and confidence >= %s"
          % (args.min_severity, args.min_confidence))
    print("skipped tests   : %s" % (", ".join(skips) or "none"))
    print("findings total  : %d  (%d at/above floor, %d below)"
          % (len(results), len(gated), len(below)))
    print()

    over = []
    for test_id, n in sorted(counts.items()):
        limit = allowance.get(test_id, 0)
        if n > limit:
            over.append((test_id, n, limit))

    if gated:
        print("--- at/above the floor ---")
        for r in sorted(gated, key=lambda x: (x["test_id"], x["filename"], x["line_number"])):
            limit = allowance.get(r["test_id"], 0)
            marker = "KNOWN" if limit else "NEW  "
            print("  %s %s/%s %s  %s:%s  %s"
                  % (marker, r["issue_severity"], r["issue_confidence"], r["test_id"],
                     r["filename"], r["line_number"], r["issue_text"]))
            snippet = (r.get("code") or "").strip().splitlines()
            if snippet:
                print("        %s" % snippet[0][:140])
            if limit and reasons.get(r["test_id"]):
                print("        allowed because: %s" % reasons[r["test_id"]])
        print()

    if below:
        print("--- below the floor (informational) ---")
        by_test = Counter("%s %s/%s" % (r["test_id"], r["issue_severity"],
                                        r["issue_confidence"]) for r in below)
        for key, n in sorted(by_test.items()):
            print("  %3d x %s" % (n, key))
        print()

    summary = ["### Bandit%s" % (" -- " + args.label if args.label else ""), "",
               "| test | severity/confidence | count | allowed |",
               "|---|---|---|---|"]
    for test_id, n in sorted(counts.items()):
        example = next(r for r in gated if r["test_id"] == test_id)
        summary.append("| %s | %s/%s | %d | %d |"
                       % (test_id, example["issue_severity"],
                          example["issue_confidence"], n, allowance.get(test_id, 0)))
    if not counts:
        summary.append("| - | - | 0 | - |")
    emit_summary(summary)

    if over:
        print("--- BANDIT GATE FAILED ---")
        for test_id, n, limit in over:
            print("  %s: %d finding(s), allowance %d" % (test_id, n, limit))
        print()
        print("Either fix the finding, add `# nosec BXXX -- <why>` on the line if it")
        print("is genuinely safe, or raise the allowance in %s with a reason."
              % args.allowance)
        if args.warn_only:
            print("\n(--warn-only: not failing the build)")
            return 0
        return 1

    stale = [t for t, limit in allowance.items() if limit > counts.get(t, 0)]
    if stale:
        print("note: allowance is now larger than reality for %s -- tighten it."
              % ", ".join(sorted(stale)))

    print("OK: nothing new at or above %s/%s." % (args.min_severity, args.min_confidence))
    return 0


if __name__ == "__main__":
    sys.exit(main())
