# Deployment

How code gets from a laptop to production, for both Write It Great apps.
This file is identical in both repos on purpose — whichever one you are in, the
rules are the same.

| | Marketing site | Proposal tool |
|---|---|---|
| Repo | `writeitgreat-llc/website` (private) | `writeitgreat-llc/proposal-evaluation` (public) |
| Heroku app | `writeitgreat-website` | `proposal-evaluation` |
| Heroku default branch | `main` (verified) | auto-detected at deploy time |
| Deployed URL (origin; bypasses Cloudflare, browser GETs 301 to the public URL — see below) | https://writeitgreat-website-be6985a92063.herokuapp.com/ | https://proposal-evaluation-20d7e1515843.herokuapp.com/ |
| Public URL | https://writeitgreat.com | https://authors.writeitgreat.com |
| CI check names | `Intake regression suite`, `Engine parity and template integrity`, `App boots on Python 3.11` | `proposal-ci` |
| Python | 3.11.11 (`runtime.txt`) | 3.11.7 (`runtime.txt`) |
| Release phase | `flask db upgrade` | `release: python migrate.py` |

---

## authors.writeitgreat.com — settled, 2026-07-31

This section used to warn that a *fifth Heroku app* might serve
`authors.writeitgreat.com`, and told readers not to trust this pipeline until
somebody confirmed it with Andy. **That was wrong.** The domain is a custom
domain on *this* app, CNAME'd to
`molecular-mandrill-tf33ef4jp15w48r2zm06lz31.herokudns.com`. There is no fifth
app, merging a PR does update what authors see, and both smoke targets are
meaningful. Kept rather than deleted because the wrong version was quoted in
several places and it is worth being able to see it retracted.

---

## ⚠️ The herokuapp origin bypasses Cloudflare — and always will

`authors.writeitgreat.com` reaches this app through Cloudflare.
`https://proposal-evaluation-20d7e1515843.herokuapp.com/` reaches the **same
app** directly — verified 2026-08-04: `Server: Heroku`, no `cf-ray`.

**That origin is permanent.** Heroku offers no origin firewall on this plan, so
it cannot be locked to Cloudflare. Anything enforced *at the edge* — WAF rules,
Cloudflare rate limiting, bot filtering, Turnstile's page-level challenge —
genuinely does not apply there, and never will. Do not describe an edge rule as
covering this app without saying "except on the origin".

**What is no longer true (fixed 2026-08-04).** `CF-Connecting-IP` used to be
forgeable there. `TRUST_CLOUDFLARE_IP=1` is set on this app, and that switch
alone used to grant trust — so on the origin a caller could type a different
address on every request and never be counted by any per-visitor control:
the sign-up limits added after the July 2026 bot, the `/social-strategy`
lead-magnet limits, `/api/submit`'s limiter, `visitor_hash`.

`edge_trust.py` now requires proof of arrival. Heroku's router **appends** the
address that opened the connection to it, so anything a caller writes into
`X-Forwarded-For` is pushed left and the last value is out of reach.
`ProxyFix(x_for=1)` reads that last value. Measured on production 2026-08-04:

```
fwd="203.0.113.99, 73.100.144.66"       # forged first, real caller appended
fwd="168.119.246.194, 162.158.110.183"  # real visitor, Cloudflare edge appended
```

If that appended hop is inside Cloudflare's published ranges the request
provably transited the edge and the header is believed; otherwise it is ignored
and the limit keys on the caller's real address.

**The Host header is NOT the test, and must never become it.** Heroku routes on
Host and both hostnames map to this app, so a caller can connect straight to
Heroku and send `Host: authors.writeitgreat.com`.
`analytics_collect.site_name()` already stopped reading Host for exactly this
reason. Host is used only to choose the redirect below, where being wrong costs
nothing.

**Ordinary visits are redirected.** GET/HEAD on the origin 301s to
`https://authors.writeitgreat.com` + path + query — every campaign link points
at `/author/register`, so the query string is preserved deliberately.

**Machine callers deliberately keep working on the origin**
(`edge_trust.BYPASS_EXEMPT_EXACT` / `BYPASS_EXEMPT_PREFIXES`):

| Path | Why it must not be redirected |
|---|---|
| `/healthz` | An external probe watches it *there on purpose*, to tell "the dyno is down" from "the edge is down". Redirected, it would measure Cloudflare and stay green through an origin outage. `deploy.yml`'s `APP_URL` is this path for the same reason, and `ci/check_edge_trust.py` asserts the two stay in step. |
| `/api/*` | The Wix caller POSTs `/api/submit` with an `X-API-Key` and polls `/api/status/<id>` with a GET. A cross-host redirect breaks the CORS preflight and, in most HTTP clients, drops the credential header. |
| `/.well-known/*` | A broken certificate renewal is an expensive way to find out the exception was needed. |

