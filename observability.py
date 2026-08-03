"""Sentry initialisation, vendored IDENTICALLY into all three Write It Great apps.

    website/app/observability.py
    proposal-evaluation/observability.py
    wig-dashboard/observability.py

They are separate repos, so no CI job can compare this file across them -- the
same blind spot `analytics_channel_rules.py` has. Every event therefore carries a
`sentry_config` tag; a version skew visible in Sentry's tag list is the only
cross-repo drift detector that exists. **Bump CONFIG_VERSION on any change**, and
copy the file to the other two in the same round of work.

WHY THIS FILE EXISTS AT ALL, RATHER THAN FIVE LINES OF sentry_sdk.init()

The published "roughly five lines per app" install is wrong for these three apps
in one specific way: it sends personal data by default, and these apps carry
unusually bad data to send. `send_default_pii=False` is necessary and NOT
sufficient. Verified against sentry-sdk 2.66.1 source, not the docs:

  * Request BODIES are not gated on send_default_pii at all. They are gated on
    `max_request_body_size`, whose default "medium" means "send up to 10,000
    bytes" (sentry_sdk/integrations/_wsgi_common.py, request_body_within_bounds).
    That is manuscript text, cleartext passwords out of request.form, and whole
    intake payloads.
  * `include_local_variables` defaults to True, so every stack frame ships its
    locals -- which is the same data again, one layer down.
  * `max_value_length` defaults to None, i.e. no truncation (consts.py:5).
  * The EventScrubber matches EXACT lowercased key names, never substrings
    (scrubber.py, `k.lower() in self.denylist`), and does NOT recurse unless you
    ask. It also never looks at a URL.
  * Sentry captures request.url and request.query_string regardless of every
    setting above. Both of these apps put live single-use credentials in a URL.

So the scrubbing here is deliberate, and `ci/check_sentry_scrub.py` in each repo
re-proves it on a synthetic event. If you relax something, that check should go
red; if it does not, the check is wrong.

PLACEMENT (do not move without reading this)

  * Call init_sentry() at MODULE scope, before the Flask object is created, so
    that an exception during app construction is still reported.
  * All three gunicorn configs leave `preload_app` unset (default False), so each
    worker imports the app module AFTER forking and gets its own transport
    thread. If anyone ever sets `preload_app = True`, event delivery from the
    workers stops SILENTLY and init must move into a `post_fork` hook.
  * init_sentry() is a no-op returning False when SENTRY_DSN is unset. No CI job
    sets it, and several import the app module directly.
"""

from __future__ import annotations

import os
import re
import threading
import time

# Bump on ANY change to this file, then copy to the other two repos.
CONFIG_VERSION = "2"

_DEFAULT_MAX_PER_HOUR = 15
_DEFAULT_MAX_PER_FINGERPRINT_PER_HOUR = 3

# Keys these apps use that sentry-sdk's DEFAULT_DENYLIST does not cover.
# Matching is exact-key, lowercased -- "code" does NOT match "status_code".
# "code" is here because it is the field name the portal's 2FA form posts the
# TOTP code under; the cost is that a benign key literally named "code" is
# redacted too, which is a trade worth making.
EXTRA_DENYLIST = [
    "confirm_password", "current_password", "currentpassword",
    "new_password", "newpassword", "totp_code", "code", "recovery_code",
    "totp_secret", "reset_token", "sso_token",
    "cf_connecting_ip", "cf-connecting-ip", "user_agent", "user-agent",
    "proposal_text", "manuscript", "content_text", "messages", "answers",
    "payload_json", "prospect", "estimate", "browser_errors",
]

# Dropped from request headers unconditionally. sentry-sdk already strips
# Cookie, Authorization, X-API-Key and X-Forwarded-For when send_default_pii is
# False. It does NOT strip CF-Connecting-IP or User-Agent, and the marketing
# site's privacy policy promises visitors we keep neither.
#
# "referer" is here for a non-obvious reason: on the author portal, the Referer
# of any request made from a password-reset page carries that page's live
# single-use token.
_DROP_HEADERS = frozenset({
    "cookie", "set-cookie", "authorization", "proxy-authorization", "x-api-key",
    "x-forwarded-for", "x-real-ip", "cf-connecting-ip", "cf-ipcountry",
    "true-client-ip", "forwarded", "user-agent", "referer", "origin",
})

