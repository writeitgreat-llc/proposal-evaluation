# CONTRACTS.md — integration API contracts this app participates in

How authors.writeitgreat.com connects to the company dashboard
(app.writeitgreat.com). This repo is **PUBLIC**: this file documents wire
shapes and env-var NAMES only — secret values live exclusively in Heroku
config vars, and webhook payloads stay lean by contract (IDs, statuses,
emails — never evaluation text or scores).

## 1. Funnel events v1.1 — SENDER (at-least-once since 2026-07-27)

Every event lands in the `funnel_outbox` table first (inside the request),
a daemon thread attempts immediate delivery (4s timeout), and a 2-minute
drain loop retries anything unsent with exponential backoff (1m→30m cap,
200 attempts ≈ 4 days — sized to outlive receiver deploy lag; sent and
long-abandoned rows pruned after 7 days). Duplicate delivery is safe by
contract — the receiver is idempotent on `external_id` and answers 200 for
duplicates. v1 was fire-and-forget; a dashboard blip lost events forever.

Fire points: author registration; proposal creation (`/api/evaluate`,
`/api/submit`, admin-created, AND the coaching-built path — gap closed
2026-07-27); the admin proposal-status route; **bulk status changes**
(`/admin/proposals/bulk-action`, per-proposal events, no-ops skipped —
previously invisible to the dashboard); the publisher-portal status route;
and **one-pager submission** (`one_pager_submitted`, the top of Andy's
funnel).

`POST {FUNNEL_EVENTS_URL}` (default: the wig-dashboard app URL +
`/api/literary/funnel-events`) · `Authorization: Bearer $FUNNEL_EVENTS_TOKEN`
(same env name on both apps).

Body `{"event": {…}}`:
- `external_id` (≤64, required — the dedup key): `pe-reg-<author.id>` ·
  `pe-sub-<proposal.id>` · `pe-pst-<proposal.id>-<new_status>` ·
  `pe-pub-<publisher_proposal.id>-<new_status>` ·
  `pe-1pg-<one_pager_submission.id>` (a bulk move and a single edit to the
  same status share an external_id on purpose — they dedup upstream)
- `type`: author_registered | proposal_submitted | one_pager_submitted |
  proposal_status_changed | publisher_status_changed
- `occurred_at` (ISO-8601|null), `author_name`, `author_email`, `book_title`,
  `proposal_submission_id`, `old_status`, `new_status`, `publisher_name`,
  `payload` (small object, optional)

Receiver semantics (dashboard side): idempotent on `external_id`
(`{"ok":true,"created":true|false}`); milestone statuses (contract_sent,
contract_signed, offer_received, deal_closed / deal_sent, deal_signed) enter
a human review queue with a bell; everything else records silently.
Receiver errors: 503 token unset · 401 mismatch · 413 >64KB · 400 malformed.

## 2. Admin SSO jump v1 — RECEIVER (`GET /sso/consume?token=…`)

The dashboard's "Authors admin" quick link mints a short-lived signed token
(`POST /api/sso/mint` there) and lands here.

- Token: itsdangerous `URLSafeTimedSerializer($SSO_JUMP_SECRET_PROPOSAL,
  salt='admin-jump-v1')` over `{jti, email, name, dash_uid, dash_role}`;
  verified with `max_age=60`. Same secret value on both apps, env-only.
- Single-use: `jti` recorded in the `consumed_sso_token` table (insert-FIRST;
  pruned >5 min) — replays bounce even within the 60s window.
- Identity mapping: token email (lowercased) against
  `AdminUser.dashboard_email` (per-admin override, editable on /admin/team),
  falling back to `AdminUser.email`.
- Hard requirements, never waived: `is_active_account` AND `totp_enabled` —
  a jump can't bypass the registration approval gate or first-login TOTP
  enrollment. Success → admin session + fixed 302 to `/admin` (no `next`
  param, ever). Every failure → /admin/login with a specific flash.
- The jump bypasses THIS app's TOTP prompt by design; that is acceptable
  only because every dashboard team login carries mandatory 2FA.

## 3. Analytics ingest v1 — SENDER (`POST /api/analytics/ingest` on the dashboard)

Full specification: `_integration/CONTRACT_ANALYTICS.md` in the team workspace.
This is the second of the two collecting properties; `website` is the first and
its side is documented in `writeitgreat-llc/website` → `CONTRACTS.md` §3.
Summary of this app's side.