POSTs and OPTIONS are never redirected, so a stale tab still submits — now
limited on its real address.

**If Cloudflare changes its IP ranges** the vendored list goes stale and
traffic through a new edge stops being trusted: limits quietly stop firing and
visitor counts quietly inflate. A degradation, not an outage, failing toward
"limit less" rather than locking authors out. The canary is a log line
containing `cloudflare-range-drift`. Refresh from
`https://www.cloudflare.com/ips-v4` (and `-v6`) and bump `RANGES_FETCHED` here
**and in `website/app/edge_trust.py`**, which carries the same list.

---

## How we work now

**One rule: nothing reaches production except by merging a PR into `main`.**

```
   branch  ──▶  push to GitHub  ──▶  open PR  ──▶  CI runs  ──▶  review
                                                                   │
                                                          merge to main
                                                                   │
                                              .github/workflows/deploy.yml
                                                                   │
                                               git push → Heroku → build
                                                                   │
                                                     release phase (migrations)
                                                                   │
                                                    post-deploy smoke check (200?)
```

There is no other route. `main` is branch-protected, so you cannot push to it
directly; the deploy workflow only triggers on a push to `main`, so an unmerged
branch cannot reach Heroku.

### The thing that broke last time

Anna has been deploying from her laptop with `git push heroku main`. That works,
and it is exactly the problem: Heroku ends up holding commits that are not in
GitHub. From then on GitHub is no longer a record of what is running in
production. Nobody can review it, nobody can roll back to a known state, and the
next person who deploys from GitHub silently reverts Anna's work.

`ci/deploy_heroku.sh` now refuses to deploy when it detects this. It fetches
Heroku's branch first, and if Heroku has commits GitHub does not, it stops and
prints them:

```
=== HEROKU AND GITHUB HAVE DIVERGED ===
1 commit(s) exist on Heroku that are NOT on GitHub main:
2b8b617 Anna's laptop hotfix
```

That is a deliberate hard stop. Recovering the work is easy, but it has to be
done through a PR:

```bash
git remote add heroku https://git.heroku.com/writeitgreat-website.git
git fetch heroku main
git checkout -b rescue-heroku-work heroku/main
git push -u origin rescue-heroku-work
# open a PR from rescue-heroku-work into main, review it, merge it
```

**Practically: remove the `heroku` git remote from your local clones.**

```bash
git remote remove heroku
```

If it isn't there, muscle memory can't push to it.

### Genuine emergencies

If the site is down at 2am and you cannot wait for a review:

1. Still open a PR. CI takes a few minutes.
2. If CI itself is broken, admins can use "Merge without waiting for
   requirements" — `enforce_admins` is deliberately `false`. GitHub records
   every override.
3. If Heroku itself is the problem, roll back rather than pushing new code:
   ```bash
   heroku releases --app writeitgreat-website
   heroku releases:rollback --app writeitgreat-website
   ```
   A rollback does not touch GitHub, so it does not cause divergence.

---

## One-time setup

### 1. Create the Heroku API key

Do this **once per repo**, as someone with access to the Heroku app. The
long-lived authorization token is what GitHub Actions authenticates with.

```bash
heroku login
heroku authorizations:create -d "GitHub Actions deploy - website"
```

Copy the **Token** value from the output (not the ID).

> Do not use `heroku auth:token`. On a normal login that returns a short-lived
> session token which expires and will silently break your deploys.

Access note (verified 2026-07-20): `ray@writeitgreat.com` **is** a collaborator
on both apps — `heroku apps` lists `proposal-evaluation`, `writeitgreat-website`,
`uplevelbooks` and `wig-dashboard`, all owned by `andy@writeitgreat.com`. Ray can
create the authorization himself. To add someone else:

```bash
heroku access:add anna@writeitgreat.com --app writeitgreat-website --permissions deploy,view
```

The token inherits the permissions of whoever created it, so create it from an
account that has deploy rights on the app.

### 2. Set the repo secret

```bash
gh secret set HEROKU_API_KEY --repo writeitgreat-llc/website
gh secret set HEROKU_API_KEY --repo writeitgreat-llc/proposal-evaluation
```

`gh` prompts for the value and does not echo it. To pipe it instead:

```bash
printf '%s' "$TOKEN" | gh secret set HEROKU_API_KEY --repo writeitgreat-llc/website
```

Verify (this shows names and dates only, never values):

```bash
gh secret list --repo writeitgreat-llc/website
gh secret list --repo writeitgreat-llc/proposal-evaluation
```

The same token can be used for both repos, but separate authorizations are
better: you can revoke one without breaking the other.

```bash
heroku authorizations          # list
heroku authorizations:revoke <id>
```

### 3. Apply branch protection

