#!/usr/bin/env python3
"""Import app.py, compile every template, and render the public pages.

app.py is ~7900 lines with 109 routes and reads OPENAI_API_KEY at import time to
construct the OpenAI client -- so importing it at all is a meaningful check, and
it is the check that catches "someone pushed a NameError to production".

What this does:
  1. imports app.py with a DUMMY OPENAI_API_KEY (no network calls are made at
     import time -- the client is only constructed, never used)
  2. asserts the route table is populated and looks sane
  3. compiles every template in templates/ so a Jinja syntax error fails the PR
     instead of 500-ing for an author
  4. runs the release-phase migration path (db.create_all + run_migrations)
     against a throwaway SQLite file -- this is exactly what migrate.py does on
     Heroku, so a migration that would abort the release fails the PR first
  5. renders the unauthenticated public pages

IMPORTANT: OPENAI_API_KEY must be a dummy value, never a real key. Nothing here
talks to OpenAI.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

SMOKE_DB = REPO_ROOT / "ci-smoke.db"

# app.py reads these at import time. Set them BEFORE importing.
os.environ.setdefault("OPENAI_API_KEY", "sk-ci-dummy-key-not-a-real-credential")
os.environ["DATABASE_URL"] = "sqlite:///" + str(SMOKE_DB)
os.environ.setdefault("SECRET_KEY", "ci-smoke-test-key")
# Keep _is_production False so secure-cookie config does not break the test client.
os.environ["APP_BASE_URL"] = "http://localhost:5000"

# (path, allowed status codes)
PUBLIC_ROUTES: list[tuple[str, tuple[int, ...]]] = [
    # "/" requires login and redirects anonymous users to author_login.
    ("/", (302, 308)),
    ("/author/login", (200,)),
    ("/author/register", (200,)),
    ("/publisher/login", (200,)),
]

# The marketing site hard-links to this exact path from several pages.
# If it ever stops existing, every "Submit your proposal" CTA 404s.
CONTRACT_ROUTES = ["/author/register"]


def main() -> int:
    if SMOKE_DB.exists():
        SMOKE_DB.unlink()

    try:
        import app as application
    except Exception:
        print("FAILURE: could not import app.py.", file=sys.stderr)
        traceback.print_exc()
        return 1

    flask_app = application.app
    print("Imported app.py OK")

    rules = list(flask_app.url_map.iter_rules())
    print(f"Registered {len(rules)} url rules")
    if len(rules) < 50:
        print(
            f"FAILURE: only {len(rules)} routes registered -- app.py did not "
            f"finish loading. Expected ~110.",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []

    # --- 1. contract routes the marketing site depends on ---------------------
    registered_paths = {str(rule.rule) for rule in rules}
    for path in CONTRACT_ROUTES:
        if path in registered_paths:
            print(f"  ok  contract route exists: {path}")
        else:
            failures.append(
                f"contract route {path} no longer exists. writeitgreat.com links "
                f"straight to https://authors.writeitgreat.com{path} from several "
                f"pages -- renaming it breaks every proposal CTA on the marketing site."
            )

    # --- 2. compile every template -------------------------------------------
    templates_dir = REPO_ROOT / "templates"
    template_count = 0
    for template_path in sorted(templates_dir.rglob("*.html")):
        rel = template_path.relative_to(templates_dir).as_posix()
        try:
            flask_app.jinja_env.get_template(rel)
            template_count += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"template {rel} failed to compile: {exc}")
    print(f"Compiled {template_count} template(s) from {templates_dir.name}/")

    # --- 3. the release-phase migration path ---------------------------------
    # This mirrors migrate.py, which Heroku runs as `release: python migrate.py`.
    # If this raises, the Heroku release would abort -- catch it in the PR.
    try:
        with flask_app.app_context():
            application.db.create_all()
            application.run_migrations()
        print("db.create_all() + run_migrations() OK (release phase would succeed)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"release-phase migration path raised {type(exc).__name__}: {exc}")
        traceback.print_exc()

    # --- 4. render public pages ----------------------------------------------
    client = flask_app.test_client()
    print("\nRendering public pages:")
    for path, allowed in PUBLIC_ROUTES:
        try:
            response = client.get(path, follow_redirects=False)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()
            continue

        if response.status_code in allowed:
            print(f"  ok  {response.status_code}  {path}")
        else:
            expected = "/".join(str(code) for code in allowed)
            failures.append(f"{path} returned {response.status_code}, expected {expected}")
            print(f"  FAIL {response.status_code}  {path}", file=sys.stderr)
            print(response.get_data(as_text=True)[:2000], file=sys.stderr)

    if SMOKE_DB.exists():
        SMOKE_DB.unlink()

    if failures:
        print("\n=== APP SMOKE FAILED ===", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\n=== APP SMOKE OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
