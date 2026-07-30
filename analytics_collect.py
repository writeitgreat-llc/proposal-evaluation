#!/usr/bin/env python3
"""First-party web analytics for authors.writeitgreat.com — collector + outbox.

This is the second property on the cross-app analytics contract
(`_integration/CONTRACT_ANALYTICS.md` in the team workspace; the sender side is
summarised in this repo's CONTRACTS.md §3). The marketing site collects on
writeitgreat.com, this app collects on authors.writeitgreat.com, both write a
local outbox row, and both ship batches server-to-server to wig-dashboard,
which is the source of truth for everything anyone reads.

It lives in its own module rather than in app.py purely for size: app.py is
already ~8,800 lines. Everything else follows this repo's conventions —
`db.Column` models, naive-UTC datetimes, the FunnelOutboxEvent outbox shape,
and the 2-minute background drain in `_start_reengagement_thread()`.

WHY EVERY DECISION HERE IS THE SAME AS THE MARKETING SITE'S
-----------------------------------------------------------
The two collectors feed one set of tables and one report. A visitor who reads
writeitgreat.com and then clicks through to authors.writeitgreat.com must be
counted the same way on both sides, or the funnel numbers are two different
measurements stacked in one chart. So the hashing construction, the bot regex,
the UA parser, the channel vocabulary, the cookie names and the env-var names
are ported deliberately unchanged from `website/app/analytics.py` and
`website/app/routes/collect.py`, comments included. Where this repo genuinely
differs the divergence is marked `DIVERGENCE:` and says why.

THE PRIVACY ARGUMENT (read before changing anything here)
---------------------------------------------------------
Nothing in this module stores an IP address or a User-Agent string. Both are
read from the live request, folded into values that cannot be reversed (a keyed
hash, a browser family name), and dropped when the request ends. Neither value
is needed to answer "which pages do people read", and we have an EU
establishment.

Two identity modes, chosen by whether the visitor accepted the analytics
cookie:

  baseline (everyone, no consent needed, no storage on the device)
      visitor_hash = blake2b(HMAC(key, utc_date) || site || ip || user_agent)
      The salt input rotates at midnight UTC, so the same person is a different
      hash tomorrow. This is the Plausible/Fathom construction: it measures
      traffic without being able to follow a person across days, which is what
      keeps it outside the ePrivacy consent requirement.

  consented (only after an explicit opt-in)
      visitor_hash = blake2b(HMAC(key, "stable") || site || cookie_id)
      Stable for the life of the cookie, so returning visitors become
      answerable.

Both modes emit the same field. `consented` on the event says which mode
produced it, and any report that depends on the stable mode has to show the
covered fraction rather than pretending it saw everyone.

The key comes from ANALYTICS_SALT_KEY — a stored secret, deliberately NOT a
per-process `os.urandom`. This app runs a single gunicorn worker with 4
threads, so a process-local salt would look correct and would silently start
double-counting every visitor the moment anyone set a second worker.

SCOPE — PUBLIC FUNNEL PAGES ONLY
--------------------------------
Only the pages a prospective author sees BEFORE they have an account are
instrumented: register, login, forgot/reset password, the confidentiality
statement, and the standalone social-strategy tool. The signed-in application
interior (author dashboard, coaching, proposal editor, publisher portal, admin)
is NOT instrumented. Measuring the behaviour of signed-in, identified users
inside a product is a materially different thing from anonymous marketing
measurement — different privacy footing, different notice requirements — and it
is deliberately out of scope.

That boundary is enforced structurally, not by a list of paths:
  * `templates/base_public.html` is the ONLY template that includes the tracker
    and the consent banner, and
  * it renders them only when `current_user.is_authenticated` is false.
A page is therefore instrumented if and only if it extends `base_public.html`
AND nobody is signed in. `ci/check_analytics.py` asserts both halves.

NOTHING HERE IS EVER A SECRET IN SOURCE
---------------------------------------
This repo is PUBLIC on GitHub. ANALYTICS_SALT_KEY and ANALYTICS_INGEST_TOKEN
come from `os.environ` and have no defaults; unset means "analytics is not
configured on this deployment", which is a silent no-op, never an error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlsplit

import requests as http_requests
from flask import Blueprint, current_app, jsonify, make_response, request

analytics_bp = Blueprint('analytics', __name__)

# Filled in by init_app(); see the note there for why the models cannot be
# defined at import time.
db = None
AnalyticsVisit = None
AnalyticsOutboxEvent = None


# ===========================================================================
# CONFIGURATION — env only, no defaults that could ever be a secret
# ===========================================================================

# The dashboard host is already committed in app.py (FUNNEL_EVENTS_URL), so
# naming it here leaks nothing new.
_DEFAULT_INGEST_URL = (
    'https://wig-dashboard-5276957fea18.herokuapp.com/api/analytics/ingest')


def _ingest_url() -> str:
    """The ingest endpoint, refusing to put a bearer token on a cleartext wire.

    The override exists for staging and for tests. A mistyped `http://` value
    would send ANALYTICS_INGEST_TOKEN in the clear on every flush, so a
    non-https override is ignored rather than honoured — failing to the known
    good URL is strictly safer than failing to the attacker's.
    """
    url = (os.environ.get('ANALYTICS_INGEST_URL') or '').strip()
    if not url:
        return _DEFAULT_INGEST_URL
    parsed = urlparse(url)
    if parsed.scheme == 'https' or parsed.hostname in ('localhost', '127.0.0.1'):
        return url
    return _DEFAULT_INGEST_URL


# Read through _ingest_url() at call time, not frozen here: ingest_token() is
# already a function for the same reason, and a constant captured at import
# cannot be overridden by a test or by a config change without a redeploy.

# Fallback batch label, only used if an outbox row somehow has no site.
DEFAULT_SITE = 'authors.writeitgreat.com'

# A visit ends after this long with no events. 30 minutes is the near-universal
# convention (GA, Plausible, Matomo); the number matters less than everyone
# using the same one when comparing figures.
VISIT_IDLE_TIMEOUT = timedelta(minutes=30)

# Cookie names, identical to the marketing site's so the two properties present
# the same choice to the same person. `wig_consent` records the CHOICE and is
# strictly necessary — without it we would have to ask again on every page,
# which is worse for the visitor than remembering the answer. `wig_vid` is the
# analytics identifier and only ever exists after an explicit opt-in.
CONSENT_COOKIE = 'wig_consent'
VISITOR_COOKIE = 'wig_vid'
CONSENT_MAX_AGE = 180 * 24 * 3600
VISITOR_MAX_AGE = 180 * 24 * 3600

MAX_BEACON_BYTES = 16 * 1024
MAX_EVENTS_PER_BEACON = 20
EVENT_KINDS = ('pageview', 'engagement', 'outbound', 'conversion')

# DIVERGENCE from the marketing site: it parks a row after 8 failed attempts,
# because its flush runs from the Heroku Scheduler. This app drains every ~2
# minutes from the existing background thread, so 8 attempts would burn the
# whole allowance during a 20-minute dashboard deploy. 100 attempts against the
# 30-minute backoff cap is ~2 days, the same intent as FUNNEL_OUTBOX_MAX_ATTEMPTS
# in app.py: outlive receiver deploy lag, then park rather than block the queue.
ANALYTICS_OUTBOX_MAX_ATTEMPTS = 100
ANALYTICS_OUTBOX_BATCH = 200

# Hard ceiling on the queue. The outbox is a BUFFER, not a store: the drain
# runs every couple of minutes and normally leaves it near empty, so anything
# approaching this means the dashboard has been unreachable for a long time or
# something is abusing the endpoint.
#
# This database holds authors, submissions, proposals and contracts. Dropping
# analytics events at the ceiling is the right trade; filling the disk the
# author funnel runs on is not. Ported from the website's MAX_OUTBOX_ROWS,
# which did not survive the original copy.
MAX_OUTBOX_ROWS = 50_000

# Rechecked at most this often rather than on every beacon — a COUNT(*) per
# pageview is a silly amount of work for an answer that changes slowly.
_CEILING_CHECK_INTERVAL = 60  # seconds
_ceiling_state = {'checked_at': 0.0, 'full': False}
_ceiling_lock = threading.Lock()

# Age at which a queued event is dropped regardless of how it got stuck. The
# retention the consent banner implies has to hold on the one copy this app
# controls, and — unlike the parked-row prune below — this one does not depend
# on a row ever having been ATTEMPTED. With ANALYTICS_INGEST_TOKEN unset the
# drain returns before attempting anything, so attempts stays 0 forever and a
# parked-only prune can never reach those rows.
ANALYTICS_OUTBOX_MAX_AGE_DAYS = 90
# Parked (poison) rows are dropped after a week. The marketing site keeps them
# forever — deleting evidence of a bug is how bugs stay hidden — but a pageview
# outbox is high volume, and an unbounded poison queue on a hobby-tier Postgres
# is a worse failure than losing a week-old stuck row. Same 7 days as
# FUNNEL_OUTBOX_PRUNE_AFTER_DAYS.
ANALYTICS_OUTBOX_PARK_PRUNE_DAYS = 7


def ingest_token() -> str:
    """The bearer token for the dashboard, or '' when not configured.

    Read at call time rather than at import, so CI can toggle it and so a
    Heroku config change takes effect on restart without a code path that
    caches an empty string forever.

    THE ENV VAR IS NAMED `ANALYTICS_INGEST_TOKEN` ON BOTH SIDES. Not a
    sender/receiver name pair: LEADS_FORWARD_TOKEN vs LEADS_INGEST_TOKEN is why
    the leads link sat dead in production for days — each side looked correctly
    configured in isolation. One name, both apps.
    """
    return os.environ.get('ANALYTICS_INGEST_TOKEN', '')


def salt_key():
    """The HMAC key, or None when analytics is not configured.

    Returning None rather than falling back to a default is deliberate: a
    hard-coded default key would make every deployment's hashes identical and
    trivially reversible by anyone with the source — and the source of this
    repo is public. Unconfigured means the collector answers 204 and stores
    nothing.
    """
    key = os.environ.get('ANALYTICS_SALT_KEY')
    return key.encode('utf-8') if key else None


def is_configured() -> bool:
    """Whether to render the tracker at all. No key → no beacons worth sending."""
    return salt_key() is not None


# ===========================================================================
# IDENTITY
# ===========================================================================

def _daily_salt(key, day):
    return hmac.new(key, day.encode('ascii'), hashlib.sha256).digest()


def visitor_hash(key, site, ip=None, user_agent=None, stable_id=None, now=None):
    """32 hex chars identifying this visitor, in one of the two modes above.

    `stable_id` (the consented cookie value) wins when present. Neither the IP
    nor the User-Agent can be recovered from the result without the key, and in
    baseline mode not even with it, once the day has rolled over.
    """
    if stable_id:
        salt = hmac.new(key, b'stable', hashlib.sha256).digest()
        material = stable_id.encode('utf-8')
    else:
        moment = now or datetime.utcnow()
        salt = _daily_salt(key, moment.strftime('%Y-%m-%d'))
        material = f"{ip or ''}|{user_agent or ''}".encode('utf-8')
    digest = hashlib.blake2b(
        salt + site.encode('utf-8') + b'|' + material, digest_size=16)
    return digest.hexdigest()


def trusts_cloudflare() -> bool:
    """Whether `CF-Connecting-IP` on this deployment can be believed.

    OFF by default, and that default is the security-relevant part. When
    Cloudflare is in front of the app it overwrites this header on every
    request, so it is authoritative. When Cloudflare is *not* in front — which
    is the case today — the header is just something any client can type, and
    trusting it unconditionally would hand every visitor an unlimited allowance
    on the beacon throttle simply by varying a header, and let them forge or
    fragment their own visitor_hash at will.

    Set `TRUST_CLOUDFLARE_IP=1` as part of the DNS cutover, in the same change
    as the proxy going live — not before.
    """
    return os.environ.get('TRUST_CLOUDFLARE_IP', '').strip().lower() in (
        '1', 'true', 'yes', 'on')


def client_ip():
    """The visitor's IP, for hashing and throttling only — never stored, never logged.

    app.py already wraps the WSGI app in ProxyFix(x_for=1), so on Heroku
    `request.remote_addr` is the real client rather than the router. Behind
    Cloudflare that stops being true — every visitor in the world would collapse
    into one visitor_hash and one throttle bucket, with no error anywhere to
    notice — which is what the gate above exists for.
    """
    if trusts_cloudflare():
        cf = request.headers.get('CF-Connecting-IP')
        if cf:
            return cf.strip()[:64] or None
    return request.remote_addr


# ===========================================================================
# BOTS
# ===========================================================================

# Deliberately broad. Bot rows are FLAGGED, never dropped (see the contract),
# so a false positive costs a row in the "excluded" bucket that can be
# reclassified later — whereas a missed bot silently inflates every number on
# the page and there is no way to tell after the fact.
_BOT_UA = re.compile(
    r"bot|crawl|spider|slurp|search|fetch|scrape|curl|wget|python-requests|"
    r"headless|phantom|puppeteer|playwright|selenium|lighthouse|pagespeed|"
    r"monitor|uptime|pingdom|preview|facebookexternalhit|whatsapp|telegram|"
    r"slackbot|discordbot|linkedinbot|twitterbot|embedly|quora link|"
    r"semrush|ahrefs|mj12|dotbot|petal|bytespider|gptbot|claudebot|ccbot|"
    r"perplexity|applebot|amazonbot|dataprovider|zoominfo",
    re.I,
)


def is_bot(user_agent, purpose=None) -> bool:
    """True for traffic that should not count as a person reading the site.

    A pure function: the caller passes the fetch-purpose header rather than
    this reading it off `request`. Half-pure classifiers that reach for the
    request context on some paths cannot be tested against a list of real
    user-agent strings, which is the only way to know a broad bot regex is not
    quietly eating real readers.
    """
    if not user_agent or len(user_agent) < 12:
        # Real browsers always send a substantial UA. An empty one is a script.
        return True
    if _BOT_UA.search(user_agent):
        return True
    # Chrome's prerender / prefetch of a link the visitor never clicked.
    hint = (purpose or '').lower()
    return 'prefetch' in hint or 'prerender' in hint


def fetch_purpose():
    """The current request's prefetch hint, under any of its three header names."""
    return (request.headers.get('Sec-Purpose')
            or request.headers.get('Purpose')
            or request.headers.get('X-Purpose'))


