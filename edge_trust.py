"""Did this request actually arrive through Cloudflare?

`authors.writeitgreat.com` is a custom domain on this Heroku app, proxied by
Cloudflare. `proposal-evaluation-20d7e1515843.herokuapp.com` is the same app,
and it answers directly — verified again 2026-08-04: `Server: Heroku`, no
`cf-ray`. Cloudflare cannot close that address; Heroku offers no origin
firewall on this plan. So it stays reachable and stops being useful instead.

Why it mattered here. Several of the controls on this app key on
`analytics_collect.client_ip()`, which reads `CF-Connecting-IP`: the sign-up
rate limits added after the July 2026 bot, the `/social-strategy` lead-magnet
limits, `/api/submit`'s limiter, and `visitor_hash`. Cloudflare overwrites that
header on traffic it proxies, so it is authoritative there and nowhere else. On
the origin it is a string the caller types, and `TRUST_CLOUDFLARE_IP=1` used to
be the whole test — so a caller on that address could pick a new identity per
request. Two places in app.py already say so in comments and pair the header
with the peer address to compensate; this module lets them stop compensating.

## Why the Host header cannot be the test

Heroku's router dispatches on Host and *both* hostnames map to this app, so a
caller can open a connection straight to Heroku and send
`Host: authors.writeitgreat.com` themselves. The request never touches
Cloudflare and the app cannot tell from Host alone.
`analytics_collect.site_name()` learned exactly this and stopped reading Host
for the same reason.

## What actually is unforgeable

The rightmost `X-Forwarded-For` hop. Heroku's router APPENDS the address that
opened the TCP connection to it, so anything the caller supplies is pushed
left. `ProxyFix(x_for=1)` at the top of app.py reads `values[-1]`, which is why
`request.remote_addr` is already described there as "the unforgeable peer".

Measured against the sibling marketing site's production router log on
2026-08-04, both halves together::

    fwd="203.0.113.99, 73.100.144.66"      # forged first, real caller appended
    fwd="168.119.246.194, 162.158.110.183" # real visitor, CF edge appended

So: if the appended hop is inside Cloudflare's published ranges, the request
provably transited Cloudflare and `CF-Connecting-IP` can be believed.

This needs no Cloudflare dashboard configuration, which matters — the zone is
on the free plan, where several of the obvious alternatives (a Transform Rule
injecting a shared secret, mTLS origin pulls) are unavailable or are another
undocumented setting to drift.

## Keeping this in step with the marketing site

`website/app/edge_trust.py` is the same idea and the range block below is kept
textually identical to it, so a diff between the two is short and obvious. The
repositories cannot see each other, so nothing in CI can enforce that — the
same limitation `analytics_channel_rules.py` documents. What each side CAN do,
and does, is assert that its own list still classifies edges observed in real
production traffic.

Refresh with::

    curl -s https://www.cloudflare.com/ips-v4
    curl -s https://www.cloudflare.com/ips-v6

and bump RANGES_FETCHED in both repos.
"""

from __future__ import annotations

import ipaddress
import os
import time

# Fetched from cloudflare.com/ips-v4 and /ips-v6 on this date. Kept as text,
# not fetched at import: a network call on boot is a way for the dyno to fail
# to start when Cloudflare is having a bad day, which is exactly when you want
# the app up.
RANGES_FETCHED = "2026-08-04"

_CLOUDFLARE_V4 = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
)

_CLOUDFLARE_V6 = (
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

CLOUDFLARE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in _CLOUDFLARE_V4 + _CLOUDFLARE_V6
)


def is_cloudflare_address(value):
    """Is this a Cloudflare edge address?

    Pure and request-free, so the tests can exercise it against addresses
    observed in production without standing up a request context.
    """
    if not value:
        return False
    try:
        addr = ipaddress.ip_address(value.strip())
    except (ValueError, AttributeError):
        return False
    return any(addr in net for net in CLOUDFLARE_NETWORKS)


def peer_address():
    """The address that actually opened the connection to Heroku's router.

    Post-ProxyFix `remote_addr` IS that value. Read through this name rather
    than touching `remote_addr` at the call sites, so the one assumption this
    file rests on has one place to be wrong.
    """
    from flask import request

    return request.remote_addr


def arrived_via_cloudflare():
    """True when this request provably transited the Cloudflare edge."""
    return is_cloudflare_address(peer_address())


