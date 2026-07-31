# Deployment

How code gets from a laptop to production, for both Write It Great apps.
This file is identical in both repos on purpose — whichever one you are in, the
rules are the same.

| | Marketing site | Proposal tool |
|---|---|---|
| Repo | `writeitgreat-llc/website` (private) | `writeitgreat-llc/proposal-evaluation` (public) |
| Heroku app | `writeitgreat-website` | `proposal-evaluation` |
| Heroku default branch | `main` (verified) | auto-detected at deploy time |
| Deployed URL | https://writeitgreat-website-be6985a92063.herokuapp.com/ | https://proposal-evaluation-20d7e1515843.herokuapp.com/ |
| Public URL | https://writeitgreat.com | https://authors.writeitgreat.com |
| CI check names | `Intake regression suite`, `Engine parity and template integrity`, `App boots on Python 3.11` | `proposal-ci` |
| Python | 3.11.11 (`runtime.txt`) | 3.11.7 (`runtime.txt`) |
| Release phase | `flask db upgrade` | `release: python migrate.py` |

---

## ⚠️ Open question: which app serves authors.writeitgreat.com?

**Confirm this with Andy before trusting the proposal-tool pipeline.**

`heroku domains -a proposal-evaluation` lists **no custom domains**, yet
`authors.writeitgreat.com` resolves to a `herokudns.com` target
(`molecular-mandrill-tf33ef4jp15w48r2zm06lz31.herokudns.com`). That domain is
not attached to any of the four apps visible to `ray@writeitgreat.com`
(`proposal-evaluation`, `writeitgreat-website`, `uplevelbooks`,
`wig-dashboard`), which suggests a fifth Heroku app in an account we cannot
list.

Both URLs currently serve byte-identical pages, which is consistent with either
"same app, domain listing hidden from collaborators" or "the same code deployed
twice to two different apps".

If it is a second app, **merging a PR will not update what authors see**. The
deploy workflow therefore smokes *both* URLs: the `herokuapp.com` one is the
real gate (it proves the app we pushed to is healthy); the custom domain is a
secondary check.

Resolve it with either:

```bash
heroku domains:add authors.writeitgreat.com -a proposal-evaluation
```

or by pointing `HEROKU_APP_NAME` in `.github/workflows/deploy.yml` at whichever
app actually owns the domain. Delete this section once it is settled.

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

**Reading a release log.** Every run prints one summary line, including when it
did nothing — silence and "it never ran" have to be distinguishable:

```
Migrations: 69 tracked, 69 already applied, 0 verified against the database,
0 applied now, 0 failed | repairs: 4 run, 0 warned [strict=on, backend=postgresql]
```

`0 applied now, 0 failed` is a healthy no-op release. `repairs` are the four
data-fix operations that re-run every time on purpose and are never fatal.

**Two escape hatches**, both config vars, both taking effect on the release that
sets them:

| Var | Effect |
|---|---|
| `MIGRATIONS_STRICT=0` | Ship even though a migration failed. Also **re-enables boot migration**, so the dynos can still repair the schema — the hatch must never leave you worse off than the every-boot behaviour it replaced. Buys a deploy, not a schema: unset it and deploy again once the incident is over. |
| `MIGRATE_ON_BOOT=1` | Force a dyno to migrate at boot, e.g. `heroku run MIGRATE_ON_BOOT=1 python -c "import app"`. |

`ci/check_migrations.py` asserts all of this on every PR, against SQLite *and*
Postgres — including a negative test that poisons a schema and requires
`migrate.py` to exit non-zero. If that check ever goes green while failures are
being swallowed, the check is broken, not the claim.

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
