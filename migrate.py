"""
Heroku release-phase migration script.
Run once before dynos start: ensures all DB columns exist.
"""
import sys
from app import app, db, run_migrations

with app.app_context():
    print("Running db.create_all()...")
    db.create_all()
    print("Running migrations...")
    run_migrations()
    print("Migration complete.")
