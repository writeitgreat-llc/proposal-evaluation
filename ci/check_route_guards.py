#!/usr/bin/env python3
"""
check_route_guards.py -- auth-coverage gate for proposal-evaluation.

Any Flask route whose URL rule contains a path parameter (``<...>``) can be
addressed by guessing or enumerating that parameter. This script finds every
such route in app.py and sorts the handlers into three buckets:

  GUARDED     an auth decorator (login_required / admin_required /
              team_required / author_login_required / publisher_login_required
              / any *_required), or a real access check near the top of the
              handler: current_user.is_authenticated, abort(401|403),
              an ownership comparison, or a call to a resolver helper that
              itself aborts (e.g. _resolve_social_strategy).

  CAPABILITY  no login required, but the row is fetched by an unguessable
              value taken from the URL -- a password-reset token, a share
              token, or a random submission_id. These are legitimate but they
              are only as safe as the token's entropy, so each one must be
              listed in ci/route_guard_allowlist.txt with a reason. An
              unlisted capability route is reported as a WARNING.

  UNGUARDED   nothing. Build fails.

Note the deliberate asymmetry: ``.get_or_404()`` on an ``<int:...>`` parameter
is NOT a guard. Sequential integer ids are trivially enumerable, so "the row
exists" tells an attacker nothing about whether they may see it. That is
exactly the IDOR that was live on ``/social-strategy/result/<id>``: the row was
loaded by sequential id and rendered with no ownership check. This script fails
on that shape.

Usage:
    python ci/check_route_guards.py
    python ci/check_route_guards.py --app app.py --allowlist ci/route_guard_allowlist.txt
    python ci/check_route_guards.py --show-guarded      # full inventory
    python ci/check_route_guards.py --strict-capability # unlisted capability routes fail too
    python ci/check_route_guards.py --warn-only         # never exit non-zero

Exit codes: 0 = clean, 1 = unguarded route found, 2 = usage/parse error.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

AUTH_DECORATORS = {
    "login_required",
    "admin_required",
    "team_required",
    "author_login_required",
    "publisher_login_required",
}

# Anything ending in _required also counts, so a new role decorator works
# without editing this file.
AUTH_DECORATOR_SUFFIX = "_required"

# Capability patterns: the handler authenticates the *request* via an
# unguessable value from the URL rather than via a session.
CAPABILITY_PATTERNS = [
    (r"verify_reset_token\s*\(", "password-reset token verification"),
    (r"[A-Za-z_]*reset_token\s*=", "lookup keyed on a password-reset token"),
    (r"share_token\s*=", "lookup keyed on a share token"),
    (r"access_token\s*=", "lookup keyed on an access token"),
]

# Real access checks.
GUARD_PATTERNS = [
    (r"current_user\.is_authenticated", "current_user.is_authenticated check"),
    (r"\babort\(\s*(401|403)", "abort(401/403)"),
    (r"\bcheck_password_hash\s*\(", "password check"),
    (r"session\.get\(\s*['\"](admin|team|author|publisher)", "session role check"),
    (r"\.author_id\s*!=\s*current_user", "ownership comparison"),
    (r"current_user\.id\s*!=", "ownership comparison"),
]

# 404-on-missing lookups. Only a guard when the key is NOT an integer.
OR_404_RE = re.compile(r"\.(first_or_404|get_or_404|one_or_404)\s*\(")

# Never treat these as resolver helpers.
NON_GUARD_CALLS = {"print", "len", "str", "int", "getattr", "json", "render_template"}

DEFAULT_GUARD_WINDOW = 15

GUARDED, CAPABILITY, UNGUARDED = "GUARDED", "CAPABILITY", "UNGUARDED"


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------

def _decorator_name(node):
    """Return the trailing name of a decorator / call expression."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _route_rules(func):
    """Every URL rule declared by @app.route / @bp.route on this function."""
    rules = []
    for dec in func.decorator_list:
        if not isinstance(dec, ast.Call) or _decorator_name(dec) != "route":
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant) \
                and isinstance(dec.args[0].value, str):
            rules.append(dec.args[0].value)
    return rules