# ===========================================================================
# USER-AGENT PARSING
# ===========================================================================
#
# Hand-rolled rather than a dependency, and that also keeps
# ci/check_undeclared_imports.py quiet: a UA parser library is a rolling
# signature database — it needs updating to stay accurate, and an out-of-date
# one is worse than a coarse one because it looks precise. We only ever report
# these as ranked tables, where "Chrome / Windows / desktop" is the whole
# answer, so coarse and stable beats precise and stale. Order matters in both
# lists: the impostors come first.

_BROWSERS = (
    ('Edge', re.compile(r"\bEdgA?e?/", re.I)),
    ('Opera', re.compile(r"\bOPR/|\bOpera", re.I)),
    ('Samsung Internet', re.compile(r"SamsungBrowser", re.I)),
    ('Chrome', re.compile(r"\bChrome/|\bCriOS/", re.I)),
    ('Firefox', re.compile(r"\bFirefox/|\bFxiOS/", re.I)),
    # Last: every browser above also says "Safari" in its UA string.
    ('Safari', re.compile(r"\bSafari/", re.I)),
)

_OSES = (
    ('iOS', re.compile(r"iPhone|iPad|iPod|\biOS\b", re.I)),
    ('Android', re.compile(r"Android", re.I)),
    ('macOS', re.compile(r"Mac OS X|Macintosh", re.I)),
    ('Windows', re.compile(r"Windows NT|Windows", re.I)),
    ('Linux', re.compile(r"Linux|X11", re.I)),
)