This app **collects**; the dashboard **stores and reports**. Visitors' browsers
beacon to `POST /e` on authors.writeitgreat.com (same-origin on purpose — a
cross-origin beacon needs a CORS preflight that `sendBeacon` cannot send, and a
collector on another host is what tracker-blockers match). `/e` writes one row
to `analytics_outbox`; the existing ~2-minute background drain in
`_start_reengagement_thread()` ships batches server-to-server under
`Authorization: Bearer $ANALYTICS_INGEST_TOKEN` — **the same env-var name on
both sides**, deliberately not a sender/receiver name pair (that asymmetry is
why the leads link sat dead in production for days).

Why a queue rather than a direct call: a pageview must not put an HTTP request
to another host on the render path of this single-worker app, and a dashboard
outage must cost zero pageviews. There is deliberately **no** per-event
immediate-send thread here, unlike `_emit_funnel_event` above: funnel events are
rare and feed a human review queue, pageviews are frequent and the contract says
ship them in batches.

Idempotent on `event_uid` (uuid4, minted here, unique). A batch that times out
after the dashboard committed it is recognised on retry, not double-counted — so
the drain is always safe to re-run. Rows are deleted only on `{"ok": true}`;
anything else increments `attempts`, records the error and sets
`next_attempt_at` on the same 1m→30m backoff as the funnel outbox. After 100
failed attempts (~2 days) a row is parked rather than blocking the queue, and
parked rows are dropped after 7 days — a pageview outbox is high volume and an
unbounded poison queue is the worse failure.

Wire shape per event (batch: `{"site": "<Host header>", "events": [...]}`,
≤200 per POST): `event_uid`, `kind` (pageview|engagement|outbound|conversion),
`occurred_at`, `visitor_hash`, `session_id`, `consented`, `path`,
`referrer_host`, `channel`, `utm_source`/`utm_medium`/`utm_campaign`,
`country`, `device_type`, `browser`, `os`, `is_bot`, `engaged_ms`,
`scroll_pct`, `target`, `is_entry`, `is_exit`. `channel` uses the same
vocabulary and the same rules as the marketing site's
`app/source_capture.py` — separate repos, so the classifier is a verbatim port
in `analytics_collect.py`; change one, change both or the two "which channel"
tables stop being comparable.

**Scope — public funnel pages only.** Instrumented: `/author/register`,
`/author/login`, `/author/forgot-password`, `/author/reset-password/<token>`,
`/confidentiality`, `/social-strategy`. NOT instrumented: the signed-in
application interior, the publisher portal, and the admin pages. The boundary is
structural, not a path list — `templates/base_public.html` is the only template
that includes the tracker, and it renders it only when
`current_user.is_authenticated` is false. Registration conversion is **not**
re-measured here: `author_registered` in §1 already answers it.

**Privacy invariants (asserted by `ci/check_analytics.py`, not just
documented):** no raw IP address and no raw User-Agent is written to storage;
`path` never carries a query string, and it is reduced to the matched Flask
route so `/author/reset-password/<token>` can never store a live reset token;
the visitor hash is keyed by `ANALYTICS_SALT_KEY` and its salt input rotates
daily, so it cannot follow a person across days unless they opted into the
`wig_vid` cookie; that cookie is set server-side, HttpOnly, Secure, SameSite=Lax;
`Sec-GPC: 1` (and `DNT: 1`) is a standing refusal — no cookie, no banner.

Env vars, all optional, all `os.environ` only (this repo is public):
`ANALYTICS_SALT_KEY` (unset ⇒ the tracker is not rendered and `/e` stores
nothing), `ANALYTICS_INGEST_TOKEN` (unset ⇒ the drain is a silent no-op),
`ANALYTICS_INGEST_URL` (defaults to the wig-dashboard app URL +
`/api/analytics/ingest`), `TRUST_CLOUDFLARE_IP` (set only in the same change
that puts Cloudflare in front, never before), `PRIVACY_URL` (when a privacy
policy exists, the consent banner links to it).

## Change protocol

Contract changes update BOTH sides' CONTRACTS.md in the same change set
(dashboard: `wig-dashboard/CONTRACTS.md`). Never add retry/queue behavior to
the funnel sender without re-reading the single-dyno constraint — the web
dyno hosts the rate limiter and the hourly email daemon; a worker dyno is a
deliberate non-goal. §3 has a third side: the analytics contract is written
against three repos at once, so a change to it is a change in
`_integration/CONTRACT_ANALYTICS.md`, `website`, this app, and the dashboard.