def _called_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            name = _decorator_name(sub)
            if name:
                names.add(name)
    return names


def find_guard_helpers(tree):
    """
    Module-level functions that can themselves deny access -- their body calls
    abort() or a *_or_404 lookup. A handler that delegates to one of these is
    treated as guarded. In app.py this is how _resolve_social_strategy (the
    IDOR fix) is recognised.
    """
    helpers = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.FunctionDef) and _route_rules(node):
            continue  # route handlers are not helpers
        calls = _called_names(node)
        if "abort" in calls or any(c.endswith("_or_404") for c in calls):
            helpers.add(node.name)
    return helpers


def rule_params(rule):
    """[('int','md_id'), ('','submission_id')] for the <...> parts of a rule."""
    out = []
    for raw in re.findall(r"<([^>]+)>", rule):
        if ":" in raw:
            conv, _, name = raw.partition(":")
        else:
            conv, name = "", raw
        out.append((conv.strip(), name.strip()))
    return out


def has_enumerable_param(rule):
    """True if every parameter is an integer -- i.e. trivially enumerable."""
    params = rule_params(rule)
    return bool(params) and all(conv in ("int", "float") for conv, _ in params)


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------

def load_allowlist(path):
    """Parse `rule  # reason` lines. Blank lines and full-line comments ignored."""
    allow = {}
    if not path.is_file():
        return allow
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rule, _, reason = line.partition("#")
        rule = rule.strip()
        if rule:
            allow[rule] = reason.strip() or "(no reason given)"
    return allow


# --------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------

class Route:
    def __init__(self, rule, func, lineno):
        self.rule = rule
        self.func = func
        self.lineno = lineno
        self.verdict = UNGUARDED
        self.reason = "no auth decorator and no access check"

    def __str__(self):
        return f"{self.rule}  ->  {self.func}()  (line {self.lineno})"


def classify(rule, func_node, head, guard_helpers):
    """Return (verdict, reason) for one rule."""
    # 1. auth decorator
    dec_names = {_decorator_name(d) for d in func_node.decorator_list}
    auth = sorted(n for n in dec_names
                  if n in AUTH_DECORATORS or n.endswith(AUTH_DECORATOR_SUFFIX))
    if auth:
        return GUARDED, "@" + auth[0]

    # 2. delegation to a resolver helper that aborts
    head_calls = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", head))
    hit = sorted((head_calls & guard_helpers) - NON_GUARD_CALLS)
    if hit:
        return GUARDED, "resolver helper %s()" % hit[0]

    # 3. capability URL (checked before the generic patterns, because
    #    reset-password handlers mention current_user only to redirect away
    #    an already-logged-in visitor -- that is not an access check)
    for pattern, label in CAPABILITY_PATTERNS:
        if re.search(pattern, head):
            return CAPABILITY, label

    # 4. real inline access check
    for pattern, label in GUARD_PATTERNS:
        if re.search(pattern, head):
            return GUARDED, label

    # 5. *_or_404 lookup: a capability URL only when the key is not an integer
    if OR_404_RE.search(head):
        if has_enumerable_param(rule):
            return UNGUARDED, ("*_or_404() on an enumerable integer id is not an "
                               "access check")
        return CAPABILITY, "*_or_404() keyed on the opaque URL parameter"

    return UNGUARDED, "no auth decorator and no access check"


