# CONTRACTS.md — integration API contracts this app participates in

How authors.writeitgreat.com connects to the company dashboard
(app.writeitgreat.com). This repo is **PUBLIC**: this file documents wire
shapes and env-var NAMES only — secret values live exclusively in Heroku
config vars, and webhook payloads stay lean by contract (IDs, statuses,
emails — never evaluation text or scores).

## 1. Funnel events v1 — SENDER

Fired fire-and-forget from a daemon thread (4s timeout, swallow everything,
no retry in v1 — a lost event is re-derivable from the admin UI) at four
points: author registration, proposal creation (`/api/evaluate`,
`/api/submit`, admin-created), the admin proposal-status route, and the
publisher-portal status route.

`POST {FUNNEL_EVENTS_URL}` (default: the wig-dashboard app URL +
`/api/literary/funnel-events`) · `Authorization: Bearer $FUNNEL_EVENTS_TOKEN`
(same env name on both apps).

Body `{"event": {…}}`:
- `external_id` (≤64, required — the dedup key): `pe-reg-<author.id>` ·
  `pe-sub-<proposal.id>` · `pe-pst-<proposal.id>-<new_status>` ·
  `pe-pub-<publisher_proposal.id>-<new_status>`
- `type`: author_registered | proposal_submitted | proposal_status_changed |
  publisher_status_changed
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
- Single-use: `jti` recorded in the `consumed_jti` table (insert-FIRST;
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

## Change protocol

Contract changes update BOTH sides' CONTRACTS.md in the same change set
(dashboard: `wig-dashboard/CONTRACTS.md`). Never add retry/queue behavior to
the funnel sender without re-reading the single-dyno constraint — the web
dyno hosts the rate limiter and the hourly email daemon; a worker dyno is a
deliberate non-goal.
