#!/usr/bin/env python3
"""
pip_audit_gate.py -- turn pip-audit output into a gate a small team can live with.

Raw `pip-audit && exit $?` is unusable here: both repos have known advisories
against their current pins right now, so a bare pip-audit fails every build on
day one and gets switched off within a week. This wrapper instead:

  * runs pip-audit and enriches every finding with a real severity from OSV
    (pip-audit's own JSON has no severity field);
  * FAILS the build on any HIGH/CRITICAL finding that is not in the baseline;
  * WARNS on everything else, and prints the whole list every run so it stays
    visible in the job summary;
  * treats the baseline (ci/vuln_baseline.txt) as a dated, justified list of
    accepted risks -- not a mute button. Entries carry an `expires:` date and
    are reported once they pass it.

Net effect: today's known advisories do not block the deploy, but the moment a
new HIGH lands -- in a direct pin or anything it drags in -- the build stops.

Usage:
    python ci/pip_audit_gate.py --requirements requirements.txt
    python ci/pip_audit_gate.py --input audit.json      # reuse an existing report
    python ci/pip_audit_gate.py --fail-on CRITICAL      # loosen
    python ci/pip_audit_gate.py --enforce-expiry        # expired baseline = failure

Exit codes: 0 = pass, 1 = un-baselined finding at/above threshold, 2 = tool error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

OSV_QUERY = "https://api.osv.dev/v1/query"

SEVERITY_ORDER = {"UNKNOWN": 0, "LOW": 1, "MODERATE": 2, "MEDIUM": 2,
                  "HIGH": 3, "CRITICAL": 4}


# --------------------------------------------------------------------------
# pip-audit
# --------------------------------------------------------------------------

def run_pip_audit(requirements: Path, extra_args):
    cmd = [sys.executable, "-m", "pip_audit", "--format", "json",
           "--progress-spinner", "off", "-r", str(requirements)]
    cmd += list(extra_args or [])
    print("$ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # pip-audit exits 1 when it finds anything; that is expected here.
    if not proc.stdout.strip():
        print(proc.stderr, file=sys.stderr)
        print("ERROR: pip-audit produced no JSON output", file=sys.stderr)
        raise SystemExit(2)
    if proc.stderr.strip():
        # surface resolution warnings, they matter
        for line in proc.stderr.strip().splitlines()[-15:]:
            print("  pip-audit: " + line)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(proc.stdout[:2000], file=sys.stderr)
        print("ERROR: could not parse pip-audit JSON: %s" % exc, file=sys.stderr)
        raise SystemExit(2)


def flatten(report):
    """[(package, version, vuln_id, [aliases], [fix_versions], description)]"""
    out = []
    for dep in report.get("dependencies", []):
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for v in dep.get("vulns", []):
            out.append({
                "package": name,
                "version": version,
                "id": v.get("id", "?"),
                "aliases": v.get("aliases", []) or [],
                "fix_versions": v.get("fix_versions", []) or [],
                "description": (v.get("description") or "").strip().replace("\n", " "),
            })
    return out


# --------------------------------------------------------------------------
# Severity enrichment via OSV
# --------------------------------------------------------------------------

def _cvss_v3_bucket(vector: str):
    """
    Very small CVSS v3.x reader: we only need a bucket, not a precise score.
    Falls back to None when the vector is not v3.
    """
    if not vector.startswith("CVSS:3"):
        return None
    parts = dict(p.split(":", 1) for p in vector.split("/")[1:] if ":" in p)
    impact = [parts.get(k, "N") for k in ("C", "I", "A")]
    highs = impact.count("H")
    network = parts.get("AV") == "N"
    no_priv = parts.get("PR") == "N"
    no_ui = parts.get("UI") == "N"
    if highs >= 2 and network and no_priv and no_ui:
        return "CRITICAL"
    if highs >= 1 and network and no_priv:
        return "HIGH"
    if highs >= 1:
        return "MEDIUM"
    return "LOW"


def osv_severity_map(package: str, version: str, timeout: float):
    """
    One OSV query per vulnerable package. Returns {vuln_id_or_alias: SEVERITY}.
    Network failures are non-fatal -- CI must not go red because OSV blinked.
    """
    body = json.dumps({"package": {"name": package, "ecosystem": "PyPI"},
                       "version": version}).encode()
    req = urllib.request.Request(OSV_QUERY, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        print("  note: OSV lookup failed for %s %s (%s)" % (package, version, exc))
        return {}

    out = {}
    for vuln in data.get("vulns", []):
        sev = (vuln.get("database_specific") or {}).get("severity")
        if not sev:
            for entry in vuln.get("severity", []) or []:
                sev = _cvss_v3_bucket(entry.get("score", ""))
                if sev:
                    break
        if not sev:
            continue
        sev = sev.upper()
        for key in [vuln.get("id")] + list(vuln.get("aliases") or []):
            if key and SEVERITY_ORDER.get(out.get(key, "UNKNOWN"), 0) < SEVERITY_ORDER.get(sev, 0):
                out[key] = sev
    return out


def enrich(findings, timeout: float, offline: bool):
    if offline:
        for f in findings:
            f["severity"] = "UNKNOWN"
        return findings
    cache = {}
    for f in findings:
        key = (f["package"].lower(), f["version"])
        if key not in cache:
            cache[key] = osv_severity_map(f["package"], f["version"], timeout)
        smap = cache[key]
        sev = smap.get(f["id"])
        if not sev:
            for alias in f["aliases"]:
                if alias in smap:
                    sev = smap[alias]
                    break
        f["severity"] = sev or "UNKNOWN"
    return findings


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

EXPIRY_RE = re.compile(r"expires:\s*(\d{4}-\d{2}-\d{2})")


def load_baseline(path: Path):
    """`VULN-ID  # reason ... expires: YYYY-MM-DD` per line."""
    entries = {}
    if not path.is_file():
        return entries
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ident, _, note = line.partition("#")
        ident = ident.strip()
        if not ident:
            continue
        m = EXPIRY_RE.search(note)
        expires = None
        if m:
            try:
                expires = dt.date.fromisoformat(m.group(1))
            except ValueError:
                pass
        entries[ident] = {"note": note.strip() or "(no reason given)",
                          "expires": expires}
    return entries


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

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
    ap.add_argument("--requirements", default="requirements.txt")
    ap.add_argument("--input", default=None, help="existing pip-audit JSON report")
    ap.add_argument("--baseline", default="ci/vuln_baseline.txt")
    ap.add_argument("--fail-on", default="HIGH",
                    choices=["LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL", "NEVER"])
    ap.add_argument("--enforce-expiry", action="store_true",
                    help="an expired baseline entry fails the build")
    ap.add_argument("--offline", action="store_true",
                    help="skip OSV severity lookup (everything becomes UNKNOWN)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--label", default="", help="name shown in the report header")
    ap.add_argument("--pip-audit-arg", action="append", default=[],
                    help="extra argument passed through to pip-audit")
    args = ap.parse_args()

    if args.input:
        report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    else:
        req = Path(args.requirements)
        if not req.is_file():
            print("ERROR: %s not found" % req, file=sys.stderr)
            return 2
        report = run_pip_audit(req, args.pip_audit_arg)

    findings = enrich(flatten(report), args.timeout, args.offline)
    baseline = load_baseline(Path(args.baseline))
    today = dt.date.today()

    threshold = SEVERITY_ORDER.get(args.fail_on, 99) if args.fail_on != "NEVER" else 99

    blocking, accepted, informational, expired = [], [], [], []
    for f in sorted(findings, key=lambda x: (-SEVERITY_ORDER.get(x["severity"], 0),
                                             x["package"], x["id"])):
        entry = baseline.get(f["id"]) or next(
            (baseline[a] for a in f["aliases"] if a in baseline), None)
        at_threshold = SEVERITY_ORDER.get(f["severity"], 0) >= threshold
        if entry:
            f["note"] = entry["note"]
            accepted.append(f)
            if entry["expires"] and entry["expires"] < today:
                f["expired_on"] = entry["expires"]
                expired.append(f)
        elif at_threshold:
            blocking.append(f)
        else:
            informational.append(f)

    header = "dependency audit" + (" -- " + args.label if args.label else "")
    print("=== %s ===" % header)
    print("findings total        : %d" % len(findings))
    print("blocking (>= %-8s): %d" % (args.fail_on, len(blocking)))
    print("accepted (baselined)  : %d  (%d past their review date)"
          % (len(accepted), len(expired)))
    print("informational         : %d" % len(informational))
    print()

    def table(rows, marker):
        for f in rows:
            fix = ", ".join(f["fix_versions"]) or "no fix released"
            print("  %-5s %-9s %-16s %-12s %-22s fix: %s"
                  % (marker, f["severity"], f["package"], f["version"], f["id"], fix))
            if f.get("description"):
                print("        %s" % f["description"][:150])
            if f.get("note"):
                print("        accepted: %s" % f["note"])

    if blocking:
        print("--- BLOCKING ---")
        table(blocking, "FAIL")
        print()
    if expired:
        print("--- baseline entries past their review date ---")
        for f in expired:
            print("  STALE %s (%s %s) accepted until %s"
                  % (f["id"], f["package"], f["version"], f["expired_on"]))
        print()
    if accepted:
        print("--- accepted risk (baselined) ---")
        table(accepted, "ACPT")
        print()
    if informational:
        print("--- below the failure threshold ---")
        table(informational, "warn")
        print()

    summary = ["### Dependency audit%s" % (" -- " + args.label if args.label else ""),
               "", "| | severity | package | version | advisory | fix |",
               "|---|---|---|---|---|---|"]
    for marker, rows in (("**BLOCK**", blocking), ("accepted", accepted),
                         ("info", informational)):
        for f in rows:
            summary.append("| %s | %s | `%s` | %s | %s | %s |"
                           % (marker, f["severity"], f["package"], f["version"],
                              f["id"], ", ".join(f["fix_versions"]) or "none"))
    if not findings:
        summary.append("| ok | - | - | - | no known advisories | - |")
    emit_summary(summary)

    if blocking:
        print("New vulnerability at or above %s. Bump the pin, or -- if there is no"
              % args.fail_on)
        print("fix and the risk is genuinely not exploitable here -- add the advisory")
        print("id to %s with a reason and an `expires:` date." % args.baseline)
        return 1

    if expired and args.enforce_expiry:
        print("Baseline entries are past their review date and --enforce-expiry is set.")
        return 1

    print("OK: no un-baselined findings at or above %s." % args.fail_on)
    return 0


if __name__ == "__main__":
    sys.exit(main())
