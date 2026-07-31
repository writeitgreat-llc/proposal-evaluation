#!/usr/bin/env python3
"""
check_ai_timeouts.py -- every OpenAI call in this app must have a time budget.

The app runs on ONE gunicorn process with four threads (see Procfile). An
OpenAI call with no timeout is therefore not a slow feature, it is a thread
taken out of circulation -- and the pinned SDK's idea of "no timeout" is
generous:

    openai==1.68.2  DEFAULT_TIMEOUT     = Timeout(connect=5, read=600,
                                                  write=600, pool=600)
                    DEFAULT_MAX_RETRIES = 2

and _base_client retries httpx.TimeoutException, so one bare call can hold a
thread for 600s x 3 = 30 minutes. Four of those is the whole site down, on the
sign-up page every campaign link points at.

app.py builds its clients through build_openai_client(), which always passes a
timeout. This script exists because there are two ways to get round that and
one of them is invisible:

  1. openai.OpenAI(...) called somewhere else, producing a second client that
     silently carries the SDK defaults.

  2. THE ONE THAT ACTUALLY HAPPENED -- the module-level call form:

         openai.chat.completions.create(...)      # bypasses the app's client
         client.chat.completions.create(...)      # uses it

     Those two lines look the same in review. openai 1.x resolves the first
     through its own lazily-built _ModuleClient, which inherits NOTHING from a
     client the application configured. _detect_ai_content used that form, so
     it was the one call in twelve that a timeout on the shared client did not
     reach -- and it runs on every submitted proposal.

It also guards the arithmetic that depends on those budgets. EVAL_STUCK_AFTER
is DERIVED from AI_BACKGROUND_TIMEOUT_SECONDS, AI_BACKGROUND_MAX_RETRIES and
_EVAL_SLOT_WAIT rather than written as a literal, because a stale literal fails
in the silent direction: a sweep threshold shorter than a legitimate evaluation
re-dispatches work that was merely slow, and the author gets two contradictory
reports while the company pays twice. Replacing the expression with a number is
a regression this catches.

Checks, in order:

  A  no module-level openai.<resource>.<...>(...) call anywhere in the repo
  B  openai.OpenAI(...) / openai.AsyncOpenAI(...) only inside
     build_openai_client()
  C  every OpenAI resource call is made on an approved client object
  D  the DEFAULT request-path budget still fits inside Heroku's 30s router
     deadline, retries included
  E  EVAL_STUCK_AFTER is still computed from the timeout constants, not a
     literal
  F  and that computation is still SOUND -- the window genuinely clears the
     worst case a scoring can take

Two things D does not prove, stated so nobody reads more into a green tick than
is there. It reads the DEFAULT baked into os.environ.get(), so a dyno running
with AI_REQUEST_TIMEOUT_SECONDS set to something reckless still passes -- CI
cannot see Heroku config. And it deliberately says nothing about
AI_SLOW_REQUEST_TIMEOUT_SECONDS, which is a request-path budget that exceeds the
router deadline ON PURPOSE; the reasoning for that one is at its definition in
app.py, and it is the exception this check is not trying to police.

Usage:
    python ci/check_ai_timeouts.py
    python ci/check_ai_timeouts.py --app app.py
    python ci/check_ai_timeouts.py --list        # inventory of every call site

Exit codes: 0 = clean, 1 = an unbudgeted or bypassing call, 2 = usage/parse error.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Client objects that are known to carry a timeout, because app.py builds them
# through build_openai_client(). Adding a name here is claiming the same.
APPROVED_CLIENTS = {"client", "background_client"}

# The factory itself -- the only place allowed to construct a client.
FACTORY_NAME = "build_openai_client"

# Resource namespaces the OpenAI SDK exposes. Two lists, because the two checks
# that use them can afford different amounts of ambiguity.
#
# After `openai.`, any of these is unmistakably an API call, so the module-level
# check can use the full surface.
MODULE_LEVEL_RESOURCES = {
    "chat",
    "completions",
    "responses",
    "embeddings",
    "moderations",
    "images",
    "audio",
    "files",
    "batches",
    "fine_tuning",
    "beta",
}

# The receiver-based check sees `<anything>.<resource>.<method>()`, and some of
# those names are ordinary English. `files` is the one that bit: Flask's
# `request.files.get(...)` appears four times in app.py and matched, which would
# have failed the build on code that has never spoken to OpenAI. A check that
# cries wolf gets deleted, so the ambiguous names are dropped here and kept
# above -- `openai.files.create(...)` is still caught, `request.files.get(...)`
# is not. What this trades away is a bare `client.files.create()` on an
# unapproved receiver; the shapes that actually carry the risk here -- chat
# completions, and every module-level form -- stay covered.
OPENAI_RESOURCES = MODULE_LEVEL_RESOURCES - {"files", "batches"}

# Heroku's router gives up here. A request-path budget at or above this can
# never produce an answer the browser will see.
ROUTER_DEADLINE_SECONDS = 30.0

# Names the derived EVAL_STUCK_AFTER expression must still mention.
STUCK_AFTER_MUST_REFERENCE = {
    "AI_BACKGROUND_TIMEOUT_SECONDS",
    "AI_BACKGROUND_MAX_RETRIES",
    "_EVAL_SLOT_WAIT",
}


# --------------------------------------------------------------------------
# AST helpers
# --------------------------------------------------------------------------

def dotted_name(node: ast.AST) -> str:
    """Render a.b.c from an Attribute/Name chain. '' if it is not one."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def root_of(dotted: str) -> str:
    return dotted.split(".", 1)[0] if dotted else ""