_TABLET = re.compile(r"iPad|Tablet|PlayBook|Silk|Kindle", re.I)
_MOBILE = re.compile(r"Mobi|iPhone|iPod|Windows Phone", re.I)
_ANDROID = re.compile(r"Android", re.I)


def device_type(ua) -> str:
    """mobile | tablet | desktop.

    Written as explicit steps rather than one clever regex. The concise version
    of the Android rule is a pair of lookarounds inside an alternation, which
    happens to give the right answer only because real Android user-agents put
    "Android" before "Mobile" — correct by accident is not a property worth
    keeping in a classifier nobody will look at again.
    """
    if _TABLET.search(ua):
        return 'tablet'
    # Android's own convention: a phone says "Mobile", a tablet does not.
    if _ANDROID.search(ua):
        return 'mobile' if re.search(r"Mobile", ua, re.I) else 'tablet'
    if _MOBILE.search(ua):
        return 'mobile'
    return 'desktop'


def parse_user_agent(user_agent):
    """Browser family, OS family and device class. The UA itself is discarded."""
    ua = user_agent or ''
    browser = next((name for name, rx in _BROWSERS if rx.search(ua)), None)
    os_name = next((name for name, rx in _OSES if rx.search(ua)), None)
    return {'browser': browser, 'os': os_name, 'device_type': device_type(ua)}


def country():
    """ISO-3166-1 alpha-2 country, or None when we genuinely don't know.

    Read from Cloudflare's `CF-IPCountry` header — free, accurate, and already
    computed at the edge. There is deliberately no GeoLite2 database here:
    shipping a 6 MB binary and a MaxMind licence obligation to answer "which
    countries read us" is not a trade worth making, and a stale IP database
    reports confident wrong answers.

    Returns None until Cloudflare is actually in front of this site. Country
    simply reads as "Unknown" on the dashboard until then, and starts
    populating on its own afterwards with no code change. "XX" is Cloudflare's
    own value for "couldn't tell", and T1 is Tor — both mean unknown.
    """
    value = (request.headers.get('CF-IPCountry') or '').strip().upper()
    if len(value) != 2 or value in ('XX', 'T1'):
        return None
    return value


# ===========================================================================
# PATHS AND REFERRERS
# ===========================================================================

# Click-tracking params that would otherwise shatter the top-pages table into
# thousands of one-visit rows. Named here because they arrive on the PATH we
# store, not only in the campaign object; the query string is dropped wholesale
# below, which covers all of them and everything like them.
_TRACKING_PARAMS = ('gclid', 'fbclid', 'msclkid', 'ttclid', 'igshid', 'mc_eid')

# A path segment that is almost certainly a credential rather than a page name:
# a long hex string, or a long opaque token that contains a digit. This is the
# BACKSTOP behind route normalisation below, not the primary mechanism —
# ordinary page segments ("author-coaching-quickstart") contain no digits and
# never trip it.
_HEX_SEGMENT = re.compile(r"^[0-9a-fA-F]{16,}$")
_TOKEN_SEGMENT = re.compile(r"^(?=[^/]*\d)[A-Za-z0-9_-]{20,}$")


