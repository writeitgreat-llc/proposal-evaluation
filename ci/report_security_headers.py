#!/usr/bin/env python3
"""
report_security_headers.py -- REPORT ONLY. Never fails a build, never edits code.

Statically checks whether the app sets the response headers and cookie flags
that a public Flask site should be sending, and prints a copy-pasteable
snippet for whatever is missing. It looks for evidence in the source rather
than making HTTP requests, so it needs no running app, no database, and no
OPENAI_API_KEY.

It is wired into CI as an informational step so the gap stays visible in every
job summary instead of living in someone's notes. Turning any of this on is a
deliberate change to the app, made by a human, in its own PR -- a CSP added by
a robot is a CSP that breaks the site at 6pm on a Friday.

Usage:
    python ci/report_security_headers.py --paths app.py
    python ci/report_security_headers.py --paths app config.py --label writeitgreat-website
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# (key, human name, regexes that count as evidence, why it matters)
CHECKS = [
    ("HSTS", "Strict-Transport-Security",
     [r"Strict-Transport-Security"],
     "Stops the first request of a session going out over plaintext HTTP. "
     "Heroku already terminates TLS; this tells the browser never to try http:// again."),
    ("CSP", "Content-Security-Policy",
     [r"Content-Security-Policy"],
     "The main defence against stored XSS. Start in Report-Only mode, watch the "
     "violation reports for a week, then enforce."),
    ("NOSNIFF", "X-Content-Type-Options",
     [r"X-Content-Type-Options"],
     "Stops the browser MIME-sniffing an uploaded file into something executable. "
     "One line, no compatibility risk -- do this one first."),
    ("FRAME", "X-Frame-Options / frame-ancestors",
     [r"X-Frame-Options", r"frame-ancestors"],
     "Blocks clickjacking of the admin panel. No compatibility risk unless "
     "something legitimately embeds the site in an iframe."),
    ("REFERRER", "Referrer-Policy",
     [r"Referrer-Policy"],
     "Keeps capability URLs (results links, reset links) out of the Referer header "
     "sent to third-party sites the user clicks through to."),
    ("PERMISSIONS", "Permissions-Policy",
     [r"Permissions-Policy"],
     "Turns off camera/microphone/geolocation for the whole origin."),
    ("COOKIE_SECURE", "SESSION_COOKIE_SECURE",
     [r"SESSION_COOKIE_SECURE"],
     "Session cookie must never be sent over plaintext."),
    ("COOKIE_HTTPONLY", "SESSION_COOKIE_HTTPONLY",
     [r"SESSION_COOKIE_HTTPONLY"],
     "Stops JavaScript reading the session cookie. Defaults to True in Flask, but "
     "set it explicitly so nobody turns it off by accident."),
    ("COOKIE_SAMESITE", "SESSION_COOKIE_SAMESITE",
     [r"SESSION_COOKIE_SAMESITE"],
     "'Lax' blocks the cross-site request forgery cases that CSRF tokens miss."),
    ("CSRF", "CSRF protection",
     [r"CSRFProtect", r"WTF_CSRF_ENABLED", r"csrf\.init_app", r"validate_csrf"],
     "Every state-changing POST needs a token. Flask-WTF gives this for free; a "
     "hand-rolled form posting to a bare @app.route does not."),
    ("PROXYFIX", "ProxyFix",
     [r"ProxyFix"],
     "Behind the Heroku router, without ProxyFix the app sees the router's IP and "
     "http:// scheme -- which breaks rate limiting by IP and secure-cookie logic."),
    ("MAXLEN", "MAX_CONTENT_LENGTH",
     [r"MAX_CONTENT_LENGTH"],
     "Caps upload size. Without it a single large POST can exhaust a dyno."),
]

SNIPPET = '''
# ---------------------------------------------------------------------------
# Security headers. Add ONE at a time, deploy, check the site still works.
# Order of least risk: X-Content-Type-Options, Referrer-Policy,
# X-Frame-Options, Permissions-Policy, HSTS, then CSP last (Report-Only first).
# ---------------------------------------------------------------------------
@app.after_request
def set_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")

    # Only send HSTS from production, and only over https, or you will pin
    # localhost to https for six months.
    if request.is_secure and not app.debug:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    # Start with Report-Only. Read the reports, tighten, THEN rename the header
    # to Content-Security-Policy.
    response.headers.setdefault("Content-Security-Policy-Report-Only", "; ".join([
        "default-src 'self'",
        "img-src 'self' data: https://res.cloudinary.com",
        "style-src 'self' 'unsafe-inline'",       # inline <style> blocks in the templates
        "script-src 'self' 'unsafe-inline'",      # inline <script> in the intake engine
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ]))
    return response
'''.strip("\n")


def emit_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass


def gather(paths):
    text = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files = sorted(p.rglob("*.py"))
        elif p.is_file():
            files = [p]
        else:
            continue
        for f in files:
            try:
                text.append(f.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, OSError):
                continue
    return "\n".join(text)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--paths", nargs="+", required=True)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    source = gather(args.paths)
    if not source:
        print("no Python source found in: %s" % ", ".join(args.paths))
        return 0

    present, missing = [], []
    for key, name, patterns, why in CHECKS:
        if any(re.search(p, source) for p in patterns):
            present.append((name, why))
        else:
            missing.append((name, why))

    print("=== security headers report%s ===" % (" -- " + args.label if args.label else ""))
    print("(informational only -- this step never fails the build)")
    print()
    print("set    : %d / %d" % (len(present), len(CHECKS)))
    print()

    if present:
        print("--- already set ---")
        for name, _ in present:
            print("  OK      %s" % name)
        print()

    if missing:
        print("--- not set ---")
        for name, why in missing:
            print("  MISSING %s" % name)
            print("          %s" % why)
        print()
        print("--- suggested snippet (do NOT paste it all at once) ---")
        print(SNIPPET)
        print()

    summary = ["### Security headers%s" % (" -- " + args.label if args.label else ""), ""]
    summary.append("%d of %d set. Missing: %s"
                   % (len(present), len(CHECKS),
                      ", ".join(n for n, _ in missing) or "none"))
    emit_summary(summary)

    print("Report only. Nothing was changed and nothing failed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