```bash
./ci/setup_branch_protection.sh            # both repos
./ci/setup_branch_protection.sh website    # just one
DRY_RUN=1 ./ci/setup_branch_protection.sh  # show the payload, change nothing
```

Requires admin on the repo (Ray has it on both) and `gh auth login`.

What it sets on `main`:

| Setting | Value | Why |
|---|---|---|
| Require a PR before merging | yes | no direct pushes to `main` |
| Required approving reviews | 1 | second pair of eyes; `REVIEWS=0` to disable |
| Dismiss stale approvals | yes | an approval applies to reviewed code, not to whatever gets pushed afterwards |
| Required status checks | see table at top | CI must be green |
| Require branches up to date (`strict`) | yes | two individually-green PRs cannot break `main` together |
| Force pushes | blocked | `main` history is the deploy record |
| Branch deletion | blocked | |
| Conversation resolution | required | review comments cannot be merged past |
| `enforce_admins` | **false** | deliberate emergency escape hatch, audit-logged |

### 4. Prove it works

Open one throwaway PR in each repo and confirm the required checks turn green
rather than sitting at *"Expected — Waiting for status to be reported"*. If they
hang, the check names do not match — see the next section.

---

## The check-name coupling (read this before renaming anything)

A required status check is identified by the **job name GitHub reports**, which
is the job's `name:` field, or the job key if there is no `name:`. Those strings
are hardcoded in `ci/setup_branch_protection.sh`:

```
.github/workflows/ci.yml            ci/setup_branch_protection.sh
--------------------------          -----------------------------
jobs.intake-suite.name       ───▶   "Intake regression suite"
jobs.static-checks.name      ───▶   "Engine parity and template integrity"
jobs.app-boot.name           ───▶   "App boots on Python 3.11"
jobs.proposal-ci.name        ───▶   "proposal-ci"
```

Rename a job without re-running the script and **every PR in that repo becomes
permanently un-mergeable**, waiting on a check name nothing will ever report.
There is no error message. Fix: re-run `ci/setup_branch_protection.sh` with the
new names.

Two related traps:

- **Never add a `paths:` filter to a CI workflow whose job is a required check.**
  A path-filtered workflow does not report "skipped" — it reports nothing, and
  the PR hangs exactly the same way. Both `ci.yml` files deliberately have no
  path filter.
- **A matrix job reports as `name (matrix-value)`.** Neither repo uses a matrix.
  If one is added, the context names change shape and must be re-applied.

---

## What the deploy workflow does

`.github/workflows/deploy.yml`, triggered only by a push to `main`:

1. **Checks out with `fetch-depth: 0`.** Required. `git push heroku` is a real
   git push; Heroku's builder rejects a shallow clone with
   `shallow update not allowed`. The default depth-1 checkout does not work.
2. **`ci/check_deploy_config.py`** — pre-flight on `Procfile` and `runtime.txt`.
   Fails fast rather than burning a Heroku build.
3. **`ci/deploy_heroku.sh <app>`** — pushes to
   `https://heroku:$HEROKU_API_KEY@git.heroku.com/<app>.git`. No third-party
   action; you can read exactly what it does. It:
   - looks up whether Heroku builds `main` or `master` on this app (these two
     apps were created at different times). Pushing to the wrong branch is
     *accepted* by the remote and then silently not built — a green job that
     deployed nothing;
   - detects GitHub/Heroku divergence and stops (see above);
   - redacts the API key from anything it prints.
4. **`ci/smoke_deploy.sh <url>`** — curls the live URL with retry and backoff
   until it returns 200, and fails the job loudly if it never does.

### Concurrency

```yaml
concurrency:
  group: deploy-heroku-writeitgreat-website
  cancel-in-progress: false
```

Two merges close together queue instead of racing, so an older commit can never
land on top of a newer one. `cancel-in-progress` is `false` on purpose:
cancelling a half-finished push or release phase (which runs migrations) leaves
production in an unknown state. Deploying twice in order is strictly better.

### Re-deploying or forcing

Actions → *Deploy to Heroku* → **Run workflow**. The `force_push` input
overwrites diverged Heroku history — destructive, and only correct once you are
certain the Heroku-only commits are worthless.

---

## The release phase (migrations)

**`proposal-evaluation` already has one** and it is correct:

```
release: python migrate.py
web: gunicorn app:app --timeout 120 --workers 1 --threads 4
```

`migrate.py` runs `db.create_all()` then `run_migrations(strict=True)`.

**A release-phase command aborts the release only if it exits non-zero.** That
is the whole mechanism, and it is worth stating plainly because this file used
to claim the outcome without the condition. `flask db upgrade` (the marketing
site) propagates failure natively. A hand-rolled script does so only if it was
written to.