# A path segment is "token-shaped" if it is a uuid, a long hex string, or a long
# mixed alphanumeric string containing at least one digit. That catches
# uuid4().hex reset tokens and itsdangerous signed tokens while leaving real
# slugs like "developmental-editing" alone.
#
# Known gap, stated rather than papered over: a secrets.token_urlsafe(32) that
# happens to contain no digit at all (~0.08% of tokens) is not matched.
_TOKENISH = re.compile(
    r"^(?:[0-9a-fA-F]{16,}"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|(?=[A-Za-z0-9_.\-]{20,}$)[A-Za-z0-9_.\-]*[0-9][A-Za-z0-9_.\-]*)$"
)

# Data that database drivers put inside the EXCEPTION MESSAGE, where no
# key-name denylist can ever reach it.
#
# This is the leak that survives everything else in this file, and it is worth
# understanding rather than trusting. SQLAlchemy formats a failed statement as
#
#   (psycopg2.errors.UniqueViolation) duplicate key value violates ...
#   DETAIL:  Key (email)=(prospect@example.com) already exists.
#   [SQL: INSERT INTO submissions (name, email, message) VALUES (%s, %s, %s)]
#   [parameters: ('Ada', 'prospect@example.com', 'my whole manuscript ...')]
#
# and that whole string is `str(exc)`, which becomes the event's exception
# `value`. The EventScrubber only matches KEY NAMES in dictionaries, and
# `_before_send` below only rewrites `event["request"]` -- so before this
# existed, one failed INSERT shipped an intake payload or a chunk of manuscript
# to Sentry in the one field nothing was looking at. `max_value_length`
# truncates it; truncation is not redaction.
#
# `[SQL: ...]` is deliberately KEPT: parameters are bound separately, so the
# statement is the query's shape, which is the part that is actually useful in
# an issue title. The residual risk is a query built by string concatenation --
# but that is a SQL-injection bug on its own terms, and it should be fixed
# there rather than hidden here.
#
# Belt and braces: each app also sets `hide_parameters: True` in its
# SQLALCHEMY_ENGINE_OPTIONS. That one matters independently, because it stops
# the parameters reaching the *log stream* too, and `_before_send` never runs
# at all when SENTRY_DSN is unset.
_SQL_PARAMS = re.compile(r"\[parameters:.*?\](?=\s*(?:\[|\(Background|$))", re.S)
_PG_DETAIL_KEY = re.compile(r"(DETAIL:\s*Key\s*\([^)]*\)=)\([^)]*\)")


def scrub_message(text: str) -> str:
    """Strip bound SQL parameters out of a free-text error message."""
    if not isinstance(text, str):
        return text
    text = _SQL_PARAMS.sub("[parameters: <redacted>]", text)
    text = _PG_DETAIL_KEY.sub(r"\1(<redacted>)", text)
    return text


_lock = threading.Lock()
_window_start = 0.0
_window_total = 0
_window_counts: dict = {}


def redact_url(url: str) -> str:
    """Drop the query string and redact token-shaped path segments.

    Sentry captures request.url and request.query_string no matter how
    send_default_pii is set, and these apps put live credentials in both:
    password-reset links carry the token in the PATH, and the dashboard SSO
    handoff carries a 60-second token in the QUERY STRING.
    """
    if not isinstance(url, str):
        return url
    url = url.split("?", 1)[0].split("#", 1)[0]
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    host, _, path = rest.partition("/")
    parts = ["<redacted>" if _TOKENISH.match(p) else p for p in path.split("/")]
    prefix = f"{scheme}://" if sep else ""
    return f"{prefix}{host}/" + "/".join(parts)