def clean_path(raw):
    """A stored path: absolute, query-free, length-capped.

    The query string is dropped wholesale rather than filtered. Campaign params
    are captured as their own fields, and anything else in a URL is either
    noise or something we should not be keeping — a password-reset token in a
    shared link, say.
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.replace('\x00', '').strip()
    if not text:
        return None
    # Accept a full URL or a bare path; keep only the path component, so a
    # client cannot make its own pageviews look like they happened elsewhere.
    parts = urlsplit(text)
    path = parts.path or '/'
    if not path.startswith('/'):
        return None
    # Collapse a trailing slash so /confidentiality and /confidentiality/ are
    # one row.
    if len(path) > 1 and path.endswith('/'):
        path = path[:-1]
    return path[:200]


def normalise_route(path):
    """Reduce a concrete path to the Flask URL rule that serves it.

    DIVERGENCE from the marketing site, and the reason for it matters. That
    site has no tokenised URLs, so it stores `location.pathname` verbatim. This
    app does: `/author/reset-password/<token>` is one of the pages in scope,
    and `/results/<submission_id>` is a capability URL. Storing the literal path
    would put a live password-reset token into the analytics store and then
    ship it to another app — the exact thing clean_path's docstring says we
    must not keep.

    Asking Flask's own url_map is the structural fix: it cannot drift from the
    routing table, and it makes the top-pages report read
    `/author/reset-password/<token>` — one row, which is the number anyone
    actually wants. A path that matches no rule (a 404, or a forged beacon) is
    left alone apart from the token backstop.
    """
    if not path:
        return path
    try:
        adapter = current_app.url_map.bind('localhost')
        rule, _args = adapter.match(path, method='GET', return_rule=True)
        return str(rule.rule)[:200]
    except Exception:
        # NotFound / MethodNotAllowed / RequestRedirect / no app context.
        return _redact_token_segments(path)


def _redact_token_segments(path):
    """Replace anything in a path that looks like a credential.

    Only reached when the path matched no route, so in practice this catches
    hand-typed or forged URLs. Cheap insurance in a repo that serves password
    resets and 2FA enrolment.
    """
    out = []
    for segment in path.split('/'):
        if _HEX_SEGMENT.match(segment) or _TOKEN_SEGMENT.match(segment):
            out.append('<redacted>')
        else:
            out.append(segment)
    return '/'.join(out)[:200]


def referrer_host(raw):
    """The host of an external referrer, or None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = urlparse(raw.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in ('http', 'https'):
        return None
    host = parsed.netloc.lower().split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host[:120] or None


def campaign_fields(raw):
    """utm_* values from the beacon, clamped. Unknown keys are ignored."""
    out = {}
    for key in ('utm_source', 'utm_medium', 'utm_campaign'):
        value = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(value, str) and value.strip():
            out[key] = value.replace('\x00', '').strip()[:120]
    return out


def is_internal(referrer, host) -> bool:
    """True when the referrer is this same site — a previous page, not a source."""
    if not referrer:
        return False
    ref_host = referrer_host(referrer)
    if not ref_host or not host:
        return False
    site = host.lower().split(':', 1)[0]
    if site.startswith('www.'):
        site = site[4:]
    return ref_host == site


# ===========================================================================
# CHANNEL CLASSIFIER
# ===========================================================================
#
# DIVERGENCE, forced: the contract says `channel` must use "the same vocabulary
# and the same classifier as lead attribution — website/app/source_capture.py".
# These are separate repos with no shared package, so the classifier is ported
# here verbatim rather than imported. That is a real duplication and it has to
# be kept in step: a new social network added on one side and not the other
# makes "which channel produces traffic" and "which channel produces leads"
# stop being comparable, which is the whole reason the contract names one
# classifier. Change one, change both.

SEARCH_HOSTS = (
    'google.', 'bing.', 'duckduckgo.', 'yahoo.', 'ecosia.', 'brave.',
    'startpage.', 'qwant.', 'baidu.', 'yandex.', 'search.marginalia.',
)
SOCIAL_HOSTS = (
    'linkedin.', 'lnkd.in', 'facebook.', 'fb.com', 'instagram.', 'threads.',
    'twitter.', 'x.com', 't.co', 'reddit.', 'youtube.', 'youtu.be',
    'tiktok.', 'pinterest.', 'substack.', 'medium.', 'bsky.', 'mastodon.',
    'news.ycombinator.',
)
EMAIL_HOSTS = ('mail.google.', 'outlook.', 'mail.yahoo.', 'superhuman.', 'hey.com')

PAID_MEDIUMS = {
    'cpc', 'ppc', 'paid', 'paidsearch', 'paid_search', 'paid-search',
    'paidsocial', 'paid_social', 'paid-social', 'display', 'banner',
    'cpm', 'retargeting', 'remarketing',
}
EMAIL_MEDIUMS = {'email', 'e-mail', 'newsletter', 'mail'}
SOCIAL_MEDIUMS = {'social', 'social-organic', 'social_organic', 'organic-social'}

CAMPAIGN_KEYS = ('utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term')

# One label per bucket, used by both the lead views and the analytics rollups.
CHANNELS = ('paid', 'email', 'social', 'organic search', 'referral', 'campaign', 'direct')


def _channel_host(referrer):
    if not referrer:
        return None
    try:
        host = urlparse(referrer).netloc.lower()
    except ValueError:
        return None
    if not host:
        return None
    if host.startswith('www.'):
        host = host[4:]
    return host.split(':', 1)[0] or None


def _matches(host, needles) -> bool:
    if not host:
        return False
    return any(n in host for n in needles)


def classify(source) -> str:
    """Which acquisition channel this visit arrived through.

    Order matters and encodes precedence: an explicit paid signal beats a
    referrer, because a paid Google click and an organic Google click carry the
    same referrer and only the click id or utm_medium tells them apart.
    """
    medium = (source.get('utm_medium') or '').lower()
    host = _channel_host(source.get('referrer'))

    if source.get('click_id') or medium in PAID_MEDIUMS:
        return 'paid'
    if medium in EMAIL_MEDIUMS or _matches(host, EMAIL_HOSTS):
        return 'email'
    if medium in SOCIAL_MEDIUMS or _matches(host, SOCIAL_HOSTS):
        return 'social'
    if _matches(host, SEARCH_HOSTS):
        return 'organic search'
    if host:
        return 'referral'
    # A campaign tag with no referrer at all: the QR code, the printed card,
    # the link pasted into a DM. Worth separating from true direct traffic —
    # somebody tagged that link on purpose.
    if any(source.get(key) for key in CAMPAIGN_KEYS):
        return 'campaign'
    # No referrer, no tags. Mostly NOT people typing the URL: app browsers,
    # link previews, and every https->http hop send no referrer at all.
    return 'direct'


def channel_for(referrer, campaign) -> str:
    return classify({'referrer': referrer, **campaign})


# ===========================================================================
# MODELS
# ===========================================================================

def _define_models(database):
    """Declare the two tables against app.py's `db`.

    They are built here rather than at module import because app.py owns the
    SQLAlchemy instance and this module must NOT import app.py: that is a
    circular import, and worse, `python app.py` runs the file as `__main__`, so
    `from app import db` would import and execute the entire 8,800-line app a
    SECOND time — every model registered twice, every route registered twice,
    two background threads.

    app.py calls init_app() before its own `db.create_all()`, so these tables
    are created by the normal release path (migrate.py). This repo has no
    Alembic; new TABLES arrive via create_all() and only new COLUMNS on
    existing tables need an entry in run_migrations(). Both of these are new
    tables, so run_migrations() needs nothing.
    """

    class AnalyticsVisit(database.Model):
        """Server-side session state for one visitor, so the browser stores nothing.

        The tracker beacon carries no session id. It can't: a session id in
        localStorage or sessionStorage is device storage, which is exactly what
        the baseline tier promises not to use. So the server keeps the mapping
        instead, keyed by the rotating visitor_hash, and mints a new session
        when the last one went quiet.

        This table is a short-lived working set, not a record. visitor_hash
        rotates at midnight UTC, so yesterday's rows are already unreachable;
        prune_analytics_visits() deletes anything untouched for a day and a
        half. Nothing here identifies a person.
        """
        __tablename__ = 'analytics_visit'

        id = database.Column(database.Integer, primary_key=True)
        visitor_hash = database.Column(database.String(32), nullable=False, index=True)
        site = database.Column(database.String(120), nullable=False)
        session_id = database.Column(database.String(40), nullable=False)
        started_at = database.Column(database.DateTime, default=datetime.utcnow,
                                     nullable=False)
        last_seen_at = database.Column(database.DateTime, default=datetime.utcnow,
                                       nullable=False, index=True)
        # Set on the first pageview of the visit and never rewritten — the
        # source of a visit is where it STARTED, not the last page that linked
        # onward.
        entry_path = database.Column(database.String(200))
        channel = database.Column(database.String(40))
        referrer_host = database.Column(database.String(120))
        utm_source = database.Column(database.String(120))
        utm_medium = database.Column(database.String(120))
        utm_campaign = database.Column(database.String(120))

        @property
        def is_stale(self):
            last = self.last_seen_at
            if last is None:
                return True
            return datetime.utcnow() - last > VISIT_IDLE_TIMEOUT

    class AnalyticsOutboxEvent(database.Model):
        """One analytics event, waiting to be shipped to the dashboard.

        Exactly the FunnelOutboxEvent pattern (app.py): a persistent row written
        inside the request, a retry drain with exponential backoff, idempotent
        on an id this side mints, and a silent no-op when the token env var is
        unset.

        The dashboard is the source of truth for analytics; this table is a
        buffer in front of it, for two reasons. A pageview must not put an HTTP
        call to another host on the critical path of a page render — this app
        runs one gunicorn worker — and a dashboard outage must cost zero
        pageviews rather than silently losing a day of traffic.

        `event_uid` is minted here and is what makes the ingest idempotent: a
        batch that times out after the dashboard committed it is re-sent,
        recognised, and counted as a duplicate rather than doubling somebody's
        traffic.

        `body_json` is the event exactly as it will be sent, already validated
        and shaped by POST /e. Stored as JSON text rather than columns because
        this table is a queue, not a model of anything — the dashboard owns the
        schema, and a contract addition should not need a migration here.
        """
        __tablename__ = 'analytics_outbox'

        id = database.Column(database.Integer, primary_key=True)
        event_uid = database.Column(database.String(40), unique=True,
                                    nullable=False, index=True)
        # The host the event was collected on. A COLUMN rather than a key inside
        # body_json: the ingest contract puts `site` at the batch level, not on
        # the event, so the drain has to be able to group by it without
        # inventing a field the receiver does not expect.
        site = database.Column(database.String(120), nullable=False, index=True)
        body_json = database.Column(database.Text, nullable=False)
        created_at = database.Column(database.DateTime, default=datetime.utcnow,
                                     nullable=False, index=True)
        attempts = database.Column(database.Integer, default=0, nullable=False)
        next_attempt_at = database.Column(database.DateTime, nullable=True)
        last_error = database.Column(database.String(300), nullable=True)

    return AnalyticsVisit, AnalyticsOutboxEvent


def init_app(flask_app, database):
    """Declare the models and register the blueprint. Called once, from app.py."""
    global db, AnalyticsVisit, AnalyticsOutboxEvent
    db = database
    if AnalyticsVisit is None:
        AnalyticsVisit, AnalyticsOutboxEvent = _define_models(database)
    if 'analytics' not in flask_app.blueprints:
        flask_app.register_blueprint(analytics_bp)
    return AnalyticsVisit, AnalyticsOutboxEvent


# ===========================================================================
# REQUEST HELPERS
# ===========================================================================

def site_name() -> str:
    """Which property this event belongs to.

    Read from the Host header rather than configured, so the same code serves
    authors.writeitgreat.com, the Heroku holding domain, and localhost without a
    config var per environment — and so a DNS change needs no deploy.
    """
    host = (request.host or '').lower().split(':', 1)[0]
    return (host[4:] if host.startswith('www.') else host)[:120] or 'unknown'


def gpc_opt_out() -> bool:
    """True when the browser is sending a Global Privacy Control signal.

    GPC is a legally recognised opt-out in California and several other US
    states, and honouring it is the whole point of it existing. We treat it as
    a standing refusal of the cookie tier: the identifier is never set, and the
    banner is never shown — asking someone who has already answered "no" at the
    browser level is exactly the dark pattern the signal exists to stop.
    """
    return (request.headers.get('Sec-GPC') == '1'
            or (request.headers.get('DNT') == '1' and _dnt_honoured()))


def _dnt_honoured() -> bool:
    """Do Not Track is deprecated and widely ignored; we honour it anyway.

    It costs nothing — the population sending DNT is small and self-selected —
    and there is no defensible reason to receive "please don't track me" and
    decide it doesn't count because the spec lost momentum.
    """
    return True


def consent_state() -> str:
    """'granted', 'denied', or 'unset' for this request."""
    if gpc_opt_out():
        return 'denied'
    value = request.cookies.get(CONSENT_COOKIE)
    return value if value in ('granted', 'denied') else 'unset'


def show_consent_banner() -> bool:
    """Only ask someone who has not already answered, one way or another."""
    return consent_state() == 'unset'


def _secure_cookies() -> bool:
    """Secure flag on, except on plain-HTTP local development.

    `request.is_secure` is the right signal rather than app.py's `_is_production`
    flag: ProxyFix(x_proto=1) is what makes this read True behind Heroku's
    router, and a cookie marked Secure on localhost would simply never be set,
    which would make the consent banner untestable locally.
    """
    return request.is_secure


def _set_cookie(response, name, value, max_age):
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=_secure_cookies(),
        # Lax, not Strict: Strict would drop the cookie on the first click in
        # from an external link — precisely the visit we most want to attribute,
        # because on this app almost every visit arrives from writeitgreat.com.
        # Lax still blocks it on cross-site POSTs, which is the CSRF-relevant
        # case.
        samesite='Lax',
        path='/',
    )


