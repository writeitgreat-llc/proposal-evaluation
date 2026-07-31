"""
Standalone schema fix — run with: heroku run python fix_schema.py

This script does NOT import app.py. It connects directly to DATABASE_URL
and adds the missing coaching_enrollment columns. Safe to run multiple times
(uses IF NOT EXISTS). No lock_timeout — it will wait for any locks to clear.

KEEP THIS FILE. It looks like dead code and it is not.

It was written after a migration silently failed to apply in production — the
incident that run_migrations()'s strict mode and `schema_migrations` ledger now
exist to prevent. It remains the only repair path that works when app.py itself
cannot be imported or booted, which is exactly when you need one.

Two deliberate differences from app.py, neither of which is an oversight:

  * No lock_timeout. run_migrations() gives up after a few seconds rather than
    sit at the head of a lock queue and stall the site. This script is run by
    hand during an incident, when waiting is what you want. Do not "fix" it to
    match the house style.
  * It writes no `schema_migrations` rows. That is harmless: the next
    run_migrations() reads the live catalog, sees the columns are there, and
    records them without touching the tables. The dangerous direction — a
    ledger row for a column that does not exist — is the one run_migrations()
    actively reconciles away.
"""
import os
import sys

db_url = os.environ.get('DATABASE_URL', '')
if not db_url:
    print("ERROR: DATABASE_URL not set")
    sys.exit(1)
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)

try:
    import sqlalchemy
    engine = sqlalchemy.create_engine(db_url)
except Exception as e:
    print(f"ERROR creating engine: {e}")
    sys.exit(1)

columns = [
    ("book_title",          "VARCHAR(500)"),
    ("completed_at",        "TIMESTAMP"),
    ("current_module",      "INTEGER DEFAULT 1"),
    ("welcome_email_sent",  "BOOLEAN DEFAULT FALSE"),
    ("complete_email_sent", "BOOLEAN DEFAULT FALSE"),
]

print("Connecting to database…")
try:
    with engine.begin() as conn:
        # Show current columns
        result = conn.execute(sqlalchemy.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'coaching_enrollment' ORDER BY ordinal_position"
        ))
        existing = [row[0] for row in result]
        print(f"Current columns: {existing}")

        for col_name, col_def in columns:
            stmt = sqlalchemy.text(
                f"ALTER TABLE coaching_enrollment ADD COLUMN IF NOT EXISTS {col_name} {col_def}"
            )
            conn.execute(stmt)
            print(f"  OK: {col_name} {col_def}")

        # Confirm
        result2 = conn.execute(sqlalchemy.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'coaching_enrollment' ORDER BY ordinal_position"
        ))
        after = [row[0] for row in result2]
        print(f"Columns after fix: {after}")

    print("\nSchema fix complete. The app should work now.")
except Exception as e:
    print(f"\nERROR: {e}")
    sys.exit(1)