def _rate_limited(event) -> bool:
    """Per-process hourly event budget.

    This bounds a BURST, not a month -- the window resets every hour, so a
    steady fault still spends quota all day. Sentry's server-side Spike
    Protection is what bounds the month, and the tool that would actually be
    right here -- a per-DSN rate limit -- is Business-tier only.

    Both knobs are environment variables on purpose: turning noise down during
    an incident must never require a `website` deploy, which bills ~29 CI
    minutes and takes the better part of an hour.
    """
    global _window_start, _window_total
    try:
        max_total = int(os.environ.get(
            "SENTRY_MAX_EVENTS_PER_HOUR", _DEFAULT_MAX_PER_HOUR))
        max_fp = int(os.environ.get(
            "SENTRY_MAX_PER_FINGERPRINT_PER_HOUR",
            _DEFAULT_MAX_PER_FINGERPRINT_PER_HOUR))
    except ValueError:
        max_total = _DEFAULT_MAX_PER_HOUR
        max_fp = _DEFAULT_MAX_PER_FINGERPRINT_PER_HOUR

    values = (event.get("exception") or {}).get("values") or []
    kind = (values[-1].get("type") if values
            else event.get("logger") or event.get("level") or "message")
    key = f"{kind}|{event.get('transaction') or ''}"

    now = time.monotonic()
    with _lock:
        if now - _window_start > 3600:
            _window_start, _window_total = now, 0
            _window_counts.clear()
        if _window_total >= max_total:
            return True
        if _window_counts.get(key, 0) >= max_fp:
            return True
        _window_total += 1
        _window_counts[key] = _window_counts.get(key, 0) + 1
    return False


def _before_send(event, hint):
    """Last gate before an event leaves the process.

    Runs AFTER the EventScrubber (sentry_sdk/client.py, _prepare_event), so this
    is a second line rather than the only one. Anything that raises in here
    drops the event: we would rather lose a report than ship one we failed to
    scrub.
    """
    try:
        req = event.get("request")
        if isinstance(req, dict):
            # Belt and braces on top of max_request_body_size="never".
            req.pop("data", None)
            req.pop("cookies", None)
            req.pop("query_string", None)
            if "url" in req:
                req["url"] = redact_url(req["url"])
            headers = req.get("headers")
            if isinstance(headers, dict):
                req["headers"] = {
                    k: v for k, v in headers.items()
                    if k.lower() not in _DROP_HEADERS
                }
            env = req.get("env")
            if isinstance(env, dict):
                env.pop("REMOTE_ADDR", None)
        event.pop("user", None)

        # Free text, in every place Sentry keeps free text. See _SQL_PARAMS.
        for value in (event.get("exception") or {}).get("values") or []:
            if isinstance(value, dict) and "value" in value:
                value["value"] = scrub_message(value["value"])
        logentry = event.get("logentry")
        if isinstance(logentry, dict):
            if "message" in logentry:
                logentry["message"] = scrub_message(logentry["message"])
            # The %-args of a log call. These are the values themselves rather
            # than a rendering of them, so there is nothing to redact around --
            # they go.
            logentry.pop("params", None)
        if "message" in event:
            event["message"] = scrub_message(event["message"])
        crumbs = (event.get("breadcrumbs") or {})
        if isinstance(crumbs, dict):
            for crumb in crumbs.get("values") or []:
                if isinstance(crumb, dict) and "message" in crumb:
                    crumb["message"] = scrub_message(crumb["message"])
    except Exception:
        return None

    if _rate_limited(event):
        return None
    return event


def _before_breadcrumb(crumb, hint):
    """Strip URLs out of outbound-HTTP breadcrumbs.

    StdlibIntegration is on by default and records every outbound request as a
    breadcrumb carrying the full URL, including its query string. The
    EventScrubber does scrub breadcrumb `data`, but only by key name, and the
    key here is `url`, which is in nobody's denylist.

    That matters concretely: a Healthchecks.io ping URL IS the credential, and
    these apps also call OpenAI, Cloudinary and each other's token-bearing
    endpoints.
    """
    try:
        if crumb.get("category") in ("httplib", "http", "requests", "urllib3"):
            data = crumb.get("data")
            if isinstance(data, dict):
                if "url" in data:
                    data["url"] = redact_url(data["url"])
                data.pop("http.query", None)
                data.pop("http.fragment", None)
    except Exception:
        return None
    return crumb