# ===========================================================================
# THROTTLE
# ===========================================================================
#
# Same shape as app.py's _check_rate_limit for /api/submit, kept local so the
# beacon's far more generous allowance cannot be confused with the submission
# limit. Generous on purpose: one visit legitimately sends a beacon per page
# plus one on each tab-hide. This exists to stop a script hammering the
# endpoint, not to police a reader. Never persisted; prunes as it goes, so the
# dict cannot grow past the IPs seen in the last window.

_BEACON_HITS = {}
_BEACON_WINDOW = 600      # seconds
_BEACON_MAX_PER_WINDOW = 60
_BEACON_LOCK = threading.Lock()


def _rate_limited(ip) -> bool:
    """True when this IP has had its allowance.

    DIVERGENCE from the marketing site: a lock. This app runs gunicorn with
    `--threads 4`, so two beacons really can mutate this dict at the same time;
    the marketing site is single-threaded. A dict mutation under threads is not
    a crash risk in CPython but the prune-then-append read-modify-write is, and
    a throttle that occasionally forgets its own state is not a throttle.
    """
    now = time.monotonic()
    cutoff = now - _BEACON_WINDOW
    with _BEACON_LOCK:
        for stale in [k for k, hits in _BEACON_HITS.items()
                      if not hits or hits[-1] < cutoff]:
            del _BEACON_HITS[stale]

        hits = [t for t in _BEACON_HITS.get(ip, []) if t >= cutoff]
        if len(hits) >= _BEACON_MAX_PER_WINDOW:
            _BEACON_HITS[ip] = hits
            return True
        hits.append(now)
        _BEACON_HITS[ip] = hits
        return False


