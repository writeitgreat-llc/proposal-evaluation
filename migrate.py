"""Heroku release-phase migration script — `release: python migrate.py`.

Runs once per release, before any new dyno starts, and is the ONLY thing that
changes the schema in production.

Three things here are load-bearing and easy to undo by accident:

1. MIGRATE_ON_BOOT=0 is set BEFORE `import app`. Importing app.py runs a
   migration pass at module level, and that pass is deliberately non-strict —
   so if it ran first it would apply (or fail to apply) everything, and the
   strict pass below would find nothing left to do and report success on a
   migration that had already failed and been swallowed. Setting the variable
   here makes the strict pass the first and only pass BY CONSTRUCTION, rather
   than by coincidence of which environment variables Heroku happens to set.

2. strict=True. This is what makes a failed migration abort the release: the
   exception escapes, this script exits non-zero, Heroku discards the release,
   and the previous dynos keep serving. Without it a broken schema ships and
   authors get error pages — which is what happened once already, and why
   fix_schema.py exists in this repo.

3. The database checks below run BEFORE `import app`, and do NOT go through
   _abort(). A missing DATABASE_URL is not a migration that failed — it is no
   database at all — so MIGRATIONS_STRICT=0 must not wave it through. They sit
   before the import because the import is one of the things being guarded:
   app.py's own copy of the presence check raises inside the try below, which
   lands in _abort(), which honours the bypass. Measured without this: a release
   with no DATABASE_URL and the bypass set exits 0, promotes the slug, and every
   new web dyno then refuses to boot — with the old dynos already shut down.

Normal releases do no work at all. Operations that have already been applied are
recorded in the `schema_migrations` ledger and skipped, so a config-var change or
a `heroku releases:rollback` — both of which re-run this script — issue zero DDL
and cannot be blocked by a lock.
"""
import os
import sys

# MUST come before `import app`. See note 1 above.
os.environ['MIGRATE_ON_BOOT'] = '0'

# Read before importing app, because the bypass has to cover the import too.
_soft = os.environ.get('MIGRATIONS_STRICT', '').strip().lower() in ('0', 'false', 'no', 'off')


def _abort(err):
    """Fail the release — unless MIGRATIONS_STRICT=0 says ship anyway.

    The bypass is total on purpose. A bypass that only covers *some* ways the
    schema step can fail is not an escape hatch, it is a surprise during the
    incident you bought it for. When it is used, app.py hands boot migration
    back its old job (see _migrate_on_boot), so the schema still gets a chance
    to catch up on the dynos.
    """
    if _soft:
        print(f"\n=== MIGRATION FAILED, SHIPPING ANYWAY (MIGRATIONS_STRICT=0) ===\n{err}\n"
              f"Boot migration is re-enabled while this is set. Unset it and deploy "
              f"again as soon as the incident is over.", file=sys.stderr)
        sys.exit(0)
    print(f"\n=== RELEASE ABORTED: MIGRATION FAILED ===\n{err}", file=sys.stderr)
    sys.exit(1)


# ── There has to BE a database, and it has to be the real one ────────────────
# See note 3. Both tests below are the release phase's own, deliberately not
# routed through _abort(): this is the one exit MIGRATIONS_STRICT=0 must not
# open, because that hatch is for a migration that failed, and there is no
# migration here to fail.
#
# WHY BOTH TESTS. They close the two halves of the same 2026-08-04 fault.
# Presence catches the absent key — the shape a detached add-on produces, and
# the one that actually happened. The backend test catches the other half the
# first one cannot: a DATABASE_URL that is present but points at SQLite still
# gets a complete schema built on this dyno's throwaway disk, "Migration
# complete." and exit 0, which is a green release with nothing behind it.
#
# The backend test is applied AFTER the postgres:// rewrite and lives HERE and
# NOT in app.py, on purpose: ci/check_migrations.py deliberately pairs
# DYNO=web.1 with a sqlite:/// URL to exercise the boot gate, and the same
# assertion next to app.py's SQLALCHEMY_DATABASE_URI would break it. A release
# dyno has no such legitimate case — production runs on Postgres or it is not
# production.
#
# EXPECTED ALARM, NOT A FAULT: `heroku pg:promote` and a detach/re-attach swap
# both produce one release where DATABASE_URL is momentarily absent, so the
# detach release will now go red and email. Heroku's own CLI already prints
# "It is safe to ignore the failed Detach DATABASE release." The Attach release
# that follows has the variable and succeeds; the site serves off the old dynos
# throughout.
_url = os.environ.get('DATABASE_URL', '').strip()
if _url.startswith('postgres://'):
    _url = _url.replace('postgres://', 'postgresql://', 1)