def _release():
    """Prefer the 40-char commit SHA.

    Sentry can map stack frames to commits with a SHA and cannot with "v62", and
    concatenating the two ("v62-1c0bcc1") breaks commit association entirely --
    so carry the release number as a tag instead, not in here.

    The SDK's own auto-detection is not usable: it reads HEROKU_BUILD_COMMIT and
    the deprecated HEROKU_SLUG_COMMIT but never HEROKU_RELEASE_VERSION, and it
    tries the local git revision first, so running locally would tag events with
    a laptop's working-tree SHA.
    """
    for var in ("SENTRY_RELEASE", "HEROKU_BUILD_COMMIT",
                "HEROKU_SLUG_COMMIT", "HEROKU_RELEASE_VERSION"):
        val = (os.environ.get(var) or "").strip()
        if val:
            return val
    return None


def _disabled_integrations():
    """Integrations that must never auto-enable.

    All three are inert while tracing is off, because they only decorate spans
    and no spans are created. They are named explicitly anyway, because they are
    the exact route by which manuscripts would start flowing the day somebody
    enables tracing without re-reading this file: the OpenAI and Anthropic
    integrations record prompts and completions, and SQLAlchemy spans carry
    bound parameters -- and on the author portal the proposal text and the
    uploaded file are bound parameters on an INSERT.
    """
    out = []
    for mod, cls in (
        ("sentry_sdk.integrations.openai", "OpenAIIntegration"),
        ("sentry_sdk.integrations.anthropic", "AnthropicIntegration"),
        ("sentry_sdk.integrations.sqlalchemy", "SqlalchemyIntegration"),
    ):
        try:
            out.append(getattr(__import__(mod, fromlist=[cls]), cls)())
        except Exception:
            # An SDK version without one of these is fine; it cannot then
            # auto-enable it either.
            pass
    return out


def init_sentry(app_slug: str, capture_logs_at=None) -> bool:
    """Initialise Sentry. Returns False, having done nothing, without SENTRY_DSN.

    app_slug: the value of the `app` tag on every event -- "website",
        "authors" or "dashboard". Three separate Sentry projects mean this is
        redundant most of the time, and invaluable the one time a DSN is pasted
        into the wrong app's config vars.

    capture_logs_at: the logging level at which a log RECORD becomes its own
        Sentry event, or None to turn that off entirely. It is not the same as
        the level at which records become breadcrumbs, which stays at INFO.

        It is None on the marketing site and only there. That app's 500 handler
        calls logger.exception() on the very exception FlaskIntegration has
        already captured, so leaving this at ERROR would bill two events for
        every 500 against a 5,000/month org-wide pool. The handful of genuinely
        useful .exception() sites there are captured explicitly instead.
    """
    dsn = (os.environ.get("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    import logging

    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration
    from sentry_sdk.scrubber import (
        DEFAULT_DENYLIST,
        DEFAULT_PII_DENYLIST,
        EventScrubber,
    )

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=_release(),

        # --- personal data. Every one of these is load-bearing; see the module
        # --- docstring for what each default actually does.
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        max_value_length=1024,
        event_scrubber=EventScrubber(
            denylist=list(DEFAULT_DENYLIST) + EXTRA_DENYLIST,
            pii_denylist=list(DEFAULT_PII_DENYLIST),
            recursive=True,
        ),
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,

        # --- quota. The whole org shares 5,000 errors a month across four
        # --- projects, so restraint here is not tuning, it is the difference
        # --- between having monitoring in week four and not having it.
        #
        # None disables tracing. 0.0 does NOT -- it still continues an incoming
        # distributed trace and bills the resulting transactions.
        traces_sample_rate=None,
        profiles_sample_rate=None,
        enable_logs=False,
        sample_rate=float(os.environ.get("SENTRY_SAMPLE_RATE", "1.0")),
        max_breadcrumbs=25,
        # A dyno gets ~30s to shut down. Two seconds of flush is a fair trade
        # against delaying every restart.
        shutdown_timeout=2,

        integrations=[
            LoggingIntegration(level=logging.INFO, event_level=capture_logs_at),
        ],
        disabled_integrations=_disabled_integrations(),
    )
    sentry_sdk.set_tag("app", app_slug)
    sentry_sdk.set_tag("sentry_config", CONFIG_VERSION)
    return True
