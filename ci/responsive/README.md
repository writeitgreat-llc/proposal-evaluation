# Responsive / mobile regression audit

Checks that `authors.writeitgreat.com` renders without horizontal bleed,
unreachable navigation, or content hidden underneath the header — at phone,
tablet and desktop widths, **signed in as well as signed out**.

Runs as a step inside the `proposal-ci` job. It is deliberately not a job of its
own: `proposal-ci` is the required status check, and a new job would be a new
status context that gates nothing until someone re-runs
`ci/setup_branch_protection.sh` *and* adds the name to `deploy.yml`'s
`BLOCKING_CHECKS`. Those three lists drift apart silently. A step inside a job
that is already required costs nothing and gates everything.

## Run it locally

```bash
# 1. the app, on a throwaway database
OPENAI_API_KEY=local-dummy SECRET_KEY=local-dev \
DATABASE_URL=sqlite:////tmp/wig_responsive.db \
  .venv/bin/gunicorn app:app --bind 127.0.0.1:5000 --workers 1 --threads 4 &

# 2. the audit
cd ci/responsive && npm install && npx playwright install chromium
node audit.js --base-url http://127.0.0.1:5000
```

`--soft` reports everything and still exits 0. `--browser webkit` is the closest
thing to Mobile Safari available on a Linux runner; CI runs chromium only, to
keep this inside the existing job rather than paying for a second one.

Findings, every raw measurement, and a screenshot of each failing
page × viewport land in `_report/`. CI uploads it as an artifact.

## What it asserts

| check | what it catches |
|---|---|
| `bleed` | `document.scrollWidth` exceeds the viewport — the page scrolls sideways |
| `overflow` | a named element crosses the viewport edge, even when an ancestor clips it and `scrollWidth` looks clean |
| `nav-unreachable` | a nav link sits outside the viewport (this is "Logout bled off screen") |
| `header-overlaps-main` | `<main>` starts above the bottom of the header |
| `sticky-under-header` | a `position: sticky` element pins above the header's bottom, so the header covers it |
| `module-unreachable` | coaching enrolment did not take, so the module page — and the only sticky element the audit can see — went unmeasured |
| `flash-missing` | the post-register banner did not render, so every flash assertion below would pass vacuously |
| `flash-covered` | a flashed banner starts underneath the header |
| `flash-not-dismissible` / `flash-close-too-small` / `flash-close-misplaced` | the dismiss button is absent, under 24px, or outside its banner |

`header-overlaps-main` is the load-bearing one. The header is `position: sticky`
and therefore in flow, so it reserves its own height whatever it grows to; that
check is what stops anyone quietly putting it back to `fixed` with a
hand-maintained `main { padding-top }`, which is the bug this whole directory
exists because of.

## Why it signs in

Three things had to be true at once for the original bug to reach production
unseen, and only fixing all three closes the hole:

1. **No browser.** `ci/smoke_app.py` drives Flask's `test_client` and gets HTML
   strings back. No viewport, no CSS, no layout — no geometry to be wrong.
2. **Wrong repo.** A responsive audit already existed in
   `writeitgreat-llc/website`, pointed at the marketing site. It has never made
   a request to this app.
3. **Wrong pages.** `base.html` renders the nav only when
   `current_user.is_authenticated`, so the signed-out header is a bare logo.
   A browser check over `smoke_app.py`'s public route list would have gone
   green on a bug that only exists once you have an account.

So the audit registers an author through the real form — no Turnstile challenge
appears because `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` are unset in CI —
and measures the dashboard it lands on, banner and all. **Do not reduce it to
public pages only.** That is the exact shape of the false green it exists to
prevent, and it would look like a passing build.

## Known gaps

- **The team-member nav and the admin dashboard are not driven.** That nav is the
  widest in the app (ten links for a full admin) and the most likely thing to
  grow next. It is not covered here because team login is 2FA-mandatory and a
  TOTP dance in CI is a flake source. The author nav exercises the same
  wrap-and-reserve behaviour, and the sticky header makes the height
  self-correcting rather than a number anyone maintains, so the ten-link case is
  protected structurally rather than by measurement.

  The one thing that gap genuinely lost — a hard-coded sticky offset on a page
  nothing can log into — is covered instead by `ci/check_sticky_offsets.py`,
  which reads the rule rather than the rendering. That is what caught
  `.bulk-bar { top: 60px }`.

  If you do want the admin pages measured: seed an `AdminUser` with a known
  `totp_secret` and feed `pyotp.TOTP(secret).now()` into `/admin/verify-2fa`.
- **Chromium only.** The repo's authors are mostly on iPhone; WebKit-on-Linux
  has its own font metrics and is a source of signal rather than a merge gate.
  Run `--browser webkit` by hand when changing type or spacing.