# ── Range-drift canary ───────────────────────────────────────────────────────
#
# Fires when a request carries `CF-Ray` (only Cloudflare sets it on genuine
# traffic) but the appended hop is not in the list above: either the list has
# gone stale, or somebody is forging CF-Ray straight at the origin.
#
# The second cause is why there is an interval. It is attacker-triggerable by
# design, and Sentry's free tier is 5,000 events a MONTH shared across all
# three apps, so an unthrottled warning here is a way for a stranger to spend
# the company's whole error budget in an afternoon. Once per process per six
# hours bounds it while still surfacing a genuine range change within a day.
_DRIFT_INTERVAL = 6 * 3600
_last_drift_note = 0.0


def note_possible_range_drift():
    """Log at most one drift warning per process per _DRIFT_INTERVAL."""
    global _last_drift_note

    from flask import current_app, request

    if not request.headers.get('CF-Ray'):
        return
    now = time.monotonic()
    if _last_drift_note and (now - _last_drift_note) < _DRIFT_INTERVAL:
        return
    _last_drift_note = now
    # Stable, distinctive wording: a log alert searching for text that never
    # appears in the logs is an alarm that can never ring, which has already
    # happened twice on this account. Grep for "cloudflare-range-drift".
    current_app.logger.warning(
        'cloudflare-range-drift: request carried CF-Ray but its appended '
        'X-Forwarded-For hop is not in the vendored Cloudflare ranges '
        '(fetched %s). Either edge_trust.py needs refreshing from '
        'cloudflare.com/ips-v4, or somebody is forging CF-Ray directly at '
        'the origin. Per-visitor throttling is degraded until resolved.',
        RANGES_FETCHED,
    )


# ── The bypass origin ────────────────────────────────────────────────────────

BYPASS_HOST_SUFFIX = '.herokuapp.com'

CANONICAL_BASE_DEFAULT = 'https://authors.writeitgreat.com'


def canonical_base():
    """Where ordinary visits at the origin are sent.

    Configurable, but with the real domain as the default rather than anything
    derived from the request — a redirect target built from Host would let a
    caller aim this app's redirects wherever it liked.
    """
    return (os.environ.get('SITE_BASE_URL')
            or CANONICAL_BASE_DEFAULT).rstrip('/')


# Paths that must keep answering on the herokuapp origin instead of being
# redirected. Each is a machine caller that deliberately, and correctly, uses
# that address.
#
#   /healthz   an external uptime probe watches it there ON PURPOSE, to
#              separate "the dyno is down" from "the edge is down". Redirect it
#              and the probe silently starts measuring Cloudflare instead — it
#              would stay green straight through an origin outage, the one
#              thing it exists to catch. deploy.yml's APP_URL is this path for
#              the same reason.
#   /api/      the Wix caller posts to /api/submit with an X-API-Key and polls
#              /api/status/<id>, which is a GET. Cross-host redirects drop
#              Authorization-style headers in most HTTP clients, and a redirect
#              also breaks the CORS preflight this endpoint answers.
#   /.well-known  ACME and friends. A broken certificate renewal is an
#              expensive way to discover the exception was needed.
#
# The analytics beacon (/e) and /consent are POST-only and the redirect is
# GET/HEAD-only, so they are already covered — listed anyway so that adding a
# GET handler later cannot silently start redirecting a browser beacon.
BYPASS_EXEMPT_EXACT = ('/healthz', '/e', '/consent')
BYPASS_EXEMPT_PREFIXES = ('/api/', '/.well-known/')


def is_bypass_host(host):
    """Is this request addressed to the un-proxied Heroku origin?"""
    if not host:
        return False
    return host.split(':')[0].strip().lower().endswith(BYPASS_HOST_SUFFIX)


def is_exempt_path(path):
    """Machine paths that keep working on the bypass origin."""
    if path in BYPASS_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in BYPASS_EXEMPT_PREFIXES)


def trusts_cloudflare_header():
    """The deployment-level switch, now necessary but no longer sufficient.

    Kept as a kill-switch that works without a deploy: if Cloudflare changes
    its ranges and the canary above starts firing, unsetting this makes the app
    ignore `CF-Connecting-IP` everywhere in one `heroku config:unset`, rather
    than requiring a release to react.
    """
    return os.environ.get('TRUST_CLOUDFLARE_IP', '').strip().lower() in (
        '1', 'true', 'yes', 'on',
    )