# ===========================================================================
# CONSENT ENDPOINT
# ===========================================================================

@analytics_bp.route('/consent', methods=['POST'])
def set_consent():
    """Record the visitor's analytics choice, server-side.

    Deliberately public and unauthenticated — an anonymous visitor with no
    session and no privileges to borrow. The worst a forged cross-site POST
    achieves is changing an analytics preference the visitor can change back in
    one click. (This app has no CSRF extension installed, so unlike the
    marketing site there is no `@csrf.exempt` to add; the reasoning is recorded
    here so nobody later "fixes" it by adding one.)

    The identifier is set with `Set-Cookie` from here rather than by
    JavaScript, and that is load-bearing rather than stylistic. Safari's
    Intelligent Tracking Prevention caps the lifetime of any cookie written by
    `document.cookie` at seven days, so a JS-set identifier would quietly
    deliver one week of returning-visitor data instead of the 180 days the
    consent text promises. HttpOnly also means an XSS on this site cannot read
    the identifier back out — which matters more here than on the marketing
    site, because this app serves logged-in accounts on the same origin.
    """
    if request.content_length is None or request.content_length > 2 * 1024:
        return jsonify(ok=False), 413
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(ok=False), 400

    granted = payload.get('analytics') is True and not gpc_opt_out()
    response = make_response(jsonify(ok=True, analytics=granted))
    _set_cookie(response, CONSENT_COOKIE,
                'granted' if granted else 'denied', CONSENT_MAX_AGE)

    if granted:
        # A fresh opaque identifier. It is random, not derived from anything
        # about the person, and it is the only thing on the device that could
        # link two visits.
        if not request.cookies.get(VISITOR_COOKIE):
            _set_cookie(response, VISITOR_COOKIE, uuid.uuid4().hex, VISITOR_MAX_AGE)
    else:
        # Withdrawal has to actually delete the identifier, not just stop
        # reading it. Anything less makes the "you can change your mind" line in
        # the banner untrue.
        response.delete_cookie(VISITOR_COOKIE, path='/')
    return response


# ===========================================================================
# THE BEACON
# ===========================================================================

def _no_content():
    """204 with no body. Every outcome a visitor can cause looks like this.

    A beacon has nobody to show an error to, and a tracker that behaves
    differently on success is a tracker that leaks information about the server.
    """
    response = make_response('', 204)
    response.headers['Cache-Control'] = 'no-store'
    return response


@analytics_bp.route('/e', methods=['POST'])
def collect():
    """The analytics beacon. Same-origin, unauthenticated, always 204.

    Deliberately public: it is called by every visitor before they have an
    account, which is the entire point. Hardened on the same pattern as
    /api/submit — size cap checked before the body is parsed, per-IP sliding
    window, no IP written anywhere.

    The path is deliberately bland. A beacon to app.writeitgreat.com would be
    both slower (a CORS preflight fired during page unload, which sendBeacon
    cannot avoid because it cannot set headers) and less reliable (a collector
    on another host is what tracker-blockers match on).
    """
    key = salt_key()
    if key is None:
        # Analytics not configured on this deployment: accept and discard, so a
        # missing config var is a quiet no-op rather than a wave of errors.
        return _no_content()

    if request.content_length is None or request.content_length > MAX_BEACON_BYTES:
        return _no_content()

    user_agent = request.headers.get('User-Agent')
    ip = client_ip()
    if _rate_limited(ip or 'unknown'):
        return _no_content()

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _no_content()
    raw_events = payload.get('events')
    if not isinstance(raw_events, list) or not raw_events:
        return _no_content()

    # Shed load rather than fill the database the author funnel runs on.
    if _outbox_full():
        return _no_content()

    site = site_name()
    consented = consent_state() == 'granted'
    stable_id = request.cookies.get(VISITOR_COOKIE) if consented else None
    who = visitor_hash(key, site, ip=ip, user_agent=user_agent, stable_id=stable_id)
    device = parse_user_agent(user_agent)
    bot = is_bot(user_agent, fetch_purpose())
    where = country()
    now = datetime.utcnow()

    try:
        visit = _current_visit(who, site, now)
        stored = 0

        for raw in raw_events[:MAX_EVENTS_PER_BEACON]:
            if not isinstance(raw, dict):
                continue
            kind = raw.get('t')
            if kind not in EVENT_KINDS:
                continue

            path = normalise_route(clean_path(raw.get('u')))
            if kind == 'pageview' and not path:
                continue

            # Referrer and campaign only mean anything on the first pageview of
            # a visit; afterwards they are the previous page of this same visit.
            if visit.entry_path is None and kind == 'pageview':
                referrer = raw.get('r') if isinstance(raw.get('r'), str) else None
                if is_internal(referrer, request.host):
                    referrer = None
                campaign = campaign_fields(raw.get('q') or {})
                visit.entry_path = path
                visit.referrer_host = referrer_host(referrer)
                visit.channel = channel_for(referrer, campaign)
                visit.utm_source = campaign.get('utm_source')
                visit.utm_medium = campaign.get('utm_medium')
                visit.utm_campaign = campaign.get('utm_campaign')

            event = {
                'event_uid': uuid.uuid4().hex,
                'kind': kind,
                'occurred_at': now.isoformat() + 'Z',
                'visitor_hash': who,
                'session_id': visit.session_id,
                'consented': consented,
                'path': path,
                'referrer_host': visit.referrer_host,
                'channel': visit.channel,
                'utm_source': visit.utm_source,
                'utm_medium': visit.utm_medium,
                'utm_campaign': visit.utm_campaign,
                'country': where,
                'device_type': device['device_type'],
                'browser': device['browser'],
                'os': device['os'],
                'is_bot': bot,
                'engaged_ms': _bounded_int(raw.get('ms'), 0, 6 * 3600 * 1000),
                'scroll_pct': _bounded_int(raw.get('sd'), 0, 100),
                'target': _target(raw, kind),
                # Which page LOAD produced this event. See the note in
                # templates/_analytics.html: without it the dashboard falls
                # back to per-path bucketing and this property reports engaged
                # time by a different rule than the marketing site.
                'page_load_id': _load_id(raw.get('pid')),
                'is_entry': path is not None and path == visit.entry_path,
                'is_exit': False,
            }
            db.session.add(AnalyticsOutboxEvent(
                event_uid=event['event_uid'],
                site=site,
                body_json=json.dumps(event),
            ))
            stored += 1

        if stored:
            visit.last_seen_at = now
            db.session.commit()
        else:
            db.session.rollback()
    except Exception as exc:
        # A tracker must never be the reason a request 500s, and the visitor
        # has nobody to tell anyway.
        db.session.rollback()
        current_app.logger.warning('Analytics collect error: %s', exc)
    return _no_content()


