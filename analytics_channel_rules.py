"""Acquisition-channel rules. THE SHARED ARTIFACT — byte-identical in two repos.

    writeitgreat-llc/website              app/analytics_channel_rules.py
    writeitgreat-llc/proposal-evaluation  analytics_channel_rules.py

Both sites answer "where did this visitor come from" and both answers land in
the same dashboard tables, so they have to be the same answer. They are
separate repos with no shared package, so this file is VENDORED: the port is a
copy, and `diff` audits it. Nothing in here names its own package, imports
anything outside the standard library, or knows which app it is running in —
that is what makes the two copies byte-identical rather than merely similar.

CHANGING THE RULES
------------------
1. Edit this file in one repo.
2. Bump RULES_VERSION below.
3. Copy the WHOLE file to the other repo. Do not hand-transcribe it. Hand
   transcription is how the substring bug this file exists to prevent got in.
4. Recompute the digest and paste it into ci/analytics_channel_fixture.json in
   BOTH repos (`python ci/check_channel_parity.py` prints the value on
   mismatch), and add a vector proving whatever you changed.
5. Ship proposal-evaluation and website close together. Neither repo's CI can
   see the other, so nothing will stop you from deploying one and not the
   other — see ci/check_channel_parity.py for exactly what is and is not
   guaranteed.

WHY THE MATCHING LOOKS PARANOID
-------------------------------
The rule that shipped before this file matched referrer hosts as bare
substrings — `any(needle in host for needle in needles)`. "t.co" is a
substring of "wri|t.co|m", so every referral from our own writeitgreat.com was
reported as social traffic, and so was every visit from microsoft.com,
sharepoint.com, dropbox.com and manuscriptwishlist.com. Any brand token is a
substring of something. Every match here is therefore anchored, in one of two
ways, because the two cases genuinely differ:

  DOMAIN MATCH (_matches) — full registrable domains, matched by equality or a
    dot-suffix, so m.facebook.com and l.instagram.com work and notx.com does
    not. Used where the brand owns a small, stable set of domains.

  BRAND MATCH (_brand_match) — the single label immediately left of the public
    suffix. Used only where a brand has real country-domain sprawl
    (google.co.uk, google.de, pinterest.co.uk, youtube.co.uk) that no suffix
    list survives. Anchoring to the registrable label is what stops a third
    party forging a bucket: google.mycompany.com and blog.google are NOT
    Google search, and an attacker who controls google.com.evil.example cannot
    file themselves under organic search.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# Bump on every rule change. Reported on the analytics ingest envelope by both
# senders, so the dashboard can see the two sites disagreeing within minutes of
# a one-sided deploy — the only detector that works across two repos.
RULES_VERSION = "2026-07-31a"

# ── Referrer host matching ──────────────────────────────────────────────────

# Brands with country-domain sprawl. Matched against the REGISTRABLE LABEL
# only (see _brand_match), never as a substring and never as any label.
SEARCH_BRANDS = frozenset({
    "google", "bing", "duckduckgo", "yahoo", "ecosia", "brave", "startpage",
    "qwant", "baidu", "yandex", "naver", "seznam",
})
SOCIAL_BRANDS = frozenset({
    "linkedin", "facebook", "instagram", "pinterest", "youtube", "tiktok",
    "reddit", "twitter", "tumblr",
})

# Everything else: exact domains, matched by equality or dot-suffix.
#
# `marginalia` is a domain rather than a brand on purpose — "marginalia" is
# ordinary book-trade vocabulary, and marginalia.blog / marginalia.press are
# not the search engine.
SEARCH_DOMAINS = (
    "marginalia.nu", "kagi.com", "mojeek.com",
)
SOCIAL_DOMAINS = (
    "lnkd.in", "fb.com", "fb.me", "threads.net", "threads.com",
    "twitter.com", "x.com", "t.co", "youtu.be", "bsky.app", "bsky.social",
    "mastodon.social", "substack.com", "medium.com", "news.ycombinator.com",
    "quora.com", "discord.com", "slack.com", "tumblr.com", "t.umblr.com",
)
EMAIL_DOMAINS = (
    "outlook.com", "outlook.live.com", "office.com", "office365.com",
    "superhuman.com", "hey.com", "proton.me", "protonmail.com",
    "fastmail.com", "zoho.com",
)

# A host whose FIRST label is one of these is somebody's webmail, whoever they
# are: mail.yahoo.co.jp, mail.yandex.ru, webmail.some-university.edu. Without
# this, regional webmail falls past the email test into the brand test and
# mail.yahoo.co.jp gets reported as organic search — the exact mistake the
# "email before search" ordering below exists to prevent, just one TLD over.
WEBMAIL_LABELS = frozenset({"mail", "webmail"})

# Labels that are really part of the public suffix, so the brand sits one
# further left: google.co.uk, yahoo.co.jp, google.com.au, pinterest.co.uk.
# Not a full public-suffix list and not trying to be — it covers the shape
# every major search engine and social network actually uses.
SUFFIX_LABELS = frozenset({
    "co", "com", "net", "org", "edu", "gov", "mil", "ac", "or", "ne", "go",
})

# utm_medium values that mean somebody paid for the click.
PAID_MEDIUMS = {
    "cpc", "ppc", "paid", "paidsearch", "paid_search", "paid-search",
    "paidsocial", "paid_social", "paid-social", "display", "banner",
    "cpm", "retargeting", "remarketing",
}
EMAIL_MEDIUMS = {"email", "e-mail", "newsletter", "mail"}
SOCIAL_MEDIUMS = {"social", "social-organic", "social_organic", "organic-social"}

CAMPAIGN_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")

# One label per bucket, used by both the lead views and the analytics rollups.
# wig-dashboard's WA_CHANNELS is this set plus "unknown", which is the
# receiver's bucket for an event that arrived with no channel at all — so the
# right relationship there is subset, not equality.
CHANNELS = (
    "paid", "email", "social", "organic search", "referral", "campaign", "direct",
)


def host_of(referrer: Optional[str]) -> Optional[str]:
    """The comparable host of a referrer URL, or None.

    `.hostname` rather than `.netloc` on purpose: it drops userinfo, drops the
    port, lowercases, and unwraps IPv6 brackets in one step. Parsing the netloc
    by hand is how `https://someone@t.co/` ends up with a host of "someone".
    """
    if not referrer:
        return None
    try:
        host = urlparse(referrer).hostname
    except ValueError:
        return None
    if not host:
        return None
    # A fully-qualified "t.co." is the same site as "t.co", and some clients
    # and proxies really do send the root dot. Without this the dot-suffix
    # anchor below misses it.
    host = host.rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _matches(host: Optional[str], domains: tuple) -> bool:
    """True when `host` IS one of `domains` or is a subdomain of one.

    Boundary-anchored on purpose — see the note at the top of this file.
    `t.co` must match `t.co` and `www.t.co`, and must not match
    `writeitgreat.com`.
    """
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def registrable_brand(host: Optional[str]) -> Optional[str]:
    """The label immediately left of the public suffix, or None.

    google.com -> google        news.google.com -> google
    google.co.uk -> google      google.mycompany.com -> mycompany
    blog.google -> blog         pinterest.co.uk -> pinterest
    """
    if not host:
        return None
    labels = host.split(".")
    if len(labels) < 2:
        return None
    brand = labels[-2]
    if brand in SUFFIX_LABELS and len(labels) >= 3:
        brand = labels[-3]
    return brand or None


def _brand_match(host: Optional[str], brands: frozenset) -> bool:
    """True when the host's REGISTRABLE label is one of `brands`."""
    return registrable_brand(host) in brands