> **Correction, 31 July 2026.** Until this date the sentence here read "a failed
> migration aborts the release" for `proposal-evaluation`, and it was **false**.
> `run_migrations()` wrapped all ~74 operations in `try/except` blocks that
> printed the error and continued, so it could essentially never raise,
> `migrate.py` always exited 0, and a migration that silently did not apply
> shipped anyway. `fix_schema.py` in the repo root is the emergency repair
> written the time that happened. Nobody was watching for it, because this file
> said someone was.

What is true now:

- **Failures are fatal.** Every operation is still attempted, but failures are
  collected and raised as a `MigrationError` naming all of them. `migrate.py`
  prints `=== RELEASE ABORTED: MIGRATION FAILED ===` and exits 1, so Heroku
  discards the release and the previous dynos keep serving.
- **Applied work is not redone.** Completed operations are recorded in a
  `schema_migrations` ledger and skipped. A release with no schema change issues
  **zero DDL**. This matters more than it sounds: Heroku runs the release phase
  for *every* release, including `heroku config:set` and
  `heroku releases:rollback`, and DDL that has to wait for a lock blocks every
  query queued behind it. A rollback must never be the thing that jams.
- **The ledger is verified, not trusted.** Each run reconciles it against the
  live catalog in one query; a row claiming a column the database does not have
  is deleted and the operation re-applies. A restored backup cannot make a
  migration disappear.
- **Web dynos no longer migrate at boot.** The release phase owns the schema.
  Off Heroku (laptop, CI, tests) boot migration still runs, because there is no
  release phase there.
- **The result is checked against the models, not just against the list.** All
  three points above are statements about the operations somebody remembered to
  write. After the last of them, `schema_drift()` asks the database what columns
  it actually has and compares that with what the models declare; a gap is a
  failed release. See *Adding a column* below for why nothing else could catch
  it.

**Reading a release log.** Every run prints one summary line, including when it
did nothing — silence and "it never ran" have to be distinguishable:

```
Schema check: all 234 model column(s) present.
Migrations: 75 tracked, 75 already applied, 0 verified against the database,
0 applied now, 0 failed | repairs: 4 run, 0 warned [strict=on, backend=postgresql]
```

`0 applied now, 0 failed` is a healthy no-op release. `repairs` are the four
data-fix operations that re-run every time on purpose and are never fatal.
`already applied` counts only failures that are operations, so a `schema:drift`
or `ddl:create_all` failure does not quietly shrink it.

### Adding a column

Two steps, and the second is the one people forget:

1. add the `db.Column` to the model;
2. add a matching `_add('table', 'column TYPE')` in `run_migrations()`.

`db.create_all()` means "make every table in the metadata exist". It does not,
and cannot, add a column to a table that already exists. So step 1 alone works
perfectly on every empty database — your laptop, CI, a fresh review app — and
does nothing at all to production, where the table has been there for months.
The column is simply never created and every request touching that table 500s.
`fix_schema.py` in the repo root is the emergency repair written the last time
this happened, for five `coaching_enrollment` columns.

Two things now catch it, and they are deliberately at different times:

- **In the pull request.** `ci/check_migrations.py` runs an *upgrade replay*:
  it builds a database from the models on the base commit — which is what
  production has — and runs this branch's `migrate.py` against it, exactly as
  Heroku will. That is the only check in this repo that ever meets a database it
  did not build from the models under test. It is also the only place a new
  `ALTER TABLE` is really executed: against a fresh database every `_add()`
  finds its column already made by `create_all()` and adopts it without issuing
  any DDL, so a malformed column definition passes every other check here.
- **At release time, against the real database.** `schema_drift()` runs at the
  end of every `run_migrations()`. If a column the models declare is not in the
  database, the release aborts and the old dynos keep serving. `MIGRATIONS_STRICT=0`
  overrides this like any other migration failure.

The drift check is **one-directional on purpose**: a column the database has and
no model declares is a dropped column, which is normal. Production carries four
(`author_module_progress.approved_at`, `.reminder_sent_at`, `.started_at`,
`homework_submission.reviewed_at`). Failing on those would make every model
cleanup a blocked deploy, and the check would be switched off within a week.

It compares **names only** — not types, widths, defaults or nullability — which
is the same contract `col:` ledger keys have. To *change* an existing column,
give the change its own new ledger key; neither the ledger nor the drift check
will notice a type that quietly differs.

### Adding an index, a unique constraint or a foreign key

`_add()` emits `ADD COLUMN` and nothing else. So an `index=True`, `unique=True`
or `ForeignKey()` on a column added *after* its table was created never reaches
the database on its own — those are only ever built by `create_all()`, at
`CREATE TABLE` time, which a live table has long since passed. Use `_index()`
and `_foreign_key()` in `run_migrations()`, next to the `_add()` that created
the column.

Both verify the **shape**, not just the name, on both backends. `CREATE UNIQUE
INDEX IF NOT EXISTS` matches the name only, so an index of the right name and
the wrong shape would let the statement succeed while the uniqueness it promises
does not exist — that is a failed release, never an adoption.

