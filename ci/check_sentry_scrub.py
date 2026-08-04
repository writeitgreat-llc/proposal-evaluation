#!/usr/bin/env python3
"""Prove that Sentry cannot ship personal data out of this app.

Run from the repo root:  python ci/check_sentry_scrub.py

This is the standing guard on observability.py. It exists because every leak it
checks for is a *default* of the Sentry SDK rather than a mistake somebody made:
request bodies up to 10,000 bytes, local variables in every stack frame, no
truncation, and a URL that is captured whatever else you turn off. A future
edit that "simplifies" observability.py back toward the documented five-line
install will reintroduce all of them at once, and nothing else in CI would
notice.

Three things are checked, and the second is the one that matters:

  1. That init_sentry() produces a client whose options are what we think.
     Checked by initialising against a DEAD LOCAL DSN -- init succeeds, the
     transport fails silently, and nothing is sent anywhere. Never point this
     at the real DSN: `website` CI runs on every push, and the org has 5,000
     events a month across four projects.

  2. That a synthetic event carrying every kind of secret these apps actually
     handle comes out the far side of _before_send with none of them left.

  3. That every event is tagged with the KIND of process that produced it, so
     a maintenance script crashing on a one-off dyno cannot be mistaken for the
     public site falling over. This one is checked end to end, on an event read
     back off the transport, because the failure it guards against -- a tag
     that is computed correctly and then never reaches an event -- looks
     exactly like success from inside the module.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # wig-dashboard and proposal-evaluation keep it at the repo root
    import observability
except ImportError:  # the marketing site keeps it inside the app package
    from app import observability  # type: ignore[no-redef]

FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


# A DSN that parses but can never connect. init() succeeds; the transport
# quietly gives up. Do not replace with a real DSN.
DEAD_DSN = "https://ffffffffffffffffffffffffffffffff@127.0.0.1:1/1"

SECRET = "s3cr3t-do-not-ship"
TOKEN = "9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c"  # uuid4().hex-shaped


def main() -> int:
    os.environ["SENTRY_DSN"] = DEAD_DSN
    os.environ.pop("SENTRY_SAMPLE_RATE", None)
    # Pose as the one-off dyno from the incident the process_type tag exists
    # for. init_sentry() reads DYNO once, so this has to be set BEFORE it runs;
    # section 7 then proves the tag reached a real event, not just that the
    # helper returns the right string.
    os.environ["DYNO"] = "run.1234"

    initialised = observability.init_sentry("ci-selftest")
    check(initialised, "init_sentry() returned False with SENTRY_DSN set")

    import sentry_sdk

    client = sentry_sdk.get_client()
    check(client.is_active(), "Sentry client is not active after init_sentry()")
    opts = client.options

    # --- 1. the options that stop collection at source -----------------------
    check(opts["max_request_body_size"] == "never",
          f'max_request_body_size is {opts["max_request_body_size"]!r}, not "never" '
          "-- request bodies up to 10,000 bytes would be sent")
    check(opts["include_local_variables"] is False,
          "include_local_variables is not False -- stack frames would carry locals")
    check(opts["send_default_pii"] is False,
          "send_default_pii is not False")
    check(opts["max_value_length"] == 1024,
          f'max_value_length is {opts["max_value_length"]!r}; the SDK default is '
          "None, meaning no truncation at all")
    check(opts["traces_sample_rate"] is None,
          "traces_sample_rate must be None, not 0 -- 0 still continues incoming "
          "distributed traces and bills the transactions")
    check(opts["before_send"] is not None, "before_send is not wired up")
    check(opts["before_breadcrumb"] is not None,
          "before_breadcrumb is not wired up -- outbound-request breadcrumbs "
          "would carry full URLs, and a Healthchecks ping URL is a credential")

    scrubber = opts["event_scrubber"]
    check(scrubber is not None, "no EventScrubber configured")
    check(getattr(scrubber, "recursive", False) is True,
          "EventScrubber.recursive is False -- nested dictionaries are not walked")
    for key in ("password", "totp_code", "proposal_text", "manuscript"):
        check(key in scrubber.denylist, f"{key!r} missing from the scrubber denylist")

    # --- 2. an event carrying everything these apps really handle ------------
    event = {
        "transaction": "/api/evaluate",
        "exception": {"values": [{"type": "ValueError"}]},
        "user": {"id": 7, "email": "author@example.com", "ip_address": "1.2.3.4"},
        "request": {
            "url": f"https://authors.writeitgreat.com/author/reset-password/{TOKEN}",
            "query_string": f"token={TOKEN}&email=author@example.com",
            "method": "POST",
            "data": {"password": SECRET, "proposal_text": "chapter one ..."},
            "cookies": {"session": SECRET},
            "headers": {
                "Referer": f"https://authors.writeitgreat.com/author/reset-password/{TOKEN}",
                "User-Agent": "Mozilla/5.0",
                "Cf-Connecting-Ip": "203.0.113.9",
                "Authorization": f"Bearer {SECRET}",
                "Content-Type": "application/json",
            },
            "env": {"REMOTE_ADDR": "203.0.113.9", "SERVER_NAME": "authors"},
        },
    }

    out = observability._before_send(event, {})
    check(out is not None, "_before_send dropped a first, unthrottled event")

    if out is not None:
        blob = repr(out)
        check(SECRET not in blob, "a secret survived _before_send")
        check(TOKEN not in blob,
              "a live single-use token survived _before_send (it is in the URL "
              "path, the query string AND the Referer header)")
        check("author@example.com" not in blob, "an email address survived _before_send")
        check("203.0.113.9" not in blob, "a visitor IP address survived _before_send")
        check("Mozilla/5.0" not in blob, "the User-Agent survived _before_send")
        check("user" not in out, "event['user'] was not removed")
        req = out.get("request", {})
        check("data" not in req, "request body was not removed")
        check("cookies" not in req, "cookies were not removed")
        check("query_string" not in req, "query string was not removed")
        check("Content-Type" in req.get("headers", {}),
              "harmless headers were dropped too -- the filter is too broad")

    # --- 3. outbound-request breadcrumbs ------------------------------------
    crumb = observability._before_breadcrumb(
        {"category": "httplib",
         "data": {"url": f"https://hc-ping.com/{TOKEN}", "http.query": f"k={SECRET}"}},
        {},
    )
    check(crumb is not None, "_before_breadcrumb dropped a breadcrumb entirely")
    if crumb is not None:
        crumb_blob = repr(crumb)
        check(TOKEN not in crumb_blob,
              "a Healthchecks-style ping URL survived _before_breadcrumb -- that "
              "URL is itself the credential")
        check(SECRET not in crumb_blob, "a query string survived _before_breadcrumb")

    # --- 4. bound SQL parameters inside an exception MESSAGE ----------------
    # The leak no key-name denylist can reach: SQLAlchemy puts the values it
    # bound into str(exc), which becomes the event's exception `value`.
    sql_event = {
        "transaction": "/api/intake",
        "exception": {"values": [{
            "type": "IntegrityError",
            "value": (
                "(psycopg2.errors.UniqueViolation) duplicate key value violates "
                'unique constraint "ix_submissions_email"\n'
                "DETAIL:  Key (email)=(prospect@example.com) already exists.\n"
                "[SQL: INSERT INTO submissions (name, email, message) VALUES (%s, %s, %s)]\n"
                "[parameters: ('Ada', 'prospect@example.com', 'MANUSCRIPT-TEXT')]\n"
                "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
            ),
        }]},
    }
    observability._window_start = 0.0
    observability._window_total = 0
    observability._window_counts.clear()
    sql_out = observability._before_send(sql_event, {})
    check(sql_out is not None, "_before_send dropped the SQL-parameter event")
    if sql_out is not None:
        sql_blob = repr(sql_out)
        check("prospect@example.com" not in sql_blob,
              "an email address bound into a failed SQL statement survived — it is "
              "inside the exception MESSAGE, which the EventScrubber never reads")
        check("MANUSCRIPT-TEXT" not in sql_blob,
              "bound SQL parameters survived inside the exception message")
        check("INSERT INTO submissions" in sql_blob,
              "the SQL statement was redacted too — keep the query shape, it is the "
              "useful half and it carries no values")

    # --- 5. url redaction leaves real paths alone ---------------------------
    check(observability.redact_url("https://writeitgreat.com/services/developmental-editing")
          == "https://writeitgreat.com/services/developmental-editing",
          "redact_url mangled an ordinary content URL")

    # --- 6. the burst limiter actually closes -------------------------------
    os.environ["SENTRY_MAX_EVENTS_PER_HOUR"] = "2"
    os.environ["SENTRY_MAX_PER_FINGERPRINT_PER_HOUR"] = "1"
    observability._window_start = 0.0
    observability._window_total = 0
    observability._window_counts.clear()
    kept = sum(
        1 for _ in range(10)
        if observability._before_send(
            {"exception": {"values": [{"type": "ValueError"}]},
             "transaction": "/x"}, {}) is not None
    )
    check(kept == 1, f"per-fingerprint cap let {kept} events through, expected 1")
    os.environ.pop("SENTRY_MAX_EVENTS_PER_HOUR", None)
    os.environ.pop("SENTRY_MAX_PER_FINGERPRINT_PER_HOUR", None)

    # --- 7. the process_type tag --------------------------------------------
    # A maintenance script that crashes must not look like a production
    # outage. Two halves, and the second is the one that would actually catch a
    # regression: that the tag survives all the way onto a real event.
    captured: list = []
    client.transport.capture_envelope = lambda envelope: captured.extend(
        item.payload.json for item in envelope.items
    )
    observability._window_start = 0.0
    observability._window_total = 0
    observability._window_counts.clear()
    sentry_sdk.capture_message("ci-selftest: process tag")

    # Pick the ERROR event out of the envelope by event_id rather than trusting
    # position: sessions and client reports ride in envelopes too, and a check
    # that goes red for that reason costs ~72 billed CI minutes to take back.
    events = [p for p in captured if isinstance(p, dict) and "event_id" in p]
    tags = (events[0].get("tags") if events else None) or {}
    check(bool(events), "no event reached the transport at all")
    check(tags.get("process_type") == "run",
          f'process_type on a real event is {tags.get("process_type")!r}, not "run" '
          "-- DYNO was 'run.1234' at init, so a heroku-run crash would still be "
          "indistinguishable from a visitor-facing 500")
    check(tags.get("sentry_config") == observability.CONFIG_VERSION,
          "the sentry_config tag did not reach the event -- it is the ONLY "
          "cross-repo drift detector that exists")

    # Every shape Heroku actually produces, plus the two it never does.
    original_dyno = os.environ.get("DYNO")
    for dyno, expected in (
        ("web.1", "web"),
        ("scheduler.1234", "scheduler"),
        ("release.1234", "release"),
        ("run.1234", "run"),
        ("worker.2", "worker"),
        ("web", "web"),                 # no instance suffix
        ("Web.1", "web"),               # case is not significant
        ("", "local"),                  # set but empty
        ("weird value!", "unknown"),    # never passed through unsanitised
        ("x" * 40, "unknown"),          # nor unbounded in length
    ):
        os.environ["DYNO"] = dyno
        got = observability.process_type()
        check(got == expected,
              f"process_type() with DYNO={dyno!r} returned {got!r}, expected {expected!r}")
    os.environ.pop("DYNO", None)
    check(observability.process_type() == "local",
          "process_type() must be 'local' with no DYNO at all -- that is a "
          "laptop or a CI runner, not a dyno we failed to recognise")
    if original_dyno is not None:
        os.environ["DYNO"] = original_dyno

    # --- 8. cross-repo drift ------------------------------------------------
    check(observability.CONFIG_VERSION == "3",
          f"CONFIG_VERSION is {observability.CONFIG_VERSION!r}, expected '3'. If you changed "
          "observability.py on purpose, bump it here AND copy the file to the "
          "other two repos -- no CI job can see across them.")

    if FAILURES:
        print("check_sentry_scrub: FAIL")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1

    print("check_sentry_scrub: OK — Sentry config verified, synthetic event clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