def analyse(app_path, window):
    source = app_path.read_text(encoding="utf-8")
    lines = source.splitlines()
    try:
        tree = ast.parse(source, filename=str(app_path))
    except SyntaxError as exc:
        print("ERROR: cannot parse %s: %s" % (app_path, exc), file=sys.stderr)
        raise SystemExit(2)

    guard_helpers = find_guard_helpers(tree)
    routes = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        param_rules = [r for r in _route_rules(node) if "<" in r]
        if not param_rules:
            continue
        if not node.body:
            head = ""
        else:
            start = node.body[0].lineno - 1
            # Clamp to this function's own body. Without the end_lineno clamp a
            # short handler's window runs on into the NEXT function and picks up
            # its guard -- which silently hid a reintroduced IDOR in testing.
            body_end = getattr(node, "end_lineno", None) or len(lines)
            end = min(start + window, body_end, len(lines))
            head = "\n".join(lines[start:end])
        for rule in param_rules:
            r = Route(rule, node.name, node.lineno)
            r.verdict, r.reason = classify(rule, node, head, guard_helpers)
            routes.append(r)

    routes.sort(key=lambda r: (r.lineno, r.rule))
    return routes


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default="app.py")
    ap.add_argument("--allowlist", default="ci/route_guard_allowlist.txt")
    ap.add_argument("--window", type=int, default=DEFAULT_GUARD_WINDOW,
                    help="lines of the handler body scanned for an inline check")
    ap.add_argument("--warn-only", action="store_true")
    ap.add_argument("--strict-capability", action="store_true",
                    help="fail on capability routes that are not allowlisted")
    ap.add_argument("--show-guarded", action="store_true")
    args = ap.parse_args()

    app_path = Path(args.app)
    if not app_path.is_file():
        print("ERROR: %s not found" % app_path, file=sys.stderr)
        return 2

    routes = analyse(app_path, args.window)
    allow = load_allowlist(Path(args.allowlist))

    guarded = [r for r in routes if r.verdict == GUARDED]
    capability = [r for r in routes if r.verdict == CAPABILITY]
    unguarded = [r for r in routes if r.verdict == UNGUARDED]

    cap_listed = [r for r in capability if r.rule in allow]
    cap_unlisted = [r for r in capability if r.rule not in allow]
    # An allowlist entry can also cover a route this script calls UNGUARDED,
    # but that must be a conscious decision, so it is still printed loudly.
    unguarded_listed = [r for r in unguarded if r.rule in allow]
    unguarded_real = [r for r in unguarded if r.rule not in allow]

    print("=== route guard audit: %s ===" % app_path)
    print("parameterised routes        : %d" % len(routes))
    print("  guarded                   : %d" % len(guarded))
    print("  capability (allowlisted)  : %d" % len(cap_listed))
    print("  capability (NOT listed)   : %d" % len(cap_unlisted))
    print("  unguarded (allowlisted)   : %d" % len(unguarded_listed))
    print("  unguarded (NOT listed)    : %d" % len(unguarded_real))
    print()

    if args.show_guarded and guarded:
        print("--- guarded ---")
        for r in guarded:
            print("  OK    %s  [%s]" % (r, r.reason))
        print()

    if cap_listed:
        print("--- capability URLs, reviewed and allowlisted ---")
        for r in cap_listed:
            print("  ALLOW %s\n          detected: %s\n          reason:   %s"
                  % (r, r.reason, allow[r.rule]))
        print()

    if cap_unlisted:
        print("--- WARNING: capability URLs not in the allowlist ---")
        for r in cap_unlisted:
            print("  WARN  %s\n          %s" % (r, r.reason))
        print()
        print("  These serve data to anyone holding the URL. Confirm the token is")
        print("  random and long (>=128 bits), then add the rule to %s" % args.allowlist)
        print()

    if unguarded_listed:
        print("--- unguarded but allowlisted (review these) ---")
        for r in unguarded_listed:
            print("  ALLOW %s\n          %s\n          reason: %s"
                  % (r, r.reason, allow[r.rule]))
        print()

    failed = False

    if unguarded_real:
        print("--- UNGUARDED PARAMETERISED ROUTES ---")
        for r in unguarded_real:
            print("  FAIL  %s\n          %s" % (r, r.reason))
        print()
        print("Each takes an id straight from the URL with no auth decorator and no")
        print("ownership check in the first %d lines of the handler. Fix by adding" % args.window)
        print("the right *_required decorator, an explicit ownership check, or -- if")
        print("the route is deliberately public -- add it to %s with a reason."
              % args.allowlist)
        failed = True

    if cap_unlisted and args.strict_capability:
        failed = True

    if failed:
        if args.warn_only:
            print("\n(--warn-only: not failing the build)")
            return 0
        return 1

    print("OK: every parameterised route is guarded or explicitly accounted for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