Neither is `CONCURRENTLY`, and that is a decision. `CREATE INDEX CONCURRENTLY`
cannot run inside a transaction block, so it could not commit with its ledger
row — the atomicity every other operation depends on. These tables are tens of
rows (measured 5 August 2026: `proposal` 37, `author` 35, `marketing_module_data`
5, `social_strategy` 3), where the build is sub-millisecond. **On a table of real
size that answer flips**, and the right move is then a one-off script, not a
weaker release phase.

Foreign keys are added **validated**, not `NOT VALID`. The scan is over tens of
rows. A failure means orphan rows exist, and the fix is the rows — a `NOT VALID`
constraint nobody ever validates is a foreign key that enforces nothing about
the data it was added to protect.

**Adding a foreign key changes DELETE behaviour.** PostgreSQL then refuses to
delete a parent while a child still points at it, and this codebase has no ORM
cascades — `grep -c 'cascade=' app.py` returns 0. `delete_author_and_dependents()`
is the hand-written order for `author`, and `ci/check_author_delete.py` is what
keeps it honest: it builds one author with a row in all 13 referencing tables and
deletes them. Add a table that points at `author` and you must add it there too,
child-first, or an admin button starts returning 500.

`proposal` has the same shape one level down — `proposal_note` and
`publisher_proposal` both carry `nullable=False` foreign keys to it — and it was
**already broken in production** until 2026-08-05: both delete paths were a bare
`db.session.delete(proposal)`, so the admin Delete button returned a 500 for any
proposal carrying a note, a publisher share, or an archive toggle. The archive
toggle is the trap: it writes a `ProposalNote` itself, so *archive the junk now
and purge it later* was precisely the workflow that guaranteed the purge would
fail. Bulk delete was worse — one commit at the end of the loop, so a single
proposal with a note aborted the whole batch, deleted nothing, and still flashed
"N proposal(s) deleted." `delete_proposals_and_dependents()` is now the order and
`ci/check_proposal_delete.py` keeps it honest.

That check is not only about a button. Uploaded files live in the database, so
deleting proposals is the **only** way to reclaim that space by hand — a broken
delete is a broken recovery path for the storage headroom check above it.

Four gaps were found and closed this way on 5 August 2026 —
`social_strategy.share_token` (unique + index), `proposal.content_hash` (index),
and foreign keys on `proposal.author_id` and `marketing_module_data.author_id`.
Two of those foreign keys were what made the delete-path repair necessary; one
of them, `social_strategy`, had been silently breaking author deletion for as
long as that table has existed.

`schema_constraint_gaps()` still prints a `Schema note:` line for anything
outstanding, and stays **non-fatal on purpose**. A missing column 500s every
request that touches the table, so refusing to ship is kind. A missing index is
slower and a missing foreign key is unenforced integrity — real, but never a
reason to refuse the unrelated deploy in front of you.

**Two escape hatches**, both config vars, both taking effect on the release that
sets them:

| Var | Effect |
|---|---|
| `MIGRATIONS_STRICT=0` | Ship even though a migration failed. Also **re-enables boot migration**, so the dynos can still repair the schema — the hatch must never leave you worse off than the every-boot behaviour it replaced. Buys a deploy, not a schema: unset it and deploy again once the incident is over. |
| `MIGRATE_ON_BOOT=1` | Force a dyno to migrate at boot, e.g. `heroku run MIGRATE_ON_BOOT=1 python -c "import app"`. |

`ci/check_migrations.py` asserts all of this on every PR, against SQLite *and*
Postgres — including a negative test that poisons a schema and requires
`migrate.py` to exit non-zero, and one that drops a column no `_add()` covers
and requires the same. If that check ever goes green while failures are being
swallowed, the check is broken, not the claim.

The upgrade replay needs the base commit in the clone, which is why `ci.yml`
checks out with `fetch-depth: 0` and passes `SCHEMA_REPLAY_BASE`. If either goes
missing the replay prints `UPGRADE REPLAY SKIPPED` and carries on — losing
earliness, not protection, because the release-time check still runs. Treat a
skip as something to fix, not as a pass.

**The marketing site does NOT have one**, even though it uses Flask-Migrate and
has `migrations/versions/*.py`. `ci/check_deploy_config.py` raises this as a
**warning** on every build (it does not block, since the site deploys fine
today). Recommended fix:

```
release: flask db upgrade
web: gunicorn "app:create_app()" --bind 0.0.0.0:$PORT
```

and set the Heroku config var so `flask` can find the factory:

```bash
heroku config:set FLASK_APP="app:create_app()" --app writeitgreat-website
```

Consequence of leaving it as-is: after a deploy that adds a column, the new code
boots against the old schema and every page touching that column 500s until
someone notices and runs `heroku run flask db upgrade` by hand. With a release
phase, that deploy simply fails and the old site stays up.