def _load_id(value):
    """The client's page-load token, clamped to something storable.

    Alphanumerics only: it arrives from an unauthenticated endpoint and is only
    ever a grouping key.
    """
    if not isinstance(value, str):
        return None
    text = ''.join(c for c in value if c.isalnum())[:32]
    return text or None


def _bounded_int(value, low, high):
    """An int inside [low, high], else low. Booleans are never numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return low
    try:
        number = int(value)
    except (ValueError, OverflowError):  # NaN / Infinity
        return low
    return max(low, min(high, number))


def _target(raw, kind):
    """The outbound URL or the conversion name, clamped. None for other kinds."""
    if kind == 'outbound':
        value = raw.get('x')
    elif kind == 'conversion':
        value = raw.get('n')
    else:
        return None
    if not isinstance(value, str):
        return None
    text = value.replace('\x00', '').strip()[:300] or None
    if text and kind == 'outbound':
        # An outbound target is a URL, and it is rendered as a clickable link
        # on the dashboard's analytics page. This endpoint is unauthenticated,
        # so without a scheme allowlist any visitor could store
        # "javascript:..." and have it run inside a signed-in admin's session
        # the moment they clicked it. Escaping does not help: a javascript: URL
        # contains nothing an HTML escaper touches.
        #
        # This guard exists verbatim in the file this module was ported from
        # (website/app/routes/collect.py:_target) and did not survive the copy.
        # ci/check_analytics.py now asserts the VALUE is rejected, not merely
        # that the field exists — a field-name-only check is what let it
        # through the first time.
        if urlparse(text).scheme.lower() not in ('http', 'https'):
            return None
    # A conversion name is a name, never an href — left alone deliberately.
    return text


def _outbox_full() -> bool:
    """True when the queue has stopped being a buffer and become a problem.

    Cached for a minute and lock-guarded: this app runs gunicorn with
    --threads 4, so two threads can reach the check at once.
    """
    now = time.monotonic()
    with _ceiling_lock:
        if now - _ceiling_state['checked_at'] > _CEILING_CHECK_INTERVAL:
            _ceiling_state['checked_at'] = now
            try:
                _ceiling_state['full'] = (
                    db.session.query(AnalyticsOutboxEvent.id)
                    .limit(MAX_OUTBOX_ROWS + 1).count() > MAX_OUTBOX_ROWS
                )
            except Exception:
                # A failing count must not take the endpoint down with it.
                _ceiling_state['full'] = False
        return bool(_ceiling_state['full'])


def _current_visit(who, site, now):
    """This visitor's open visit, starting a new one if the last went quiet.

    The row is keyed on the rotating visitor_hash, so it is unreachable once the
    day turns over — a visit spanning midnight UTC is split in two. That is a
    real, accepted inaccuracy of the cookieless tier and it is a very small one;
    the alternative is storing something durable on the device, which is the
    thing the baseline tier promises not to do.
    """
    visit = (AnalyticsVisit.query
             .filter_by(visitor_hash=who, site=site)
             .order_by(AnalyticsVisit.last_seen_at.desc())
             .first())
    if visit is not None and not visit.is_stale:
        return visit

    visit = AnalyticsVisit(
        visitor_hash=who,
        site=site,
        session_id=uuid.uuid4().hex,
        started_at=now,
        last_seen_at=now,
    )
    db.session.add(visit)
    # Flushed, not committed: the caller commits once, after the events. A visit
    # row with no events would otherwise count as a session that read nothing.
    db.session.flush()
    return visit


# ===========================================================================
# OUTBOX DRAIN
# ===========================================================================

def _analytics_backoff(attempts):
    """Retry delay: 1m, 2m, 4m … capped at 30m.

    Identical to app.py's _funnel_outbox_backoff. `attempts` arrives already
    incremented, so the first retry (attempts=1) waits 1 minute.
    """
    return timedelta(seconds=min(60 * (2 ** min(max(attempts - 1, 0), 10)), 1800))


# Status codes that mean "this payload is wrong", as opposed to "we are
# temporarily broken". ONLY these age a row towards being parked. A connection
# error, a 500, a 503 or a dyno restart is our problem, not the payload's, and
# ageing rows on those quietly abandons real traffic during an outage that
# fixes itself. Note 503 is deliberately absent: the dashboard answers 503
# while ITS token is unset, which is exactly the normal state during a staged
# rollout — treating that as poison would park everything before the receiver
# was ever switched on.
PERMANENT_STATUSES = (400, 401, 403, 413, 422)


def _post_batch(site, events):
    """One delivery attempt.

    Returns ``(error, permanent)`` — ``(None, False)`` on success. ``permanent``
    says whether the far side rejected the PAYLOAD, which is the only thing
    that may count against a row's attempt budget.
    """
    try:
        resp = http_requests.post(
            _ingest_url(),
            json={'site': site, 'events': events},
            headers={'Authorization': f'Bearer {ingest_token()}'},
            timeout=10,
        )
    except Exception as exc:
        return f'{exc.__class__.__name__}: {exc}'[:300], False
    if resp.status_code != 200:
        return f'HTTP {resp.status_code}', resp.status_code in PERMANENT_STATUSES
    try:
        answer = resp.json()
    except ValueError:
        answer = None
    if not (isinstance(answer, dict) and answer.get('ok') is True):
        # A 200 whose body is not {"ok": true} is a contract violation by the
        # receiver, not a transport hiccup — that will not fix itself.
        return 'HTTP 200 (body not ok)', True
    return None, False


def flush_analytics_outbox(batch_size=ANALYTICS_OUTBOX_BATCH):
    """Send one batch of queued events. Returns (sent, failed, human summary).

    Never raises. A dashboard outage leaves the rows exactly where they are, to
    go out on the next drain — which is the whole reason the outbox exists.

    Must be called inside an app context.
    """
    if not ingest_token():
        return 0, 0, 'Analytics forward is not configured (ANALYTICS_INGEST_TOKEN unset).'

    now = datetime.utcnow()
    rows = (AnalyticsOutboxEvent.query
            .filter(AnalyticsOutboxEvent.attempts < ANALYTICS_OUTBOX_MAX_ATTEMPTS)
            .filter(db.or_(AnalyticsOutboxEvent.next_attempt_at.is_(None),
                           AnalyticsOutboxEvent.next_attempt_at <= now))
            .order_by(AnalyticsOutboxEvent.id)
            .limit(batch_size).all())
    if not rows:
        return 0, 0, 'Nothing to send.'

    # `site` is a batch-level field in the ingest contract, so a POST carries
    # one site — but ALL ready sites go out in this pass, one POST each.
    #
    # Selecting the oldest row's site and sending only that used to mean a
    # client could starve real traffic: `site` comes from the Host header, so
    # injecting rows under many distinct hosts outran the drain (a handful of
    # flushes per tick) and genuine pageviews never shipped. Grouping costs
    # nothing when there is only one site, which is the normal case here.
    by_site = {}
    for row in rows:
        try:
            by_site.setdefault(row.site, []).append((row, json.loads(row.body_json)))
        except (TypeError, ValueError):
            # Unparseable queue row: it can never succeed, so retiring it is the
            # only way it stops being retried forever.
            row.attempts = ANALYTICS_OUTBOX_MAX_ATTEMPTS
            row.last_error = 'unparseable body_json'

    if not by_site:
        db.session.commit()
        return 0, len(rows), 'No sendable rows — every candidate had an unparseable body.'

    sent = failed = 0
    notes = []
    for site, pairs in by_site.items():
        batch_rows = [row for row, _ in pairs]
        error, permanent = _post_batch(site, [event for _, event in pairs])
        if error is not None:
            for row in batch_rows:
                # Only a payload-level rejection ages a row. Everything else
                # is retried indefinitely — an outage must not silently
                # destroy a day of traffic via the parked-row prune.
                if permanent:
                    row.attempts += 1
                row.last_error = error
                row.next_attempt_at = (
                    datetime.utcnow() + _analytics_backoff(row.attempts + 1))
            failed += len(batch_rows)
            notes.append(f'{site}: failed ({error})')
            continue

        # Accepted — including any the dashboard recognised as duplicates,
        # which is the whole point of event_uid. Both outcomes mean "it is
        # safely on the other side", so both free the row.
        for row in batch_rows:
            db.session.delete(row)
        sent += len(batch_rows)
        notes.append(f'{site}: sent {len(batch_rows)}')

    db.session.commit()
    return sent, failed, '; '.join(notes)


def prune_analytics_visits(older_than_hours=36):
    """Delete visit rows nothing can reach any more.

    visitor_hash rotates at midnight UTC, so a row from yesterday can never be
    matched again — it is dead weight the moment the day turns. 36 hours rather
    than 24 leaves a margin for a visit that straddles midnight and a drain that
    runs late.
    """
    cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
    deleted = (AnalyticsVisit.query
               .filter(AnalyticsVisit.last_seen_at < cutoff)
               .delete(synchronize_session=False))
    db.session.commit()
    return deleted


def drain_analytics_outbox(app_obj):
    """Ship queued analytics events (runs inside the existing ~2-minute loop).

    Takes the Flask app explicitly and wraps its OWN app context, because the
    caller is a daemon thread with no request and Flask-SQLAlchemy 3.x requires
    one. Its own try/except for the same reason drain_funnel_outbox() has one:
    an uncaught exception here would kill the shared loop that also drains the
    funnel outbox and sends the hourly emails.

    Sending is a silent no-op when ANALYTICS_INGEST_TOKEN is unset, so this app
    keeps working standalone. Housekeeping is NOT skipped in that case: the
    visit table is a working set that has to be pruned whether or not anything
    is being forwarded, or an unconfigured-forward deployment grows it forever.
    """
    if AnalyticsOutboxEvent is None:
        return  # init_app() never ran
    with app_obj.app_context():
        try:
            if ingest_token():
                # A few batches per pass: a burst of traffic must not need hours
                # of 2-minute ticks to clear, and 200 events is a small POST.
                for _ in range(5):
                    sent, _failed, _summary = flush_analytics_outbox()
                    if sent == 0:
                        break

            now = datetime.utcnow()
            # Parked rows: tried many times, rejected every time, kept a week
            # so the evidence outlives the incident.
            AnalyticsOutboxEvent.query.filter(
                AnalyticsOutboxEvent.attempts >= ANALYTICS_OUTBOX_MAX_ATTEMPTS,
                AnalyticsOutboxEvent.created_at
                < now - timedelta(days=ANALYTICS_OUTBOX_PARK_PRUNE_DAYS),
            ).delete(synchronize_session=False)
            # Age-based sweep, INDEPENDENT of attempts. This runs whether or
            # not forwarding is configured, which the parked-row prune above
            # cannot: with ANALYTICS_INGEST_TOKEN unset the flush returns
            # before attempting anything, so attempts stays 0 forever and
            # those rows would accumulate without limit on a deployment that
            # never forwards at all.
            AnalyticsOutboxEvent.query.filter(
                AnalyticsOutboxEvent.created_at
                < now - timedelta(days=ANALYTICS_OUTBOX_MAX_AGE_DAYS),
            ).delete(synchronize_session=False)
            db.session.commit()

            prune_analytics_visits()
        except Exception as exc:
            db.session.rollback()
            print(f'Analytics outbox drain error: {exc}')
        finally:
            db.session.remove()
