#!/usr/bin/env python3
"""The standing guard on the herokuapp origin.

Wired into .github/workflows/ci.yml. It is an explicit step, not a glob -- a
file dropped into ci/ runs nowhere until it is named in the workflow.

`proposal-evaluation-20d7e1515843.herokuapp.com` serves this same app and does
not pass through Cloudflare. Heroku offers no origin firewall on this plan, so
the address cannot be closed; it is made useless instead. Two halves, and this
file guards both.

**The security half.** Several controls here key on
`analytics_collect.client_ip()`, which reads `CF-Connecting-IP`: the sign-up
rate limits added after the July 2026 bot, the `/social-strategy` lead-magnet
limits, `/api/submit`'s limiter, and `visitor_hash`. Cloudflare overwrites that
header on traffic it proxies and nothing overwrites it on the origin, so
`TRUST_CLOUDFLARE_IP=1` being the whole test meant a caller there could pick a
new identity per request. edge_trust.py fixes that by proving arrival from the
appended `X-Forwarded-For` hop, which Heroku's router writes and a client
cannot reach.

**The indexing half.** An author-facing copy of the site at a second address
splits the ranking a paid campaign is buying. GET/HEAD on the origin now 301s
to authors.writeitgreat.com.

The assertions that earn this file's existence, because each fails silently:

1. `is_cloudflare_address()` still classifies edges ACTUALLY OBSERVED in
   production. A stale range list degrades throttling to nothing and inflates
   visitor counts, with no error anywhere.
2. The machine paths are still exempt. The Wix caller posts to `/api/submit`
   with an X-API-Key and polls `/api/status/<id>` with a GET; a redirect there
   breaks both the CORS preflight and, in most clients, the credential.
3. The deploy workflow's origin smoke target is one of those exempt paths.
   That coupling lives in two files and drifts apart; when it does, the deploy
   gate of an already-merged main is what breaks.

Usage:
    python ci/check_edge_trust.py           # from the repo root
Exit codes: 0 = fine, 1 = a check failed.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "edge_check.db")
# app.py builds an OpenAI client at import time and raises without this. DUMMY.
os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ.setdefault("SECRET_KEY", "ci-edge-trust-test-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ.pop("TRUST_CLOUDFLARE_IP", None)
# The redirect target must not depend on a var that happens to be set in CI.
os.environ.pop("SITE_BASE_URL", None)

import edge_trust  # noqa: E402
from app import app  # noqa: E402

FAILURES: list[str] = []

BYPASS = "http://proposal-evaluation-20d7e1515843.herokuapp.com"
CANONICAL = "https://authors.writeitgreat.com"

# Addresses observed in the production router log on 2026-08-04. The Cloudflare
# ones are real edges that served real visitors; the direct ones are a probe
# sent straight at an origin plus two third-party monitors doing the same.
# Hard-coding measurements rather than inventing plausible addresses is the
# point -- it is what makes a range list going stale a test failure.
OBSERVED_CLOUDFLARE = (
    "162.158.110.183",
    "104.23.209.50",
    "162.158.49.78",
    "162.158.38.66",
    "162.158.230.161",
)
OBSERVED_DIRECT = (
    "73.100.144.66",
    "5.223.73.226",
    "192.53.169.77",
)


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        if detail:
            print(f"          {detail}")
        FAILURES.append(label)


def main() -> int:
    app.config["TESTING"] = True

    # Capture what the app sees as remote_addr AFTER ProxyFix, so the one
    # assumption everything here rests on is proved end-to-end through the real
    # WSGI stack rather than asserted in a comment.
    seen: dict[str, object] = {}

    @app.before_request
    def _capture():
        from flask import request

        seen["remote_addr"] = request.remote_addr
        return None

    client = app.test_client()

    print("\nProxyFix reads the hop Heroku appends, not the one a caller supplies")
    # Measured on production 2026-08-04:
    #   fwd="203.0.113.99, 73.100.144.66"
    # where 203.0.113.99 was the forged header and 73.100.144.66 the real
    # caller. Heroku appends; ProxyFix(x_for=1) takes values[-1].
    client.get(
        "/healthz",
        base_url=BYPASS,
        headers={
            "X-Forwarded-For": "203.0.113.99, 73.100.144.66",
            "X-Forwarded-Proto": "https",
        },
    )
    check("remote_addr is the RIGHTMOST X-Forwarded-For hop",
          seen.get("remote_addr") == "73.100.144.66",
          f"got {seen.get('remote_addr')!r}; if this is the forged value, "
          "every trust decision in edge_trust.py is inverted")

    print("\nCloudflare range list still covers observed production traffic")
    check("the vendored list parses and is non-empty",
          len(edge_trust.CLOUDFLARE_NETWORKS) > 0)
    check("RANGES_FETCHED is a date",
          bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", edge_trust.RANGES_FETCHED)),
          edge_trust.RANGES_FETCHED)
    for addr in OBSERVED_CLOUDFLARE:
        check(f"{addr} is recognised as a Cloudflare edge",
              edge_trust.is_cloudflare_address(addr),
              "refresh the ranges from cloudflare.com/ips-v4 -- until then, "
              "traffic through this edge is untrusted, so the sign-up and "
              "lead-magnet limits do not fire and visitor counts inflate")
    for addr in OBSERVED_DIRECT:
        check(f"{addr} is NOT treated as Cloudflare",
              not edge_trust.is_cloudflare_address(addr))
    for junk in ("", None, "not-an-ip", "162.158.110.183, 1.2.3.4", "1.2.3.4/8"):
        check(f"malformed peer {junk!r} is not trusted",
              not edge_trust.is_cloudflare_address(junk))

    print("\nCF-Connecting-IP is believed only on requests that came via Cloudflare")
    os.environ["TRUST_CLOUDFLARE_IP"] = "1"
    try:
        import analytics_collect

        with app.test_request_context(
            "/e", headers={"CF-Connecting-IP": "198.51.100.5"},
            environ_base={"REMOTE_ADDR": "162.158.110.183"},
        ):
            check("via a real edge, the header IS used",
                  analytics_collect.client_ip() == "198.51.100.5",
                  str(analytics_collect.client_ip()))

        with app.test_request_context(
            "/e", headers={"CF-Connecting-IP": "198.51.100.5"},
            environ_base={"REMOTE_ADDR": "73.100.144.66"},
        ):
            check("direct to the origin, the header is IGNORED",
                  analytics_collect.client_ip() == "73.100.144.66",
                  "this is the whole bypass: a caller varying this header "
                  "would get a fresh allowance on every per-visitor limit")

        # The nastier version: forge the header AND claim a Cloudflare address
        # in a place the caller controls. Only the appended hop decides.
        with app.test_request_context(
            "/e",
            headers={
                "CF-Connecting-IP": "198.51.100.5",
                "X-Forwarded-For": "162.158.110.183",
                "CF-Ray": "a2605e459a9fb39b-BOS",
            },
            environ_base={"REMOTE_ADDR": "73.100.144.66"},
        ):
            check("claiming a Cloudflare address in a client-supplied header "
                  "does not buy trust",
                  analytics_collect.client_ip() == "73.100.144.66",
                  str(analytics_collect.client_ip()))
    finally:
        os.environ.pop("TRUST_CLOUDFLARE_IP", None)

    print("\nOrdinary visits to the origin are sent to the real domain")
    r = client.get("/author/login", base_url=BYPASS,
                   headers={"X-Forwarded-Proto": "https"})
    check("GET /author/login on the origin 301s", r.status_code == 301,
          str(r.status_code))
    check("...to the canonical domain",
          r.headers.get("Location") == f"{CANONICAL}/author/login",
          str(r.headers.get("Location")))

    r = client.get("/author/register?utm_source=meta&utm_campaign=launch",
                   base_url=BYPASS, headers={"X-Forwarded-Proto": "https"})
    check("the path and the campaign parameters survive the redirect",
          r.headers.get("Location")
          == f"{CANONICAL}/author/register?utm_source=meta&utm_campaign=launch",
          "every campaign link points at /author/register; dropping utm_* "
          "here would silently cost the source")

    r = client.get("/author/login", base_url=CANONICAL)
    check("the canonical domain itself is never redirected",
          r.status_code != 301, str(r.status_code))

    print("\nMachine callers keep working on the origin")
    for path in ("/healthz", "/api/status/abc123",
                 "/.well-known/acme-challenge/x"):
        r = client.get(path, base_url=BYPASS,
                       headers={"X-Forwarded-Proto": "https"})
        check(f"GET {path} is not redirected off the origin",
              r.status_code != 301,
              f"got 301 -> {r.headers.get('Location')}. The Wix caller polls "
              "/api/status and the origin-only uptime probe watches /healthz")

    r = client.open("/api/submit", method="OPTIONS", base_url=BYPASS,
                    headers={"X-Forwarded-Proto": "https"})
    check("the CORS preflight on /api/submit is not redirected",
          r.status_code != 301, str(r.status_code))

    r = client.post("/api/submit", base_url=BYPASS,
                    headers={"X-Forwarded-Proto": "https"})
    check("POST on the origin is not redirected", r.status_code != 301,
          str(r.status_code))

    print("\nThe deploy workflow's origin smoke target is an exempt path")
    # Two files, one coupling, and it drifts: if the smoke target stops being
    # exempt, `curl -L` follows the 301 and the origin check silently starts
    # measuring Cloudflare instead -- both smoke steps then test the same path.
    deploy_yml = REPO_ROOT / ".github" / "workflows" / "deploy.yml"
    body = deploy_yml.read_text()
    m = re.search(r"^\s*APP_URL:\s*(\S+)", body, re.M)
    check("deploy.yml still defines APP_URL", m is not None)
    if m:
        url = m.group(1)
        rest = url.split("://", 1)[-1]
        path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
        check(f"APP_URL path {path!r} is exempt from the origin redirect",
              edge_trust.is_exempt_path(path),
              "the post-deploy smoke check would follow the 301 to the edge, "
              "so it would no longer isolate 'the dyno is broken' from 'the "
              "edge is broken' -- which is the only reason it exists")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("All edge-trust checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