---

## Monitoring

Three independent things, and they answer different questions: `/healthz` tells
you **whether the app is up**, Sentry tells you **what broke**, and the storage
headroom check tells you **what is about to break**. None replaces the others and
none is on by default — `/healthz` needs a monitor pointed at it, Sentry needs a
`SENTRY_DSN` set, and the headroom check needs `DATABASE_ALLOTMENT_BYTES` to
match the plan you are actually on.

### Database storage headroom

Uploaded proposals, one-pager audio feedback and knowledge-base documents are
stored **in the database** as `bytea`, so database storage is a resource this app
can exhaust by doing its job. Nothing measured it until 2026-08-05.

`check_database_headroom()` runs on the hourly background loop. Every tick it
logs the measurement, whatever the level:

```
db.storage used=22.5MB allotment=64GB ratio=0.0003
```

Grep for `db.storage` to confirm it is alive. That line is deliberate: the two
log alarms this account shipped in August 2026 were both silently broken — they
searched for text that never appears — and a continuous measurement is the
cheapest defence against a third.

Over `DATABASE_HEADROOM_WARN_RATIO` (default `0.70`) it logs at ERROR, which
Sentry captures, and emails `TEAM_EMAILS` **once a day**.

| var | default | notes |
|---|---|---|
| `DATABASE_ALLOTMENT_BYTES` | `68719476736` (64 GB) | **Must match the current plan.** standard-0 is 64 GB. |
| `DATABASE_HEADROOM_WARN_RATIO` | `0.70` | Drive it to `0.0000001` for one tick to prove the alarm fires, then unset it. |

**Change `DATABASE_ALLOTMENT_BYTES` in the same change as any `pg:upgrade` or
plan move.** There is no Heroku API this app can read for "how big is my plan",
so a stale value reports healthy headroom against a plan you are no longer on —
which is worse than no number at all. This app has already been caught by exactly
that: it moved from essential-0 (1 GB) to standard-0 (64 GB) on 2026-08-04 and
the in-code arithmetic about "~200 uploads away" stayed behind for a week.

**Why 70% and not Heroku's own emails.** Heroku sends nothing until 100%, and its
standard-tier enforcement only revokes `INSERT`/`CREATE`/`UPDATE` after a database
has been over **150%** for **30 continuous days**. That is a generous ladder, not
a cliff — but every rung of it arrives after the point where you would have wanted
to know. See <https://devcenter.heroku.com/articles/heroku-postgres-over-plan-capacity>.

**`/healthz` cannot cover this.** It proves liveness with `SELECT 1`, and Heroku's
enforcement leaves `SELECT` working while blocking writes — so during a real
storage outage the uptime monitors would stay green while every upload failed.
That is the gap this check exists to fill, and it is why the check is not simply
another field on `/healthz`.

**Reclaiming space is not symmetric with filling it.** Deleting rows marks space
reusable; it does not shrink what Heroku measures. That needs `VACUUM FULL`, which
takes an exclusive lock — i.e. downtime. Plan for the alert, not the cleanup.

### `/healthz`

```bash
curl -i https://authors.writeitgreat.com/healthz
```

Public, unauthenticated, never rate limited, never cached. It runs a real
`SELECT 1`, so it reports "the dyno is up and the database is not" — the failure
a static 200 hides.

**The response shape is a three-app contract.** `writeitgreat.com`,
`authors.writeitgreat.com` and `app.writeitgreat.com` all answer `GET /healthz`
identically so that **one** uptime-monitor template covers the fleet:

| | healthy | unhealthy |
|---|---|---|
| status | `200` | `503` |
| `ok` | `true` | `false` |
| `db` | `"ok"` | `"error"` |
| `release` | Heroku release/commit, or `null` | same |
| `time` | `2026-08-03T16:04:08.486570+00:00` | same |

Headers on both: `Cache-Control: no-store, no-cache, must-revalidate` and
`CDN-Cache-Control: no-store`.

Configure the monitor as **`GET /healthz`, expect `200`, body contains
`"ok":true`** — that rule is valid against all three apps unchanged.

Things worth knowing before you change anything here:

- **`time` is timezone-aware UTC** (`+00:00`, not a bare `Z`). All three apps
  emit the same form; a parser fed one of each will eventually be fed the wrong
  one during an incident.
- **Renaming a key is a three-repo change.** It does not break this app, it
  breaks the monitor for all three, silently — a keyword rule that stops
  matching reads as "down", or as "up forever", depending on how it was written.
  `time` was called `checked_at` here until 2026-08-03; that is the last time it
  is allowed to differ.
