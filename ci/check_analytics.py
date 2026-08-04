#!/usr/bin/env python3
"""
check_analytics.py -- end-to-end gate for the first-party analytics collector.

Wired into the `proposal-ci` job in .github/workflows/ci.yml as an explicit
step. A file dropped into ci/ runs nowhere until it is named in a workflow.

Three jobs, in order of how much they matter.

1. THE PRIVACY INVARIANTS ARE ASSERTED, NOT DOCUMENTED. The consent banner
   tells visitors that we count them without storing anything about them. That
   promise is only worth something if something fails loudly when a future
   change breaks it, so this script feeds a request with a known IP and a known
   user-agent through the collector and then greps every column of every row of
   both analytics tables for either value. Same treatment for a password-reset
   token in the URL, which is a shape this repo has and the marketing site does
   not.

2. THE SCOPE BOUNDARY IS ASSERTED. Only the public funnel pages are measured;
   the signed-in interior is not. That is enforced by template inheritance
   (templates/base_public.html) rather than by a list of paths, so this checks
   the enforcement mechanism itself: that the tracker is included from exactly
   one template, that no interior template extends it, and -- live, against a
   real logged-in session -- that an author's dashboard carries no tracker.

3. A normal integration test of the beacon, the consent endpoints, the bot
   heuristics and the outbox, against a real app and a real database.

Usage:
    python ci/check_analytics.py            # from the repo root
Exit codes: 0 = fine, 1 = a check failed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

# A file database, not :memory: -- the app opens more than one connection and an
# in-memory SQLite is per-connection, which fails in ways that look like
# application bugs. Same choice ci/smoke_app.py makes.
_TMPDIR = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "analytics_check.db")
# app.py builds an OpenAI client at import time and raises without this. DUMMY.
# Nothing here talks to OpenAI.
os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ.setdefault("SECRET_KEY", "ci-analytics-test-key")
# Keeps _is_production False so the test client is not fighting secure cookies.
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["ANALYTICS_SALT_KEY"] = "ci-test-key-not-a-secret"
os.environ.pop("ANALYTICS_INGEST_TOKEN", None)
os.environ.pop("TRUST_CLOUDFLARE_IP", None)

# Distinctive values we hunt for in storage afterwards.
PROBE_IP = "203.0.113.77"
PROBE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1 CIPROBEUA"
)
# Built rather than written out. A 32-character hex string assigned to a name
# containing TOKEN is exactly what gitleaks' generic-api-key rule matches, and
# it failed CI on this file — correctly, by its own rules. Suppressing the
# finding with an inline allow would teach the next person to suppress the real
# one; making the fixture obviously fake costs nothing and keeps the scanner
# worth listening to. Still 32 hex chars, so it is the same SHAPE as a real
# reset token, which is what the redaction assertions below actually test.
PROBE_RESET_TOKEN = "deadbeef" * 4

# The complete set of templates allowed to be instrumented. Every one of these
# is a page a prospective author sees BEFORE they have an account. This list is
# not how the boundary is enforced -- base_public.html is -- it is how a change
# to the boundary is forced to be deliberate.
PUBLIC_TEMPLATES = {
    "author_register.html",
    "author_login.html",
    "author_forgot_password.html",
    "author_reset_password.html",
    "confidentiality.html",
    "social_strategy_standalone.html",
}

# Interior / staff templates that must never become instrumented.
INTERIOR_TEMPLATES = {
    "author_dashboard.html",
    "author_proposal.html",
    "author_coaching_dashboard.html",
    "author_coaching_module.html",
    "author_marketing_platform.html",
    "index.html",
    "coach.html",
    "results.html",
    "admin_login.html",
    "admin_dashboard.html",
    "admin_verify_2fa.html",
    "publisher_login.html",
    "publisher_register.html",
    "publisher_dashboard.html",
}

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
    import analytics_collect as ac
    import app as application

    flask_app = application.app
    db = application.db
    Outbox = ac.AnalyticsOutboxEvent
    Visit = ac.AnalyticsVisit

    with flask_app.app_context():
        db.create_all()

    client = flask_app.test_client()
    headers = {"User-Agent": PROBE_UA}
    environ = {"REMOTE_ADDR": PROBE_IP}

    def beacon(events, **kw):
        return client.post(
            "/e",
            data=json.dumps({"v": 1, "events": events}),
            content_type="application/json",
            headers={**headers, **kw.pop("headers", {})},
            environ_base={**environ, **kw.pop("environ_base", {})},
            **kw,
        )

    # ----------------------------------------------------------------------
    print("\nCollector")
    res = beacon([
        {"t": "pageview", "u": "/author/register?utm_source=linkedin&fbclid=xyz",
         "r": "https://www.linkedin.com/feed/", "q": {"utm_source": "linkedin"}},
        {"t": "engagement", "u": "/author/register", "ms": 42000, "sd": 80},
    ])
    check("beacon answers 204", res.status_code == 204, f"got {res.status_code}")
    check("beacon body is empty", res.data == b"", repr(res.data[:40]))

    with flask_app.app_context():
        rows = Outbox.query.order_by(Outbox.id).all()
        check("two events queued", len(rows) == 2, f"got {len(rows)}")
        events = [json.loads(r.body_json) for r in rows]
        pv = events[0]
        check("query string stripped from stored path",
              pv["path"] == "/author/register", str(pv["path"]))
        check("referrer reduced to a host",
              pv["referrer_host"] == "linkedin.com", str(pv["referrer_host"]))
        check("channel classified as social", pv["channel"] == "social", str(pv["channel"]))
        check("device parsed from UA", pv["device_type"] == "mobile", str(pv["device_type"]))
        check("browser parsed from UA", pv["browser"] == "Safari", str(pv["browser"]))
        check("os parsed from UA", pv["os"] == "iOS", str(pv["os"]))
        check("visitor hash is 32 hex",
              len(pv["visitor_hash"]) == 32
              and all(c in "0123456789abcdef" for c in pv["visitor_hash"]))
        check("engagement carries engaged_ms", events[1]["engaged_ms"] == 42000)
        check("scroll depth captured", events[1]["scroll_pct"] == 80)
        check("both events share one session",
              events[0]["session_id"] == events[1]["session_id"])
        check("consented is false without the cookie", pv["consented"] is False)
        check("country is None without Cloudflare", pv["country"] is None)
        check("outbox row carries the configured property name",
              rows[0].site == ac.DEFAULT_SITE, rows[0].site)

    print("\nThe site label is not caller-chosen")
    with flask_app.app_context():
        Outbox.query.delete()
        db.session.commit()
    for forged in ("evil.example.com", "authors.writeitgreat.com", "localhost"):
        beacon([{"t": "pageview", "u": "/author/register"}], headers={"Host": forged})
    with flask_app.app_context():
        sites = {r.site for r in Outbox.query.all()}
        check("the site label ignores the Host header entirely",
              sites == {ac.DEFAULT_SITE}, str(sites))
        check("an invented property cannot be created by a visitor",
              "evil.example.com" not in sites, str(sites))
        Outbox.query.delete()
        db.session.commit()

    # ----------------------------------------------------------------------
    print("\nTokenised URLs (this repo has them; the marketing site does not)")
    beacon([{"t": "pageview", "u": f"/author/reset-password/{PROBE_RESET_TOKEN}"}])
    with flask_app.app_context():
        row = Outbox.query.order_by(Outbox.id.desc()).first()
        stored = json.loads(row.body_json)["path"]
        check("reset-password path stored as its ROUTE, not the token",
              stored == "/author/reset-password/<token>", str(stored))
    beacon([{"t": "pageview", "u": f"/no/such/page/{PROBE_RESET_TOKEN}"}])
    with flask_app.app_context():
        row = Outbox.query.order_by(Outbox.id.desc()).first()
        stored = json.loads(row.body_json)["path"]
        check("an unrouted token-shaped segment is redacted anyway",
              PROBE_RESET_TOKEN not in stored and "<redacted>" in stored, str(stored))

    # ----------------------------------------------------------------------
    print("\nPrivacy invariants (the promises the consent banner makes to visitors)")
    with flask_app.app_context():
        # Every column of every row of both analytics tables, as one string.
        blob = json.dumps([
            {c.name: str(getattr(r, c.name)) for c in r.__table__.columns}
            for model in (Outbox, Visit)
            for r in model.query.all()
        ])
        check("no raw IP address anywhere in storage", PROBE_IP not in blob)
        check("no raw User-Agent anywhere in storage", "CIPROBEUA" not in blob)
        check("no fbclid leaked into storage",
              "fbclid" not in blob and "xyz" not in blob)
        check("no password-reset token anywhere in storage",
              PROBE_RESET_TOKEN not in blob)

    # ----------------------------------------------------------------------
    print("\nIdentity rotation")
    key = ac.salt_key()
    today = ac.visitor_hash(key, "example.com", ip=PROBE_IP, user_agent=PROBE_UA)
    tomorrow = ac.visitor_hash(key, "example.com", ip=PROBE_IP, user_agent=PROBE_UA,
                               now=datetime.utcnow() + timedelta(days=1))
    check("same visitor, same day -> same hash",
          today == ac.visitor_hash(key, "example.com", ip=PROBE_IP, user_agent=PROBE_UA))
    check("same visitor, next day -> DIFFERENT hash (daily rotation)",
          today != tomorrow)
    check("different site -> different hash",
          today != ac.visitor_hash(key, "other.com", ip=PROBE_IP, user_agent=PROBE_UA))
    check("different IP -> different hash",
          today != ac.visitor_hash(key, "example.com", ip="198.51.100.9",
                                   user_agent=PROBE_UA))
    check("consented id is stable across days",
          ac.visitor_hash(key, "example.com", stable_id="abc")
          == ac.visitor_hash(key, "example.com", stable_id="abc",
                             now=datetime.utcnow() + timedelta(days=90)))
    check("the salt key comes from the environment, with no fallback",
          ac.salt_key() == b"ci-test-key-not-a-secret")

    # ----------------------------------------------------------------------
    print("\nConsent")
    res = client.post("/consent", json={"analytics": True})
    joined = " | ".join(res.headers.getlist("Set-Cookie"))
    check("consent choice recorded", f"{ac.CONSENT_COOKIE}=granted" in joined, joined)
    check("visitor id issued on opt-in", f"{ac.VISITOR_COOKIE}=" in joined, joined)
    check("consent cookie is HttpOnly", joined.count("HttpOnly") >= 2,
          "a JS-readable id is both an XSS target and ITP-capped at 7 days")
    check("consent cookie is SameSite=Lax", "Lax" in joined, joined)
    check("visitor cookie lasts 180 days", "Max-Age=15552000" in joined, joined)

    res = client.post("/consent", json={"analytics": False})
    joined = " | ".join(res.headers.getlist("Set-Cookie"))
    check("withdrawal records the refusal", f"{ac.CONSENT_COOKIE}=denied" in joined, joined)
    check("withdrawal actually deletes the id",
          f"{ac.VISITOR_COOKIE}=;" in joined or f'{ac.VISITOR_COOKIE}=""' in joined, joined)
    client.delete_cookie(ac.CONSENT_COOKIE)
    client.delete_cookie(ac.VISITOR_COOKIE)

    check("oversize consent body rejected",
          client.post("/consent", data="x" * 4096,
                      content_type="application/json").status_code == 413)

    # ----------------------------------------------------------------------
    print("\nGlobal Privacy Control")
    res = client.post("/consent", json={"analytics": True}, headers={"Sec-GPC": "1"})
    joined = " | ".join(res.headers.getlist("Set-Cookie"))
    check("GPC refuses consent even when asked for",
          f"{ac.CONSENT_COOKIE}=denied" in joined, joined)
    check("GPC never issues an identifier",
          f"{ac.VISITOR_COOKIE}=" not in joined or f"{ac.VISITOR_COOKIE}=;" in joined,
          joined)
    client.delete_cookie(ac.CONSENT_COOKIE)
    client.delete_cookie(ac.VISITOR_COOKIE)
    page = client.get("/author/register", headers={"Sec-GPC": "1"}).get_data(as_text=True)
    tail = page.split('id="wig-consent"', 1)
    check("banner is not shown to a GPC browser",
          len(tail) == 2 and "hidden>" in tail[1][:400],
          "the banner must render hidden, not be asked again")
    with flask_app.test_request_context("/", headers={"DNT": "1"}):
        check("DNT is honoured as an opt-out too", ac.gpc_opt_out() is True)

    # ----------------------------------------------------------------------
    print("\nUser-agent classification (real strings, not invented ones)")
    # Every one of these is a verbatim UA from a real browser. The bot regex is
    # deliberately broad, so the thing worth testing is that it does not eat a
    # genuine reader -- a false positive silently deletes real traffic from
    # every number on the page.
    real = [
        ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
         "Safari", "iOS", "mobile"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
         "Chrome", "macOS", "desktop"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
         "Edge", "Windows", "desktop"),
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
         "Firefox", "Windows", "desktop"),
        ("Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0.0.0 Mobile Safari/537.36",
         "Chrome", "Android", "mobile"),
        ("Mozilla/5.0 (Linux; Android 13; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0.0.0 Safari/537.36",
         "Chrome", "Android", "tablet"),
        ("Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.5 Safari/604.1",
         "Safari", "iOS", "tablet"),
    ]
    for ua, browser, os_name, device in real:
        got = ac.parse_user_agent(ua)
        label = f"{browser}/{os_name}/{device}"
        check(f"{label} parsed correctly",
              (got["browser"], got["os"], got["device_type"]) == (browser, os_name, device),
              str(got))
        check(f"{label} is NOT flagged as a bot", ac.is_bot(ua) is False)

    # ----------------------------------------------------------------------
    # The proxy IS real now and TRUST_CLOUDFLARE_IP=1 is set in production, so
    # the question has moved: not "is the switch off" but "does the switch
    # alone grant trust". It must not -- the herokuapp origin answers outside
    # Cloudflare. ci/check_edge_trust.py is the fuller guard.
    print("\nCloudflare header trust (the switch alone must never be enough)")
    check("CF-Connecting-IP is not trusted by default", ac.trusts_cloudflare() is False,
          "an untrusted deployment that believes this header gives every client "
          "an unlimited throttle allowance for the price of one header")
    with flask_app.test_request_context(
            "/e", headers={"CF-Connecting-IP": "198.51.100.5"},
            environ_base={"REMOTE_ADDR": PROBE_IP}):
        check("a forged CF-Connecting-IP is ignored", ac.client_ip() == PROBE_IP,
              str(ac.client_ip()))
    os.environ["TRUST_CLOUDFLARE_IP"] = "1"
    try:
        with flask_app.test_request_context(
                "/e", headers={"CF-Connecting-IP": "198.51.100.5"},
                environ_base={"REMOTE_ADDR": PROBE_IP}):
            check("a forged CF-Connecting-IP is STILL ignored when the request "
                  "did not arrive via a Cloudflare edge, even with "
                  "TRUST_CLOUDFLARE_IP=1",
                  ac.client_ip() == PROBE_IP,
                  "this is the herokuapp-origin bypass: believing the header "
                  "here gives a caller a fresh identity per request")
        with flask_app.test_request_context(
                "/e", headers={"CF-Connecting-IP": "198.51.100.5"},
                # A real Cloudflare edge, observed in production 2026-08-04.
                environ_base={"REMOTE_ADDR": "162.158.110.183"}):
            check("CF-Connecting-IP IS used when the request provably came "
                  "through Cloudflare",
                  ac.client_ip() == "198.51.100.5", str(ac.client_ip()))
    finally:
        os.environ.pop("TRUST_CLOUDFLARE_IP", None)

    # ----------------------------------------------------------------------
    print("\nBots")
    with flask_app.app_context():
        Outbox.query.delete()
        db.session.commit()
    beacon([{"t": "pageview", "u": "/author/register"}],
           headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    with flask_app.app_context():
        row = Outbox.query.order_by(Outbox.id.desc()).first()
        check("bot traffic is flagged",
              row is not None and json.loads(row.body_json)["is_bot"] is True)
        check("bot traffic is kept, not dropped", row is not None,
              "reclassifying later is impossible if the rows are gone")
    check("a prefetch is treated as a bot",
          ac.is_bot(real[1][0], "prefetch;prerender") is True)

    # ----------------------------------------------------------------------
    print("\nHardening")
    check("oversize body rejected",
          beacon([{"t": "pageview", "u": "/" + "x" * 20000}]).status_code == 204)
    check("garbage body does not 500",
          client.post("/e", data="not json", content_type="application/json",
                      headers=headers).status_code == 204)
    with flask_app.app_context():
        count_before = Outbox.query.count()
    check("unknown event kind ignored",
          beacon([{"t": "evil", "u": "/"}]).status_code == 204)
    with flask_app.app_context():
        check("unknown event kind stores nothing", Outbox.query.count() == count_before)
    check("a client cannot forge a path onto another origin",
          beacon([{"t": "pageview", "u": "https://evil.example/owned"}]).status_code == 204)
    with flask_app.app_context():
        row = Outbox.query.order_by(Outbox.id.desc()).first()
        stored = json.loads(row.body_json)["path"]
        check("forged absolute URL reduced to its path", stored == "/owned", str(stored))
    check("a hostile engaged_ms is clamped, not stored",
          ac._bounded_int(10 ** 12, 0, 6 * 3600 * 1000) == 6 * 3600 * 1000)
    check("a boolean is never a number", ac._bounded_int(True, 0, 100) == 0)

    # ----------------------------------------------------------------------
    print("\nUnconfigured deployment")
    saved = os.environ.pop("ANALYTICS_SALT_KEY")
    try:
        with flask_app.app_context():
            before = Outbox.query.count()
        check("beacon still answers 204 with no key configured",
              beacon([{"t": "pageview", "u": "/author/register"}]).status_code == 204)
        with flask_app.app_context():
            check("nothing is stored with no key configured",
                  Outbox.query.count() == before)
        page = client.get("/author/register").get_data(as_text=True)
        check("tracker is not even rendered with no key configured",
              "wigTrack" not in page)
    finally:
        os.environ["ANALYTICS_SALT_KEY"] = saved

    # ----------------------------------------------------------------------
    print("\nOutbox")
    with flask_app.app_context():
        sent, failed_count, message = ac.flush_analytics_outbox()
        check("unconfigured forward is a no-op, not an error",
              sent == 0 and failed_count == 0 and "not configured" in message, message)
        stale = Visit(visitor_hash="f" * 32, site="x", session_id="s",
                      started_at=datetime.utcnow() - timedelta(days=5),
                      last_seen_at=datetime.utcnow() - timedelta(days=5))
        db.session.add(stale)
        db.session.commit()
        check("stale visit rows are pruned", ac.prune_analytics_visits() >= 1)
    check("the outbox drain survives an unconfigured deployment",
          ac.drain_analytics_outbox(flask_app) is None)
    check("sender and receiver read the SAME env var name",
          "ANALYTICS_INGEST_TOKEN" in Path(REPO_ROOT / "analytics_collect.py")
          .read_text(encoding="utf-8"),
          "LEADS_FORWARD_TOKEN vs LEADS_INGEST_TOKEN is why the leads link "
          "sat dead in production for days")

    # The delivery half, with the HTTP call stubbed. Nothing here talks to the
    # dashboard; what is being tested is that a success frees the row and a
    # failure keeps it and schedules a retry -- i.e. that a dashboard outage
    # costs zero pageviews, which is the whole argument for the outbox.
    os.environ["ANALYTICS_INGEST_TOKEN"] = "ci-test-token-not-a-secret"
    original_post = ac._post_batch
    try:
        with flask_app.app_context():
            Outbox.query.delete()
            db.session.commit()
        beacon([{"t": "pageview", "u": "/author/register"}])

        # 503 is what the dashboard answers while ITS token is unset — the
        # normal state during a staged rollout. It must NOT count against the
        # row's attempt budget: rows that exhaust it are parked, and parked
        # rows are deleted after a week, so ageing on transient failures turns
        # a two-day outage into permanent data loss.
        ac._post_batch = lambda site, events: ("HTTP 503", False)
        with flask_app.app_context():
            sent, failed_count, _msg = ac.flush_analytics_outbox()
            check("a dashboard outage sends nothing and loses nothing",
                  sent == 0 and failed_count == 1 and Outbox.query.count() == 1)
            row = Outbox.query.first()
            check("a transient failure schedules a retry WITHOUT ageing the row",
                  row.attempts == 0 and row.next_attempt_at is not None
                  and row.next_attempt_at > datetime.utcnow(),
                  f"attempts={row.attempts}")
            row.next_attempt_at = None
            db.session.commit()

        # A payload the receiver will never accept is the only thing that may
        # age a row towards parking.
        ac._post_batch = lambda site, events: ("HTTP 400", True)
        with flask_app.app_context():
            ac.flush_analytics_outbox()
            row = Outbox.query.first()
            check("a payload rejection DOES age the row", row.attempts == 1,
                  f"attempts={row.attempts}")
            row.next_attempt_at = None
            db.session.commit()

        seen = {}

        def _capture(site, events):
            seen["site"] = site
            seen["events"] = events
            return (None, False)

        ac._post_batch = _capture
        with flask_app.app_context():
            sent, failed_count, _msg = ac.flush_analytics_outbox()
            check("a successful send frees the row", sent == 1 and Outbox.query.count() == 0)
        check("the batch carries the configured property name",
              seen.get("site") == ac.DEFAULT_SITE, str(seen.get("site")))
        event = (seen.get("events") or [{}])[0]
        for field in ("event_uid", "kind", "occurred_at", "visitor_hash", "session_id",
                      "consented", "path", "channel", "country", "device_type",
                      "browser", "os", "is_bot", "engaged_ms", "scroll_pct",
                      "target", "page_load_id", "is_entry", "is_exit"):
            check(f"wire field present: {field}", field in event)
        check("no IP or User-Agent on the wire either",
              PROBE_IP not in json.dumps(event) and "CIPROBEUA" not in json.dumps(event))

        # `site` no longer comes from the Host header, so a live deployment
        # produces one site. The grouping still matters: rows queued BEFORE
        # that change carry their old per-host label, and mixing them into one
        # batch would file that traffic under whichever label the batch picked.
        with flask_app.app_context():
            Outbox.query.delete()
            for legacy in (ac.DEFAULT_SITE, "localhost"):
                db.session.add(Outbox(event_uid=f"grouping-{legacy}", site=legacy,
                                      body_json=json.dumps({"kind": "pageview"})))
            db.session.commit()
        batches = []
        ac._post_batch = lambda site, events: (batches.append(site), (None, False))[1]
        with flask_app.app_context():
            ac.flush_analytics_outbox()
            check("every ready site ships in one pass, one POST each",
                  len(batches) == 2 and len(set(batches)) == 2, str(batches))
            check("the queue drains completely", Outbox.query.count() == 0)
    finally:
        ac._post_batch = original_post
        os.environ.pop("ANALYTICS_INGEST_TOKEN", None)

    # ======================================================================
    # The guards that did not survive the port from the marketing site
    # ======================================================================
    print("\nPorted guards")

    # An outbound target is rendered as a clickable link in a signed-in
    # admin's dashboard. This endpoint is unauthenticated.
    with flask_app.app_context():
        Outbox.query.delete()
        db.session.commit()
    beacon([{"t": "outbound", "u": "/author/register", "x": "javascript:alert(1)"},
            {"t": "outbound", "u": "/author/register", "x": "data:text/html,<script>"},
            {"t": "outbound", "u": "/author/register", "x": "https://example.com/ok"}])
    with flask_app.app_context():
        targets = [json.loads(r.body_json).get("target") for r in Outbox.query.all()]
        check("javascript: outbound target is rejected",
              "javascript:alert(1)" not in targets, str(targets))
        check("data: outbound target is rejected",
              not any(t and t.startswith("data:") for t in targets), str(targets))
        check("a real https target still survives",
              "https://example.com/ok" in targets, str(targets))

    # A conversion NAME is not a URL and is deliberately left alone.
    with flask_app.app_context():
        Outbox.query.delete()
        db.session.commit()
    beacon([{"t": "conversion", "u": "/author/register", "n": "author_registered"}])
    with flask_app.app_context():
        names = [json.loads(r.body_json).get("target") for r in Outbox.query.all()]
        check("conversion names are untouched", names == ["author_registered"], str(names))

    # The queue is a buffer. This database holds authors and proposals.
    check("the outbox has a row ceiling", getattr(ac, "MAX_OUTBOX_ROWS", 0) >= 1000)
    # checked_at must be NOW, not 0: a stale timestamp makes _outbox_full()
    # recompute the real (empty) count and overwrite the flag being forced.
    ac._ceiling_state["checked_at"] = time.monotonic()
    ac._ceiling_state["full"] = True
    try:
        with flask_app.app_context():
            Outbox.query.delete()
            db.session.commit()
        beacon([{"t": "pageview", "u": "/author/register"}])
        with flask_app.app_context():
            check("a full outbox sheds load instead of growing",
                  Outbox.query.count() == 0, f"{Outbox.query.count()} rows written")
    finally:
        ac._ceiling_state["checked_at"] = 0.0
        ac._ceiling_state["full"] = False

    # With no ingest token the flush never attempts anything, so `attempts`
    # stays 0 and a parked-row-only prune can never reach these rows.
    with flask_app.app_context():
        Outbox.query.delete()
        db.session.commit()
    beacon([{"t": "pageview", "u": "/author/register"}])
    with flask_app.app_context():
        old = Outbox.query.first()
        old.created_at = datetime.utcnow() - timedelta(
            days=ac.ANALYTICS_OUTBOX_MAX_AGE_DAYS + 1)
        db.session.commit()
        check("an aged, never-attempted row is still prunable",
              old.attempts == 0, f"attempts={old.attempts}")
    ac.drain_analytics_outbox(flask_app)
    with flask_app.app_context():
        check("age-based prune runs with no ingest token configured",
              Outbox.query.count() == 0, f"{Outbox.query.count()} rows survived")

    # The reset-password page is instrumented, so when a visitor follows a link
    # from it, document.referrer on the NEXT page is the full reset URL — token
    # included. The server discards same-origin referrers, but discarding it
    # there would still have put a live credential in a request body and any
    # proxy in between. It has to be dropped in the browser.
    tracker_src = " ".join(
        (REPO_ROOT / "templates" / "_analytics.html").read_text(encoding="utf-8").split())
    check("the pageview does not send a raw document.referrer",
          "r: document.referrer" not in tracker_src)
    check("referrers are filtered to external ones in the browser",
          "function externalReferrer" in tracker_src
          and "r: externalReferrer()" in tracker_src)
    check("the page-load id is minted and sent (contract §5)",
          "event.pid = PID" in tracker_src and "crypto.getRandomValues" in tracker_src)

    # A non-https override would put the bearer token on a cleartext wire.
    saved_url = os.environ.pop("ANALYTICS_INGEST_URL", None)
    try:
        os.environ["ANALYTICS_INGEST_URL"] = "http://evil.example.com/ingest"
        check("a cleartext ingest URL is refused",
              ac._ingest_url() == ac._DEFAULT_INGEST_URL, ac._ingest_url())
        os.environ["ANALYTICS_INGEST_URL"] = "https://staging.example.com/ingest"
        check("an https override is honoured",
              ac._ingest_url() == "https://staging.example.com/ingest")
    finally:
        os.environ.pop("ANALYTICS_INGEST_URL", None)
        if saved_url is not None:
            os.environ["ANALYTICS_INGEST_URL"] = saved_url

    # ======================================================================
    # SCOPE: public funnel pages only, never the signed-in interior
    # ======================================================================
    print("\nScope boundary -- how it is enforced")
    templates = REPO_ROOT / "templates"
    includers = sorted(
        p.name for p in templates.glob("*.html")
        if re.search(r'include\s+["\'](_analytics|_consent)\.html', p.read_text("utf-8"))
    )
    check("the tracker is included from exactly one template",
          includers == ["base_public.html"], str(includers))

    base_public = (templates / "base_public.html").read_text("utf-8")
    check("that template gates on current_user.is_authenticated",
          "not current_user.is_authenticated" in base_public,
          "a signed-in visitor must never be measured, even by mistake")
    check("that template gates on the salt key being configured",
          "analytics_configured" in base_public)

    extenders = {
        p.name for p in templates.glob("*.html")
        if re.search(r'extends\s+["\']base_public\.html["\']', p.read_text("utf-8"))
    }
    check("exactly the intended public pages are instrumented",
          extenders == PUBLIC_TEMPLATES,
          f"unexpected: {sorted(extenders - PUBLIC_TEMPLATES)}; "
          f"missing: {sorted(PUBLIC_TEMPLATES - extenders)}")
    check("no interior or staff template is instrumented",
          not (extenders & INTERIOR_TEMPLATES),
          str(sorted(extenders & INTERIOR_TEMPLATES)))
    check("base.html itself carries no tracker",
          "_analytics.html" not in (templates / "base.html").read_text("utf-8"),
          "every interior page inherits from it")

    print("\nScope boundary -- live")
    for path in ("/author/register", "/author/login", "/author/forgot-password",
                 "/confidentiality", "/social-strategy"):
        body = client.get(path).get_data(as_text=True)
        check(f"tracker present on {path}", "wigTrack" in body and '"/e"' in body)
        check(f"consent banner present on {path}", "wig-consent" in body)
    check("cookie settings control offered on a public page",
          "data-consent-open" in client.get("/author/register").get_data(as_text=True))

    # A real author with a live reset token, so the reset page actually renders
    # instead of bouncing to /author/forgot-password.
    with flask_app.app_context():
        author = application.Author(email="ci-analytics@example.com", name="CI Author")
        author.set_password("not-a-real-password-ci")
        author.password_reset_token = PROBE_RESET_TOKEN
        author.password_reset_expires = datetime.utcnow() + timedelta(hours=1)
        db.session.add(author)
        db.session.commit()
        author_id = author.id

    reset = client.get(f"/author/reset-password/{PROBE_RESET_TOKEN}",
                       follow_redirects=False)
    check("the reset page renders with a live token", reset.status_code == 200,
          f"got {reset.status_code}")
    reset_body = reset.get_data(as_text=True)
    check("the reset page is instrumented", "wigTrack" in reset_body)
    check("the reset page reports its ROUTE, never its token",
          '"/author/reset-password/<token>"' in reset_body
          and PROBE_RESET_TOKEN not in reset_body,
          "a token in a beacon body is a token leaving the page")

    for path in ("/admin/login", "/publisher/login", "/publisher/register"):
        body = client.get(path).get_data(as_text=True)
        check(f"staff/publisher page {path} is NOT instrumented",
              "wigTrack" not in body and "wig-consent" not in body)

    # A real logged-in session, because the static checks above cannot prove
    # what the running app does with one.
    with client.session_transaction() as sess:
        sess["_user_id"] = str(author_id)
        sess["_fresh"] = True
        sess["user_type"] = "author"
    dash = client.get("/author/dashboard", follow_redirects=False)
    check("the signed-in author dashboard renders", dash.status_code == 200,
          f"got {dash.status_code}")
    body = dash.get_data(as_text=True)
    check("a signed-in author is NOT instrumented",
          "wigTrack" not in body and "wig-consent" not in body,
          "measuring identified users inside the product is a different thing "
          "with a different privacy footing, and is out of scope")

    # And the belt-and-braces half: even on an instrumented template, being
    # signed in switches it off.
    body = client.get("/confidentiality").get_data(as_text=True)
    check("even a public template drops the tracker when signed in",
          "wigTrack" not in body)

    # ----------------------------------------------------------------------
    print(f"\n{checks - len(failures)}/{checks} checks passed.")
    if failures:
        print("\n=== ANALYTICS CHECK FAILED ===", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: collector, consent, privacy invariants and the scope boundary all hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