_FIX = ("Re-attach the Postgres add-on as DATABASE and deploy again: "
        "`heroku addons -a proposal-evaluation` to list them, then "
        "`heroku addons:attach <add-on-name> -a proposal-evaluation --as DATABASE`.\n"
        "If you are seeing this on a `Detach DATABASE` release during a "
        "`heroku pg:promote` or an add-on swap, it is expected and harmless — the "
        "`Attach DATABASE` release that follows has the variable and will succeed.\n"
        "While dynos are refusing to boot, `heroku ps:exec` will not connect. Use "
        "`heroku run bash`, which is a fresh one-off dyno and never imports app.py.")

if os.environ.get('DYNO') and not _url:
    print("\n=== RELEASE ABORTED: DATABASE_URL IS NOT SET ===\n"
          "There is no database to migrate. Without this check the release phase "
          "builds a complete schema in a throwaway SQLite file on this dyno, "
          "prints 'Migration complete.' and promotes a slug with nothing behind "
          "it — which is what a release still recorded as SUCCESSFUL did on "
          "2026-08-04. MIGRATIONS_STRICT=0 does not cover this, deliberately.\n"
          + _FIX, file=sys.stderr)
    sys.exit(1)

if os.environ.get('DYNO') and not _url.startswith('postgresql://'):
    print("\n=== RELEASE ABORTED: DATABASE_URL IS NOT A POSTGRES DATABASE ===\n"
          f"On a dyno the schema must be applied to Postgres, not to "
          f"{_url.split(':', 1)[0]!r}. A SQLite URL here would migrate a file on "
          "this dyno's disk, report success, and promote a slug whose dynos have "
          "nothing to talk to. MIGRATIONS_STRICT=0 does not cover this either.\n"
          + _FIX, file=sys.stderr)
    sys.exit(1)

# Same placement and same reasoning for the signing key: app.py's own guard
# raises inside the try below, which lands in _abort(), which honours the
# bypass — so with MIGRATIONS_STRICT=0 set, a release with no SECRET_KEY would
# promote a slug whose every web dyno then refuses to boot, with the old dynos
# already shut down. A missing signing key is not a migration that failed;
# the bypass must not cover it.
if os.environ.get('DYNO') and not os.environ.get('SECRET_KEY', '').strip():
    print("\n=== RELEASE ABORTED: SECRET_KEY IS NOT SET ===\n"
          "The only fallback is the placeholder printed in this public "
          "repository, and a session signed with a public value is a session "
          "anyone can forge. MIGRATIONS_STRICT=0 does not cover this either.\n"
          "Set a strong random value and deploy again: heroku config:set "
          "SECRET_KEY=<64 random hex chars> -a proposal-evaluation "
          "(generate one with: python3 -c 'import secrets; "
          "print(secrets.token_hex(32))').", file=sys.stderr)
    sys.exit(1)


try:
    from app import app, db, run_migrations, MigrationError  # noqa: E402
except Exception as e:                                        # noqa: BLE001
    _abort(f"could not import app.py: {e!r}")

with app.app_context():
    print("Running db.create_all()...")
    try:
        db.create_all()
        print("Running migrations...")
        run_migrations(strict=True)
    except MigrationError as e:
        # The message already names every operation that failed; a traceback
        # here would only bury it.
        _abort(e)
    except Exception as e:                                    # noqa: BLE001
        # Anything else — a connection blip, a missing table create_all cannot
        # build — is still a reason not to promote this slug.
        _abort(f"{type(e).__name__}: {e}")
    print("Migration complete.")