- **The body never contains the exception.** A SQLAlchemy/psycopg2 connection
  error renders the DSN — host, database, sometimes the password — into its
  `str()`, and this endpoint is public. Detail goes to the log; the caller gets
  the word `error`. Do not "improve" the diagnostics here.
- **A DB failure is logged, not sent to Sentry.** During an outage every real
  request is already failing and already being reported; capturing here would
  re-report a known outage once per poll and eat the burst budget that carries
  the events saying *which* query broke.
- **The probe is bounded, by three settings covering three different failures.**
  `SET LOCAL statement_timeout = '5s'` in the view bounds a slow or queued query
  on a database that is still answering (skipped on SQLite, which would raise on
  it). `connect_timeout` bounds opening a connection. `tcp_user_timeout` bounds
  an already-open socket that has stopped being answered — and that last one is
  the case a statement timeout can never reach, because the setting would have
  to travel over the wedged socket to take effect. It is also what covers
  `pool_pre_ping`, which runs before the view's first statement.
  Without these, one poll pins one of the four gunicorn threads for the full
  `--timeout 120`, once a minute, and the probe becomes the outage instead of
  reporting it.
  Two caveats: `tcp_user_timeout` is a **Linux-only** libpq option, real on a
  dyno and ignored on a Mac, so it cannot be reproduced locally; and there is
  deliberately **no session-wide `statement_timeout`**, because `migrate.py`
  imports this config during the release phase and a ceiling there would abort a
  long migration and fail the deploy.
- **`release` is `null` until someone enables the dyno metadata:**

  ```bash
  heroku labs:enable runtime-dyno-metadata -a proposal-evaluation
  ```

  Until then this field is honestly `null` rather than wrong, and Sentry events
  also ship with no release (which is what kills commit/stack-frame
  association). Setting `SENTRY_RELEASE` explicitly works too.

`/healthz` is asserted on by `ci/smoke_app.py`, so a 404 or a 503 in CI fails the
build before it can page anybody.

### Sentry (`SENTRY_*` config vars)

All of the wiring lives in `observability.py`, which is vendored
**byte-identically** into all three Write It Great repos — do not edit it in one
place. `ci/check_sentry_scrub.py` (a step in `proposal-ci`) proves it still
scrubs; `CONFIG_VERSION` in that file is the only cross-repo drift detector
there is, and it rides on every event as the `sentry_config` tag.

Every event also carries a **`process_type`** tag naming the kind of process
that produced it, derived from Heroku's `DYNO` variable: `web` (a real visitor),
`scheduler`, `release` (the migration phase), `run` (a human running a script by
hand), or `local` off Heroku entirely. It exists because a one-off
`heroku run` script that crashes imports the app, therefore starts Sentry, and
therefore reports itself as an unhandled production error — an alert saying the
site is down when it is not. Nothing is suppressed: a *scheduled* job failing at
3am is exactly what this is all for. If you want a quieter inbox, narrow it on
the alert rule in the Sentry UI, where it is visible and reversible, rather than
in code that ships to three repos.

Do not go looking for a search box. A Sentry **issue alert rule** accepts no
query string anywhere — `!process_type:run` is Issues-*search* syntax from a
different screen, and these notes used to print it as if you could paste it into
a rule. The real path, confirmed in the UI on 2026-08-04 and already applied to
the live `wig-website` rule:

> **Alerts → the rule → Edit → the `IF` block →** change the `Any event`
> dropdown to **"The event's tags match {key} {match} {value}"**, then key
> `process_type`, match `does not equal`, value `run`.

⚠️ The `wig-authors` rule has never been opened and may carry the same drift
found on `wig-website` — priority-based triggers rather than every-new-issue,
and default recipients rather than an explicit address.

| Var | Default | What it does |
|---|---|---|
| `SENTRY_DSN` | unset | **The on switch.** Unset, `init_sentry()` returns `False` having done nothing — no client, no traffic, nothing to leak. That is why CI, `ci/smoke_app.py` and laptops are silent. |
| `SENTRY_ENVIRONMENT` | `production` | Environment tag on every event. |
| `SENTRY_RELEASE` | unset | Overrides release detection. Prefer the 40-char commit SHA — Sentry maps stack frames to commits with a SHA and cannot with `v62`. |
| `SENTRY_SAMPLE_RATE` | `1.0` | Fraction of error events sent. Lower it only during an incident. |
| `SENTRY_MAX_EVENTS_PER_HOUR` | `15` | Per-process hourly ceiling. Bounds a burst, not a month. |
| `SENTRY_MAX_PER_FINGERPRINT_PER_HOUR` | `3` | Per-issue hourly ceiling, so one hot loop cannot crowd out everything else. |
| `LOG_LEVEL` | `INFO` | Logging threshold. **It also gates Sentry**: `LoggingIntegration` turns `ERROR` records into events, and a record the logger drops never reaches Sentry, so `LOG_LEVEL=CRITICAL` silently turns log-derived events off. |

