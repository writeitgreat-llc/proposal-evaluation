"""Heroku release-phase migration script — `release: python migrate.py`.

Runs once per release, before any new dyno starts, and is the ONLY thing that
changes the schema in production.

Two things here are load-bearing and easy to undo by accident:

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
