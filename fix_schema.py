"""
Standalone schema fix — run with: heroku run python fix_schema.py

This script does NOT import app.py. It connects directly to DATABASE_URL
and adds the missing coaching_enrollment columns. Safe to run multiple times
(uses IF NOT EXISTS). No lock_timeout — it will wait for any locks to clear.
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