**THIS REPO IS PUBLIC.** The DSN lives in Heroku config vars only. Never write
one into a file here, never add a fallback, never put one in a workflow.

```bash
heroku config:set SENTRY_DSN='https://…@…ingest.sentry.io/…' -a proposal-evaluation
heroku config:unset SENTRY_DSN -a proposal-evaluation   # the off switch
```

Two things that are deliberate and look like bugs:

- **`init_sentry()` is wrapped in `try/except Exception: pass`.** Monitoring must
  never be able to take the app down. `import app` *is* the release phase here
  (`release: python migrate.py` imports it), so unwrapped, a typo'd `SENTRY_DSN`
  would raise `BadDsn` and abort a **schema** release over a config var that has
  nothing to do with the schema — blocking the deploy of an already-merged
  `main`. It is silent because logging is configured a few lines later; check
  the `sentry_config` tag on events if you need to know Sentry came up.
- **`hide_parameters: True` in `SQLALCHEMY_ENGINE_OPTIONS`** is not redundant
  with the scrubbing in `observability.py`. That scrubbing runs in `_before_send`,
  a Sentry hook, which does not run at all when `SENTRY_DSN` is unset and never
  sees the log stream anyway. Without `hide_parameters`, a failed `INSERT` writes
  the bound values — author email, password hash, proposal text — into the
  exception message, which `logger.exception()` puts on stdout, which on Heroku
  is the log drain. One protects Sentry, the other protects the logs. Keep both.

Recommended order for turning DSNs on, one app per week: `wig-dashboard` first
(internal only, so a scrubbing mistake exposes staff data rather than
customers'), **this app** second — it carries manuscripts, so read the first
day's events for stray `[parameters: …]` before trusting it — and the marketing
site last.

---

## Running the checks locally

Everything CI runs is a committed script — no CI-only magic.

```bash
# marketing site
bash ci/run_intake_tests.sh          # 175-assertion intake suite
python3 ci/check_engine_parity.py    # shipped page vs tested prototype
python3 ci/check_templates.py
python3 ci/check_app_boots.py        # needs Python 3.11
python3 ci/check_deploy_config.py

# proposal tool
OPENAI_API_KEY=dummy python3 ci/smoke_app.py
python3 ci/check_undeclared_imports.py
python3 ci/check_deploy_config.py
python3 ci/check_migrations.py       # add MIGRATION_TEST_DATABASE_URL for the
                                     # Postgres half -- see below
```

`ci/check_migrations.py` runs against SQLite on its own. Give it a throwaway
Postgres and it also exercises the half production actually uses:

```bash
createdb wig_migcheck
MIGRATION_TEST_DATABASE_URL=postgresql://localhost/wig_migcheck \
  python3 ci/check_migrations.py
```

It creates and drops its own private schema per scenario, so it never touches
anything in `public`.

The two delete checks take a Postgres URL the same way, and **need one to mean
anything** — SQLite does not enforce foreign keys unless `PRAGMA foreign_keys` is
on, so the SQLite leg cannot fail for the reason either check exists. Both say so
loudly when the variable is unset:

```bash
createdb wig_delcheck
AUTHOR_DELETE_TEST_DATABASE_URL=postgresql://localhost/wig_delcheck \
  python3 ci/check_author_delete.py
PROPOSAL_DELETE_TEST_DATABASE_URL=postgresql://localhost/wig_delcheck \
  python3 ci/check_proposal_delete.py
```

Note for the marketing site: it needs **Python 3.10+** (`app/__init__.py` uses
`str | None`). On a 3.9 machine the app-boot and route checks cannot run locally
— CI on 3.11 is where they get verified.

`OPENAI_API_KEY` for the proposal tool must be a **dummy** value. `app.py`
constructs an OpenAI client at import time and raises without it, but nothing in
CI calls OpenAI. Never put a real key in a workflow file — that repo is public.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| PR stuck on "Expected — Waiting for status to be reported" | required check name ≠ job name | re-run `ci/setup_branch_protection.sh` with the current names |
| `shallow update not allowed` | checkout without `fetch-depth: 0` | already set; check nobody removed it |
| `HEROKU AND GITHUB HAVE DIVERGED` | somebody pushed straight to Heroku | rescue-branch PR (see above) |
| Deploy green, site unchanged | pushed to the non-default Heroku branch | `deploy_heroku.sh` detects this; check its "Heroku deploy branch:" line |
| `DEPLOY SMOKE FAILED` | slug built, app crashed on boot | `heroku logs --tail --app <app>`, then `heroku releases:rollback` |
| `Invalid credentials provided` on push | expired/revoked token | new `heroku authorizations:create`, re-run `gh secret set` |

**A failing smoke check does not roll anything back.** The code is already live
at that point. Roll back explicitly if it is bad.