def enclosing_functions(tree: ast.AST) -> dict[int, str]:
    """Map every line number inside a function body to that function's name."""
    spans: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            for line in range(node.lineno, end + 1):
                # Inner functions win; walk order makes the last write the
                # deepest only by luck, so prefer the tighter span explicitly.
                prev = spans.get(line)
                if prev is None or end - node.lineno < spans.get(("span", line), 10**9):
                    spans[line] = node.name
                    spans[("span", line)] = end - node.lineno
    return {k: v for k, v in spans.items() if isinstance(k, int)}


def literal_default(call: ast.Call) -> str | None:
    """Second positional arg of os.environ.get('X', 'default') as a string."""
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return str(call.args[1].value)
    return None


def find_env_default(tree: ast.AST, target: str) -> str | None:
    """Default literal for `TARGET = <type>(os.environ.get('..', 'default'))`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if target not in names:
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Call) and dotted_name(sub.func) == "os.environ.get":
                got = literal_default(sub)
                if got is not None:
                    return got
    return None


def find_assignment(tree: ast.AST, target: str) -> ast.Assign | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == target for t in node.targets):
                return node
    return None


class UnevaluableError(Exception):
    """The expression is not arithmetic this checker is willing to evaluate."""


def safe_eval(node: ast.AST, ns: dict[str, float]) -> float:
    """Evaluate a small arithmetic expression to seconds.

    Deliberately NOT eval(): this walks a whitelist of node types, so the check
    cannot be turned into an arbitrary-code path by editing app.py, and an
    expression shape it does not understand fails loudly instead of silently
    producing a number. timedelta(...) collapses to seconds so both sides of
    the comparison are plain floats.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id not in ns:
            raise UnevaluableError(f"unknown name {node.id!r}")
        return ns[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        val = safe_eval(node.operand, ns)
        return val if isinstance(node.op, ast.UAdd) else -val
    if isinstance(node, ast.BinOp):
        left, right = safe_eval(node.left, ns), safe_eval(node.right, ns)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        raise UnevaluableError(f"unsupported operator {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        # timedelta(...) -> seconds. Any other call is out of scope.
        if dotted_name(node.func).split(".")[-1] != "timedelta":
            raise UnevaluableError(f"unsupported call {dotted_name(node.func)!r}")
        units = {"weeks": 604800.0, "days": 86400.0, "hours": 3600.0,
                 "minutes": 60.0, "seconds": 1.0, "milliseconds": 0.001,
                 "microseconds": 0.000001}
        total = 0.0
        for kw in node.keywords:
            if kw.arg not in units:
                raise UnevaluableError(f"unsupported timedelta unit {kw.arg!r}")
            total += safe_eval(kw.value, ns) * units[kw.arg]
        if node.args:                        # timedelta(days) positionally
            total += safe_eval(node.args[0], ns) * 86400.0
        return total
    raise UnevaluableError(f"unsupported expression node {type(node).__name__}")


def constant_seconds(tree: ast.AST, target: str, ns: dict[str, float]) -> float | None:
    """Value of a module-level numeric constant, env-configurable or literal."""
    env_default = find_env_default(tree, target)
    if env_default is not None:
        try:
            return float(env_default)
        except ValueError:
            return None
    assign = find_assignment(tree, target)
    if assign is None:
        return None
    try:
        return safe_eval(assign.value, ns)
    except UnevaluableError:
        return None


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def scan_calls(tree: ast.AST, path: Path, funcs: dict[int, str]):
    """Return (module_bypasses, constructions, resource_calls)."""
    module_bypasses, constructions, resource_calls = [], [], []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        if not name:
            continue
        parts = name.split(".")

        # openai.OpenAI(...) / openai.AsyncOpenAI(...)
        if len(parts) == 2 and parts[0] == "openai" and parts[1] in ("OpenAI", "AsyncOpenAI"):
            constructions.append((node.lineno, name, funcs.get(node.lineno)))
            continue

        # openai.<resource>....(...) -- the module-level bypass
        if parts[0] == "openai" and len(parts) >= 3 and parts[1] in MODULE_LEVEL_RESOURCES:
            module_bypasses.append((node.lineno, name, funcs.get(node.lineno)))
            continue

        # <receiver>.<resource>[....].<method>(...) -- a call on some client
        # object. Test every segment between the receiver and the method, not a
        # fixed position: `c.chat.completions.create` puts the resource at
        # parts[1] of four, `c.embeddings.create` at parts[1] of three, and
        # `c.beta.threads.runs.create` at parts[1] of five. An earlier version
        # of this looked at parts[-3], which lines up only for the four-segment
        # chat shape -- so `c.embeddings.create` and `c.responses.create` were
        # invisible to it.
        if len(parts) >= 3 and parts[0] != "openai":
            if any(p in OPENAI_RESOURCES for p in parts[1:-1]):
                resource_calls.append((node.lineno, name, root_of(name), funcs.get(node.lineno)))

    return module_bypasses, constructions, resource_calls


def imports_openai(tree: ast.AST) -> bool:
    """Does this module import openai at all?

    Scopes the call checks to files that could actually reach the API. Without
    it, a resource name as ordinary as `files` or `images` would fail the build
    on unrelated code -- and a check that cries wolf is a check somebody turns
    off.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "openai" or a.name.startswith("openai.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == "openai" or (node.module or "").startswith("openai."):
                return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default="app.py", help="application module to scan")
    ap.add_argument("--root", default=".", help="repo root to sweep for stray call forms")
    ap.add_argument("--list", action="store_true", help="print every call site found")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    app_path = (root / args.app).resolve()
    if not app_path.exists():
        print(f"check_ai_timeouts: cannot find {app_path}", file=sys.stderr)
        return 2

    failures: list[str] = []
    notes: list[str] = []

    # Every tracked .py file, not just app.py: a bypass added to a helper module
    # is exactly as unbudgeted, and analytics_collect.py already imports plenty.
    py_files = sorted(
        p for p in root.rglob("*.py")
        if not any(part in {".venv", "venv", "__pycache__", "node_modules", ".git"}
                   for part in p.parts)
    )

    all_resource_calls = []
    for py in py_files:
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:
            print(f"check_ai_timeouts: cannot parse {py}: {exc}", file=sys.stderr)
            return 2

        rel = py.relative_to(root)
        if not imports_openai(tree):
            continue

        funcs = enclosing_functions(tree)
        bypasses, constructions, resource_calls = scan_calls(tree, py, funcs)

        # ── A: the module-level form ────────────────────────────────────────
        for lineno, name, fn in bypasses:
            failures.append(
                f"{rel}:{lineno}: `{name}(...)` uses the module-level OpenAI form"
                f"{f' in {fn}()' if fn else ''}.\n"
                f"    openai 1.x serves this from its own _ModuleClient, which does NOT\n"
                f"    inherit the timeout {FACTORY_NAME}() sets -- it keeps the SDK default\n"
                f"    of 600s x 3 attempts. Call it on an approved client instead:\n"
                f"      {' or '.join(sorted(APPROVED_CLIENTS))}.{name.split('.', 1)[1]}(...)"
            )

        # ── B: client construction only in the factory ──────────────────────
        for lineno, name, fn in constructions:
            if fn != FACTORY_NAME:
                failures.append(
                    f"{rel}:{lineno}: `{name}(...)` constructs an OpenAI client outside"
                    f" {FACTORY_NAME}(){f' (in {fn}())' if fn else ''}.\n"
                    f"    A client built anywhere else carries the SDK's 600s x 3 default\n"
                    f"    unless every caller remembers to pass timeout= and max_retries=.\n"
                    f"    Build it through {FACTORY_NAME}() so it cannot be forgotten."
                )

        # ── C: resource calls must be on an approved client ─────────────────
        for lineno, name, receiver, fn in resource_calls:
            all_resource_calls.append((rel, lineno, name, receiver, fn))
            if receiver not in APPROVED_CLIENTS:
                # `self.client....` and locals shadowing are the plausible
                # false positives; both still deserve a look, so report rather
                # than guess.
                failures.append(
                    f"{rel}:{lineno}: `{name}(...)` is an OpenAI call on `{receiver}`,"
                    f" which is not a known-budgeted client"
                    f"{f' (in {fn}())' if fn else ''}.\n"
                    f"    Approved clients: {', '.join(sorted(APPROVED_CLIENTS))}.\n"
                    f"    If `{receiver}` really is built through {FACTORY_NAME}(), add it\n"
                    f"    to APPROVED_CLIENTS in this file -- deliberately, not reflexively."
                )

    # ── D + E: the constants, read from app.py ──────────────────────────────
    app_tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))

    req_timeout = find_env_default(app_tree, "AI_REQUEST_TIMEOUT_SECONDS")
    req_retries = find_env_default(app_tree, "AI_REQUEST_MAX_RETRIES")
    if req_timeout is None or req_retries is None:
        failures.append(
            "app.py: AI_REQUEST_TIMEOUT_SECONDS / AI_REQUEST_MAX_RETRIES are no longer\n"
            "    defined as os.environ.get(name, 'default') -- this check can no longer\n"
            "    read the request-path budget, so it can no longer prove it fits inside\n"
            "    Heroku's 30s router deadline."
        )
    else:
        budget = float(req_timeout) * (int(req_retries) + 1)
        if budget >= ROUTER_DEADLINE_SECONDS:
            failures.append(
                f"app.py: the request-path OpenAI budget is {float(req_timeout):g}s x"
                f" {int(req_retries) + 1} attempts = {budget:g}s, which is not under"
                f" Heroku's {ROUTER_DEADLINE_SECONDS:g}s router deadline.\n"
                f"    Past that the router has already sent the browser an H12 and the\n"
                f"    dyno is holding one of four threads for an answer nobody will read.\n"
                f"    Remember retries multiply: wall-clock is timeout x (max_retries + 1)."
            )
        else:
            notes.append(
                f"request-path budget {float(req_timeout):g}s x {int(req_retries) + 1}"
                f" = {budget:g}s, inside the {ROUTER_DEADLINE_SECONDS:g}s router deadline"
            )

    stuck = find_assignment(app_tree, "EVAL_STUCK_AFTER")
    if stuck is None:
        failures.append("app.py: EVAL_STUCK_AFTER is gone. The stuck-evaluation sweep "
                        "has no threshold.")
    else:
        referenced = {n.id for n in ast.walk(stuck.value) if isinstance(n, ast.Name)}
        missing = STUCK_AFTER_MUST_REFERENCE - referenced
        if missing:
            failures.append(
                f"app.py:{stuck.lineno}: EVAL_STUCK_AFTER no longer derives from"
                f" {', '.join(sorted(missing))}.\n"
                f"    It must be computed from the OpenAI budget, not written as a\n"
                f"    literal. A literal goes stale the first time someone changes\n"
                f"    AI_BACKGROUND_TIMEOUT_SECONDS -- and it goes stale silently, in the\n"
                f"    direction where the sweep re-dispatches evaluations that were merely\n"
                f"    slow: two contradictory reports to the author, two OpenAI bills."
            )
        else:
            notes.append("EVAL_STUCK_AFTER is still derived from the OpenAI budget")

        # ── F: and the derivation is actually SOUND ─────────────────────────
        #
        # E only proves the expression MENTIONS the right names. An expression
        # that drops a factor, forgets the retry backoff, or wraps the lot in
        # min(..., timedelta(minutes=10)) passes E and is wrong. This one
        # re-does the arithmetic: it reads the constants, computes the true
        # worst case from first principles, evaluates whatever EVAL_STUCK_AFTER
        # currently says, and fails unless the second clears the first.
        #
        # It is written to be independent of the expression it is checking --
        # if the two ever agree only because they were edited together, this
        # check has stopped being worth anything.
        ns: dict[str, float] = {}
        wanted = ["AI_BACKGROUND_TIMEOUT_SECONDS", "AI_DETECT_TIMEOUT_SECONDS",
                  "AI_BACKGROUND_MAX_RETRIES", "AI_RETRY_AFTER_MAX_SECONDS",
                  "_EVAL_SLOT_WAIT", "EVAL_STUCK_SLACK"]
        for name in wanted:
            val = constant_seconds(app_tree, name, ns)
            if val is not None:
                ns[name] = val

        missing_consts = [n for n in wanted if n not in ns]
        if missing_consts:
            failures.append(
                f"app.py: cannot read {', '.join(missing_consts)}, so the stuck-sweep\n"
                f"    window can no longer be checked against the OpenAI budget. If one of\n"
                f"    these was renamed, rename it here too — do not delete the check."
            )
        else:
            # Two OpenAI calls per scoring: the scoring itself and the AI
            # detector. Each is attempted (retries + 1) times, and each retry
            # sleeps for up to the Retry-After cap before the next attempt.
            attempts = ns["AI_BACKGROUND_MAX_RETRIES"] + 1
            true_worst = (
                (ns["AI_BACKGROUND_TIMEOUT_SECONDS"] + ns["AI_DETECT_TIMEOUT_SECONDS"]) * attempts
                + 2 * ns["AI_BACKGROUND_MAX_RETRIES"] * ns["AI_RETRY_AFTER_MAX_SECONDS"]
                + ns["_EVAL_SLOT_WAIT"]
            )
            try:
                declared = safe_eval(stuck.value, ns)
            except UnevaluableError as exc:
                failures.append(
                    f"app.py:{stuck.lineno}: EVAL_STUCK_AFTER is no longer plain arithmetic"
                    f" this check can evaluate ({exc}).\n"
                    f"    That means nothing verifies it still clears the OpenAI budget.\n"
                    f"    Keep it as arithmetic over the timeout constants."
                )
            else:
                if declared <= true_worst:
                    failures.append(
                        f"app.py:{stuck.lineno}: EVAL_STUCK_AFTER is {declared:.0f}s but a"
                        f" scoring can legitimately take {true_worst:.0f}s.\n"
                        f"        scoring   ({ns['AI_BACKGROUND_TIMEOUT_SECONDS']:.0f}s"
                        f" + {ns['AI_DETECT_TIMEOUT_SECONDS']:.0f}s) x {attempts:.0f} attempts\n"
                        f"      + backoff   2 calls x {ns['AI_BACKGROUND_MAX_RETRIES']:.0f}"
                        f" x {ns['AI_RETRY_AFTER_MAX_SECONDS']:.0f}s Retry-After\n"
                        f"      + queueing  {ns['_EVAL_SLOT_WAIT']:.0f}s (_EVAL_SLOT_WAIT)\n"
                        f"      = {true_worst:.0f}s\n"
                        f"    The sweep would re-dispatch evaluations that are merely slow:\n"
                        f"    two contradictory reports to the author, two OpenAI bills, and\n"
                        f"    it happens during an OpenAI slowdown when responses are longest."
                    )
                else:
                    notes.append(
                        f"stuck-sweep window {declared:.0f}s clears the {true_worst:.0f}s"
                        f" worst-case scoring by {declared - true_worst:.0f}s"
                    )

    # ── Report ──────────────────────────────────────────────────────────────
    if args.list:
        print(f"OpenAI call sites ({len(all_resource_calls)}):")
        for rel, lineno, name, receiver, fn in all_resource_calls:
            mark = "ok " if receiver in APPROVED_CLIENTS else "BAD"
            print(f"  {mark} {rel}:{lineno}  {name}"
                  f"{f'  [{fn}()]' if fn else ''}")
        print()

    if failures:
        print("check_ai_timeouts: FAIL\n")
        for f in failures:
            print(f"  - {f}\n")
        print(f"{len(failures)} problem(s). Every OpenAI call needs a time budget; see the "
              f"clients block at the top of app.py.")
        return 1

    print(f"check_ai_timeouts: OK -- {len(all_resource_calls)} OpenAI call site(s), all on "
          f"a budgeted client.")
    for n in notes:
        print(f"  - {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
