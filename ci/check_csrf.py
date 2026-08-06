#!/usr/bin/env python3
"""check_csrf.py -- the CSRF invariants stay pinned.

App-wide CSRF (Flask-WTF) shipped on this branch's ancestor with exactly three
exemptions, each named for the mechanism that authenticates the caller instead.
Nothing else in CI would notice any of these regressions:

  * someone flips WTF_CSRF_ENABLED off "temporarily" in a debug session and
    commits it -- every form still renders, every test that carries a token
    still passes, and the protection is simply gone;
  * a new route grows @csrf.exempt because a fetch() caller 400'd during
    development -- the right fix is sending the header, the easy fix is the
    decorator, and only an enumerated pin makes the easy fix loud;
  * a new template's <form method="post"> forgets its hidden token -- it 400s
    for real users on submit, or worse, someone "fixes" that with another
    exemption;
  * a new fetch() POST forgets the X-CSRFToken header -- same failure, same
    pressure toward the same wrong fix.

Four sections:

  1. the protection is ON: config not disabled, and -- behaviourally -- a
     token-less POST to /author/register is refused and creates no account
     (the same negative probe shape as ci/check_money_paths.py);
  2. the exemption set is EXACTLY the allowlist below. Both directions fail:
     an unlisted exemption is a new hole, a listed-but-missing one means the
     allowlist is stale and no longer documents reality. Enumeration mirrors
     flask_wtf's own _is_exempt(): a view is exempt iff its blueprint is in
     csrf._exempt_blueprints or its dotted module.name is in
     csrf._exempt_views;
  3. every <form method="post"> in templates/ (case-insensitive, any
     attribute order, multi-line tags included) carries a csrf_token hidden
     input or a {{ csrf_token() }} call -- no exclusions: even a form posting
     to an exempt endpoint renders the input harmlessly;
  4. base.html carries the <meta name="csrf-token"> + window.CSRF_TOKEN
     global, and every file under templates/ and static/ that sends a
     fetch()/XHR POST also references the token (X-CSRFToken /
     window.CSRF_TOKEN / the meta tag) -- except the two analytics partials
     allowlisted below, which post ONLY to the csrf-exempt analytics
     endpoints and whose primary transport (sendBeacon) cannot set headers
     at all.

FAULT DRILL (2026-08-06): each planted alone, confirmed to fail by name,
then reverted:
  * app.py author_login: a bogus @csrf.exempt added
      -> FAIL "unlisted CSRF exemption: author_login ..."
  * templates/author_register.html: the csrf_token hidden input deleted
      -> FAIL "templates/author_register.html: <form method=post> ... has no
         csrf_token"

Plain SQLite on purpose, like every check that imports the app: what this
file pins is wiring, not database semantics.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(REPO_ROOT))

CHECK_DB = REPO_ROOT / "ci-csrf-check.db"
if CHECK_DB.exists():
    CHECK_DB.unlink()

os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ["DATABASE_URL"] = "sqlite:///" + str(CHECK_DB)
os.environ.setdefault("SECRET_KEY", "ci-csrf-check-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"
os.environ["MIGRATE_ON_BOOT"] = "0"
# Same shell hygiene as ci/check_money_paths.py: a laptop's real Turnstile or
# SMTP credentials must not change what this file tests.
for _leak in ("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY",
              "SMTP_USER", "SMTP_PASSWORD", "FUNNEL_EVENTS_TOKEN"):
    os.environ.pop(_leak, None)

import app as appmod  # noqa: E402

failures: list[str] = []
total = 0

# ---------------------------------------------------------------------------
# The pinned exemption set. Endpoint name -> why a session-bound token is NOT
# the control on this endpoint. Mirrors ci/route_guard_allowlist.txt: every
# entry must carry a reason a reviewer can check, and the comparison below is
# EXACT in both directions.
# ---------------------------------------------------------------------------
EXEMPT_ALLOWLIST = {
    "api_submit":
        "machine-to-machine (the Wix server): authenticated by X-API-Key via "
        "hmac.compare_digest; no browser session exists to forge",
    "analytics.set_consent":
        "anonymous by design -- the consent cookie carries no session "
        "privilege to forge (see set_consent()'s docstring)",
    "analytics.collect":
        "anonymous analytics beacon; navigator.sendBeacon cannot set a token "
        "header even if one were demanded",
}

# Files that send a POST without the X-CSRFToken header, each with the reason
# that makes that safe. Keep this list at exactly these two if you can.
JS_EXEMPT_FILES = {
    "templates/_analytics.html":
        "posts only to analytics.collect (csrf-exempt); primary transport is "
        "sendBeacon, which cannot set headers -- the fetch() is its fallback",
    "templates/_consent.html":
        "posts only to analytics.set_consent (csrf-exempt, anonymous by "
        "design)",
}


def check(condition: bool, message: str) -> None:
    global total
    total += 1
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def finish() -> None:
    if CHECK_DB.exists():
        CHECK_DB.unlink()
    print()
    print(f"{total} checks, {len(failures)} failed")
    if failures:
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All CSRF invariants hold.")
    sys.exit(0)


def _fresh_g():
    """Same g-cache trap as ci/check_money_paths.py: flask_wtf caches the
    token and flask_login the user on `g`, which outlives simulated requests
    under one app context. A real request gets a fresh `g`."""
    from flask import g
    g.pop("csrf_token", None)
    g.pop("_login_user", None)


# ===========================================================================
# 1. The protection is ON
# ===========================================================================
print("\n1. CSRFProtect is enabled and actually refusing")

check("csrf" in appmod.app.extensions,
      "CSRFProtect is registered on the app (app.extensions['csrf'])")
check(appmod.app.config.get("WTF_CSRF_ENABLED", True) is not False,
      "WTF_CSRF_ENABLED is not disabled in config")
check(appmod.app.config.get("WTF_CSRF_CHECK_DEFAULT", True) is not False,
      "WTF_CSRF_CHECK_DEFAULT is not disabled in config")

with appmod.app.app_context():
    appmod.db.create_all()
    client = appmod.app.test_client()

    _fresh_g()
    resp = client.post("/author/register",
                       data={"name": "CSRF Probe", "email": "csrf.probe@example.test",
                             "password": "csrf-probe-pass", "confirm_password": "csrf-probe-pass",
                             "_gotcha": ""})
    check(resp.status_code == 302
          and resp.headers.get("Location", "") == "/author/register",
          f"a token-less POST to /author/register is refused with the retry redirect "
          f"(got {resp.status_code} -> {resp.headers.get('Location', '')!r})")
    check(appmod.Author.query.filter_by(email="csrf.probe@example.test").first() is None,
          "...and it created no account")

    # =======================================================================
    # 2. The exemption set is EXACTLY the allowlist
    # =======================================================================
    print("\n2. The exemption set is exactly the documented endpoints")

    csrf_ext = appmod.csrf
    if not hasattr(csrf_ext, "_exempt_views") or not hasattr(csrf_ext, "_exempt_blueprints"):
        check(False, "flask_wtf still exposes _exempt_views/_exempt_blueprints "
                     "(its exemption bookkeeping changed -- update this check)")
        finish()

    actual_exempt = set()
    for endpoint, view in appmod.app.view_functions.items():
        # Mirror flask_wtf CSRFProtect._is_exempt() exactly.
        bp_name = endpoint.rpartition(".")[0]
        if bp_name and appmod.app.blueprints.get(bp_name) in csrf_ext._exempt_blueprints:
            actual_exempt.add(endpoint)
            continue
        dest = f"{view.__module__}.{view.__name__}"
        if dest in csrf_ext._exempt_views:
            actual_exempt.add(endpoint)

    expected = set(EXEMPT_ALLOWLIST)
    unlisted = sorted(actual_exempt - expected)
    stale = sorted(expected - actual_exempt)

    for ep in unlisted:
        check(False,
              f"unlisted CSRF exemption: {ep} -- every exemption is a hole in the "
              f"app-wide protection and must be enumerated here with the mechanism "
              f"that authenticates the caller instead")
    for ep in stale:
        check(False,
              f"allowlisted exemption no longer exists: {ep} -- the allowlist is "
              f"stale; delete the entry so it keeps documenting reality")
    if not unlisted and not stale:
        check(True,
              "exempt endpoints == allowlist: " + ", ".join(sorted(expected)))
    for ep in sorted(expected & actual_exempt):
        print(f"         {ep}: {EXEMPT_ALLOWLIST[ep]}")

# ===========================================================================
# 3. Every POST form in templates/ renders a token
# ===========================================================================
print("\n3. Every <form method=post> in templates/ carries the token")

TEMPLATES = REPO_ROOT / "templates"
FORM_TAG_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
METHOD_POST_RE = re.compile(r"""method\s*=\s*["']?\s*post\s*["']?""", re.IGNORECASE)
TOKEN_RE = re.compile(
    r"""name\s*=\s*["']csrf_token["']|csrf_token\s*\(""", re.IGNORECASE)

post_forms = 0
for tpl in sorted(TEMPLATES.rglob("*.html")):
    text = tpl.read_text(encoding="utf-8")
    rel = tpl.relative_to(REPO_ROOT)
    for m in FORM_TAG_RE.finditer(text):
        if not METHOD_POST_RE.search(m.group(0)):
            continue
        post_forms += 1
        end = text.find("</form", m.end())
        body = text[m.end(): end if end != -1 else len(text)]
        line = text.count("\n", 0, m.start()) + 1
        ok = bool(TOKEN_RE.search(body))
        check(ok, f"{rel}: <form method=post> at line {line} "
                  + ("carries the token"
                     if ok else
                     "has no csrf_token hidden input or csrf_token() call"))

check(post_forms >= 30,
      f"the form scan actually found the app's POST forms ({post_forms} found; "
      f"a collapse here means the regex went blind, not that forms disappeared)")

# ===========================================================================
# 4. The meta tag exists and every JS POST sends the header
# ===========================================================================
print("\n4. base.html exposes the token and every fetch()/XHR POST sends it")

base_html = (TEMPLATES / "base.html").read_text(encoding="utf-8")
check('<meta name="csrf-token"' in base_html and "csrf_token()" in base_html,
      "base.html carries the <meta name=\"csrf-token\"> tag filled by csrf_token()")
check("window.CSRF_TOKEN" in base_html,
      "base.html publishes the token as window.CSRF_TOKEN for fetch() callers")

# A file "sends a POST from JS" when it contains a fetch()/XHR with an
# explicit POST method. Attribute order and quoting vary; keep both patterns
# wide and case-insensitive.
JS_POST_RES = [
    re.compile(r"""method\s*:\s*["']post["']""", re.IGNORECASE),   # fetch({method:'POST'})
    re.compile(r"""\.open\s*\(\s*["']post["']""", re.IGNORECASE),  # xhr.open('POST', ...)
]
TOKEN_REF_RE = re.compile(r"X-CSRFToken|window\.CSRF_TOKEN|csrf-token", re.IGNORECASE)

js_posters = 0
scan_files = sorted(TEMPLATES.rglob("*.html")) + sorted((REPO_ROOT / "static").rglob("*.js"))
for path in scan_files:
    text = path.read_text(encoding="utf-8")
    rel = str(path.relative_to(REPO_ROOT))
    if not any(p.search(text) for p in JS_POST_RES):
        continue
    js_posters += 1
    if rel in JS_EXEMPT_FILES:
        print(f"  ok   {rel} posts without the header, allowlisted: {JS_EXEMPT_FILES[rel]}")
        total += 1
        continue
    ok = bool(TOKEN_REF_RE.search(text))
    check(ok, f"{rel} "
              + ("sends its fetch()/XHR POSTs with the CSRF token"
                 if ok else
                 "sends a fetch()/XHR POST but never references the CSRF token "
                 "(X-CSRFToken header / window.CSRF_TOKEN / the csrf-token meta)"))

check(js_posters >= 8,
      f"the JS scan actually found the app's fetch() POST callers ({js_posters} "
      f"found; a collapse here means the regex went blind)")

stale_js = sorted(set(JS_EXEMPT_FILES) - {str(p.relative_to(REPO_ROOT)) for p in scan_files})
check(not stale_js,
      f"every JS-allowlist entry still exists on disk (stale: {stale_js})")

finish()
