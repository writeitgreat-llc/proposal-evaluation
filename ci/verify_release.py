#!/usr/bin/env python3
"""verify_release.py -- prove the deploy actually deployed something.

On 4 August 2026 the deploy of dab33bd reported SUCCESS in GitHub Actions
twice while production stayed on the previous release both times. main and
production diverged silently for about forty minutes, and the only signal
either time was a Heroku "Release phase command failed" email. CLAUDE.md
records that a GitHub/production split is how this project got burned before.

There are two ways the pipeline goes green having deployed nothing, and the
existing post-deploy smoke check cannot see either:

  1. THE RELEASE PHASE FAILS. `git push heroku` returns 0 as soon as the slug
     BUILDS; the release command (python migrate.py) runs afterwards and can
     fail independently. Heroku then discards the release and keeps the old
     dynos serving -- correct behaviour, and exactly what migrate.py's
     strict=True is for. ci/smoke_deploy.sh then gets a healthy 200 from the
     OLD release and prints "=== SMOKE OK ===".

  2. THE PUSH IS A NO-OP. Re-pushing a commit Heroku already has prints
     "Everything up-to-date", builds nothing, runs no release phase, and exits
     0 -- followed by the same green smoke check. This is what happens to
     anyone using workflow_dispatch to re-deploy current main after a
     Heroku-side problem, which is the case deploy.yml's header explicitly
     documents that input for.

Both are invisible to "does the site return 200", because in both cases the
site is serving perfectly -- just not the code you pushed.

WHAT THIS ASSERTS, and why it takes two sources rather than one:

  * From the HEROKU API: a release NEWER than the one before the push exists,
    and its status is `succeeded` rather than `failed` or `pending`. This is
    what catches both modes above. A no-op push creates no release at all, so
    the version never moves; a failed release phase creates one whose status
    is `failed`.

  * From /healthz ON THE APP: the running web dyno reports that same version.
    The API can only tell you Heroku considers the release good. It cannot
    tell you the dynos actually picked it up -- and a release that succeeded
    while the web dyno stayed on the old slug is precisely the divergence this
    file exists to catch. HEROKU_RELEASE_VERSION is a dyno environment
    variable, so /healthz reporting vN is first-hand evidence from the process
    serving requests.

A config-var change also bumps the release version, which is why "advanced" is
necessary but not sufficient and is always paired with the status check.

  usage:
    ci/verify_release.py --capture --app <name>
    ci/verify_release.py --verify  --app <name> --before <vN> --healthz <url>

  env: HEROKU_API_KEY (required)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HEROKU_API = "https://api.heroku.com"
ACCEPT = "application/vnd.heroku+json; version=3"


def _api(path: str):
    key = os.environ.get("HEROKU_API_KEY")
    if not key:
        print("ERROR: HEROKU_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)
    req = urllib.request.Request(
        HEROKU_API + path,
        headers={"Accept": ACCEPT, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def current_release(app: str):
    """The most recent release, or None if the app has none."""
    # range header: newest first, one row.
    key = os.environ.get("HEROKU_API_KEY")
    req = urllib.request.Request(
        f"{HEROKU_API}/apps/{app}/releases",
        headers={
            "Accept": ACCEPT,
            "Authorization": f"Bearer {key}",
            "Range": "version ..; order=desc, max=1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 206:  # partial content is the normal paged response
            rows = json.loads(exc.read().decode())
        else:
            raise
    return rows[0] if rows else None


def _version_int(v) -> int:
    """'v220' or 220 -> 220. Returns -1 for anything unusable."""
    if v is None:
        return -1
    s = str(v).strip().lstrip("vV")
    return int(s) if s.isdigit() else -1


def healthz_release(url: str):
    """The release version the RUNNING dyno reports, or None."""
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            return json.loads(resp.read().decode()).get("release")
    except Exception as exc:  # noqa: BLE001 -- any failure means "cannot tell"
        print(f"  /healthz not readable yet: {exc}")
        return None


def cmd_capture(app: str) -> int:
    rel = current_release(app)
    print(f"v{rel['version']}" if rel else "none")
    return 0


def cmd_verify(app: str, before: str, healthz: str,
               attempts: int = 12, delay: int = 10) -> int:
    before_n = _version_int(before)
    print(f"Release before push: {before or 'unknown'}")

    rel = None
    for attempt in range(1, attempts + 1):
        rel = current_release(app)
        if rel is None:
            print(f"  attempt {attempt}: app reports no releases at all")
        else:
            status = rel.get("status")
            print(f"  attempt {attempt}: v{rel['version']} status={status}")
            # A release sits at `pending` while the release phase runs. Waiting
            # is the whole point -- calling it good here would reintroduce the
            # bug, since the phase that fails is the one still running.
            if _version_int(rel["version"]) > before_n and status != "pending":
                break
        if attempt < attempts:
            time.sleep(delay)

    if rel is None or _version_int(rel["version"]) <= before_n:
        print("", file=sys.stderr)
        print("=== DEPLOY VERIFY FAILED: nothing was deployed ===", file=sys.stderr)
        print(f"The release version did not advance past {before or 'unknown'}.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("Almost always this means the push was a no-op -- Heroku already had",
              file=sys.stderr)
        print("this commit, so it printed 'Everything up-to-date', built nothing and",
              file=sys.stderr)
        print("ran no release phase. The job would previously have gone green here.",
              file=sys.stderr)
        print("", file=sys.stderr)
        print("To genuinely re-run a release against the same code:", file=sys.stderr)
        print(f"  heroku releases:retry --app {app}", file=sys.stderr)
        print("  # or push an empty commit, as PR #142 did", file=sys.stderr)
        return 1

    if rel.get("status") != "succeeded":
        print("", file=sys.stderr)
        print("=== DEPLOY VERIFY FAILED: the release did not succeed ===", file=sys.stderr)
        print(f"v{rel['version']} status={rel.get('status')}", file=sys.stderr)
        print("", file=sys.stderr)
        print("The slug built, so the push exited 0, but the release phase", file=sys.stderr)
        print("(python migrate.py) failed. Heroku has DISCARDED this release and is", file=sys.stderr)
        print("still serving the previous one -- which is why the smoke check passed.", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"  heroku releases:output v{rel['version']} --app {app}", file=sys.stderr)
        return 1

    deployed = f"v{rel['version']}"
    print(f"Heroku reports {deployed} succeeded.")

    # Second source: the process actually serving requests.
    for attempt in range(1, attempts + 1):
        seen = healthz_release(healthz)
        print(f"  attempt {attempt}: /healthz reports {seen or 'null'}")
        if seen and _version_int(seen) >= _version_int(deployed):
            print("")
            print(f"=== DEPLOY VERIFIED === {deployed} succeeded and is being served.")
            return 0
        if seen is None and attempt == attempts:
            break
        time.sleep(delay)

    print("", file=sys.stderr)
    print("=== DEPLOY VERIFY FAILED: the new release is not being served ===", file=sys.stderr)
    print(f"Heroku says {deployed} succeeded, but /healthz never reported it.", file=sys.stderr)
    print("", file=sys.stderr)
    print("If /healthz reported null throughout, the dyno metadata that supplies", file=sys.stderr)
    print("HEROKU_RELEASE_VERSION is off and this half of the check cannot work:", file=sys.stderr)
    print(f"  heroku labs:enable runtime-dyno-metadata -a {app}", file=sys.stderr)
    print("Otherwise the web dynos are still serving an older slug.", file=sys.stderr)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--capture", action="store_true")
    p.add_argument("--verify", action="store_true")
    p.add_argument("--app", required=True)
    p.add_argument("--before", default="")
    p.add_argument("--healthz", default="")
    p.add_argument("--attempts", type=int, default=12)
    p.add_argument("--delay", type=int, default=10)
    a = p.parse_args()

    if a.capture:
        return cmd_capture(a.app)
    if a.verify:
        if not a.healthz:
            print("ERROR: --verify needs --healthz", file=sys.stderr)
            return 2
        return cmd_verify(a.app, a.before, a.healthz, a.attempts, a.delay)
    print("ERROR: pass --capture or --verify", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