def _is_webmail(host: Optional[str]) -> bool:
    """True when the host's first label marks it as somebody's webmail."""
    if not host:
        return False
    labels = host.split(".")
    return len(labels) >= 2 and labels[0] in WEBMAIL_LABELS


def classify(source: dict) -> str:
    """Which acquisition channel this visit or lead arrived through.

    Order matters and encodes precedence: an explicit paid signal beats a
    referrer, because a paid Google click and an organic Google click carry
    the same referrer and only the click id or utm_medium tells them apart.
    """
    medium = (source.get("utm_medium") or "").lower()
    host = host_of(source.get("referrer"))

    if source.get("click_id") or medium in PAID_MEDIUMS:
        return "paid"
    # Email before search: mail.google.com carries the brand "google" and would
    # otherwise be reported as organic search traffic.
    if medium in EMAIL_MEDIUMS or _matches(host, EMAIL_DOMAINS) or _is_webmail(host):
        return "email"
    if (medium in SOCIAL_MEDIUMS
            or _matches(host, SOCIAL_DOMAINS)
            or _brand_match(host, SOCIAL_BRANDS)):
        return "social"
    if _matches(host, SEARCH_DOMAINS) or _brand_match(host, SEARCH_BRANDS):
        return "organic search"
    if host:
        return "referral"
    # A campaign tag with no referrer at all: the QR code, the printed card,
    # the link pasted into a DM. Worth separating from true direct traffic —
    # somebody tagged that link on purpose.
    if any(source.get(key) for key in CAMPAIGN_KEYS):
        return "campaign"
    # No referrer, no tags. Mostly NOT people typing the URL: app browsers,
    # link previews, and every https->http hop send no referrer at all.
    return "direct"


def classify_stored_host(host: Optional[str], campaign: Optional[dict] = None) -> str:
    """Classify from a STORED host rather than a full referrer URL.

    Every table stores `referrer_host`, never the referrer itself, so every
    backfill has only a bare host to work from. Handing that bare host to
    classify() does NOT work and does not fail loudly either:
    `urlparse("linkedin.com").hostname` is None, so the whole string lands in
    `.path`, every match short-circuits, and the row silently becomes
    "direct". A backfill written the obvious way relabels the entire table.

    This is the one supported way to do it. Use it from backfills; the parity
    fixture asserts it agrees with classify().
    """
    source = dict(campaign or {})
    if host:
        source["referrer"] = "https://" + host + "/"
    return classify(source)
