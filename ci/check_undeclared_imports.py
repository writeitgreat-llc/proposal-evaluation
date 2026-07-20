#!/usr/bin/env python3
"""Warn about third-party imports that are not declared in requirements.txt.

WARNING ONLY -- always exits 0. It is wired into ci.yml with
`continue-on-error: true` as well, belt and braces.

WHY IT EXISTS: app.py does `import requests as http_requests`, but `requests` is
not in requirements.txt. It currently installs anyway, purely because
xhtml2pdf -> pyHanko -> requests. The day xhtml2pdf drops that dependency (or
pins a version that does not need it), the Heroku build succeeds and then every
dyno crashes on import with ModuleNotFoundError. Adding `requests==2.32.5` to
requirements.txt makes the dependency explicit and costs nothing.

Run `python3 ci/check_undeclared_imports.py --strict` to make it fail, once the
existing findings are cleaned up.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
import sysconfig
from pathlib import Path

REPO_ROOT = Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parents[1])

# Distribution name in requirements.txt -> module name actually imported.
DIST_TO_MODULE = {
    "flask": "flask",
    "flask-sqlalchemy": "flask_sqlalchemy",
    "flask-login": "flask_login",
    "flask-migrate": "flask_migrate",
    "flask-wtf": "flask_wtf",
    "python-docx": "docx",
    "pypdf2": "PyPDF2",
    "python-dotenv": "dotenv",
    "psycopg2-binary": "psycopg2",
    "pillow": "PIL",
    "email-validator": "email_validator",
    "whitenoise": "whitenoise",
    "wtforms": "wtforms",
    "sqlalchemy": "sqlalchemy",
    "xhtml2pdf": "xhtml2pdf",
    "qrcode": "qrcode",
    "pyotp": "pyotp",
    "openai": "openai",
    "httpx": "httpx",
    "gunicorn": "gunicorn",
    "werkzeug": "werkzeug",
    "cloudinary": "cloudinary",
    "requests": "requests",
}


def annotate(message: str, file: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning file={file}::{message}")
    else:
        print(f"[WARNING] {message}")


def declared_modules() -> set[str]:
    requirements = REPO_ROOT / "requirements.txt"
    if not requirements.is_file():
        return set()

    modules: set[str] = set()
    for line in requirements.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        dist = re.split(r"[=<>!~\[; ]", line, 1)[0].strip().lower()
        if not dist:
            continue
        modules.add(DIST_TO_MODULE.get(dist, dist.replace("-", "_")))
    return modules


def top_level_imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        print(f"[skip] {path.name}: {exc}")
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def is_stdlib(module: str) -> bool:
    """True if `module` ships with Python itself.

    sys.stdlib_module_names is the clean answer but only exists on 3.10+. On
    older interpreters fall back to locating the module and checking whether it
    lives in the stdlib directory rather than site-packages -- without this
    fallback the script silently treats every stdlib import as undeclared.
    """
    names = getattr(sys, "stdlib_module_names", None)
    if names is not None:
        return module in names

    if module in sys.builtin_module_names:
        return True
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    if spec.origin in {"built-in", "frozen"}:
        return True
    stdlib_dir = sysconfig.get_paths().get("stdlib", "")
    return bool(stdlib_dir) and spec.origin.startswith(stdlib_dir) and "site-packages" not in spec.origin


def main() -> int:
    strict = "--strict" in sys.argv

    declared = declared_modules()

    # First-party packages/modules in this repo are not third-party imports.
    first_party = {
        entry.stem if entry.suffix == ".py" else entry.name
        for entry in REPO_ROOT.iterdir()
        if (entry.suffix == ".py") or (entry.is_dir() and (entry / "__init__.py").exists())
    }

    sources = sorted(
        path
        for path in REPO_ROOT.rglob("*.py")
        if "ci/" not in path.relative_to(REPO_ROOT).as_posix()
        and not any(part in {".git", "venv", ".venv", "node_modules"} for part in path.parts)
    )

    findings: dict[str, list[str]] = {}
    for path in sources:
        rel = path.relative_to(REPO_ROOT).as_posix()
        for module in sorted(top_level_imports(path)):
            if module in declared or module in first_party or is_stdlib(module):
                continue
            findings.setdefault(module, []).append(rel)

    print(f"Scanned {len(sources)} Python file(s) against requirements.txt")

    if not findings:
        print("\n=== NO UNDECLARED THIRD-PARTY IMPORTS ===")
        return 0

    for module, files in sorted(findings.items()):
        annotate(
            f"`{module}` is imported ({', '.join(files[:3])}) but is not in "
            f"requirements.txt. It only installs today as a transitive dependency "
            f"of something else. Pin it explicitly.",
            file="requirements.txt",
        )

    print(
        f"\n=== {len(findings)} UNDECLARED IMPORT(S) === "
        f"(warning only -- this does not fail the build)"
    )
    return 1 if strict else 0


if __name__ == "__main__":
    sys.exit(main())
