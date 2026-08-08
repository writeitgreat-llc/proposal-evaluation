#!/usr/bin/env python3
"""What search engines are told about authors.writeitgreat.com.

Wired into .github/workflows/ci.yml as a STEP, not a job -- proposal-ci is the
required status check, and a new job would be a new status context that gates
nothing until ci/setup_branch_protection.sh and deploy.yml's BLOCKING_CHECKS
are updated in lockstep.

This host had no crawler instructions at all. The origin 404'd /robots.txt and
Cloudflare answered with its content-signals boilerplate: 1,248 bytes of pure
comment carrying no User-agent, no Disallow and no Sitemap line. Meanwhile
Googlebot has an unbroken followable path from the marketing site into the
STAFF sign-in screen -- six indexable pages on writeitgreat.com link
/author/register, which links /author/login, which links /admin/login at the
foot of the page.

The distinction this file exists to defend, because getting it backwards is
the classic own-goal:

    robots.txt governs the FETCH. noindex governs the INDEX.

`Disallow: /admin` plus a noindex header on /admin would be self-defeating: the
crawler never fetches, so it never reads the noindex, and a URL already known
from a link stays in the index as a bare blue link with no snippet and no way
to remove it. So auth and staff paths are crawlable-but-noindexed, and only
capability-token URLs -- which no visitor should ever reach from a search
result, and where the fetch itself is what we want to prevent -- are blocked
outright.

Usage:
    python ci/check_crawl_posture.py        # from the repo root
Exit codes: 0 = fine, 1 = a check failed.
"""
from __future__ import annotations

import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="wig-crawl-posture-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMPDIR, "crawl.db")
# app.py builds an OpenAI client at import time and raises without this. DUMMY.
os.environ.setdefault("OPENAI_API_KEY", "ci-dummy-openai-credential")
os.environ.setdefault("SECRET_KEY", "ci-crawl-posture-test-key")
os.environ["APP_BASE_URL"] = "http://localhost:5000"

from app import app  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL {label}" + (f" -- {detail}" if detail else ""))
        FAILURES.append(label)


def main() -> int:
    client = app.test_client()

    print("\nrobots.txt is served by this app, not synthesised by Cloudflare")
    res = client.get("/robots.txt")
    body = res.get_data(as_text=True)
    check("it exists and is plain text",
          res.status_code == 200
          and res.headers.get("Content-Type", "").startswith("text/plain"),
          f"status={res.status_code} type={res.headers.get('Content-Type')!r}")
    check("it carries a real User-agent directive, not only comments",
          "User-agent: *" in body, f"body={body!r}")

    print("\nThe public funnel stays crawlable")
    # Blocking these would stop Google reading the page the marketing site
    # sends people to, and would discard the anchor-text signal, without
    # reliably keeping anything out of the index.
    disallowed = [line.split(":", 1)[1].strip()
                  for line in body.splitlines()
                  if line.strip().lower().startswith("disallow:")
                  and line.split(":", 1)[1].strip()]
    for keep in ("/author/register", "/social-strategy"):
        blocked = [d for d in disallowed if keep.startswith(d)]
        check(f"{keep} is not blocked from crawling", not blocked,
              f"blocked by Disallow rules {blocked}")

    print("\nCapability-token URLs are blocked from being fetched at all")
    for path in ("/results/", "/download/",
                 "/social-strategy/result/", "/social-strategy/pdf/"):
        check(f"{path} is disallowed", path in disallowed,
              f"disallow list is {disallowed}")

    print("\nAuth and staff pages are crawlable but refuse indexing")
    # Crawlable ON PURPOSE: the noindex below can only be obeyed if the
    # crawler is allowed to fetch the page and read the header.
    for path in ("/admin/login", "/author/forgot-password"):
        res = client.get(path, base_url="https://authors.writeitgreat.com")
        header = res.headers.get("X-Robots-Tag", "")
        check(f"{path} sends X-Robots-Tag: noindex",
              "noindex" in header,
              f"status={res.status_code} X-Robots-Tag={header!r}")
        blocked = [d for d in disallowed if path.startswith(d)]
        check(f"{path} is NOT also blocked in robots.txt (the self-defeat trap)",
              not blocked,
              f"blocked by {blocked} -- a blocked crawler never reads the "
              f"noindex, so an already-indexed URL could never be removed")

    print("\nThe public funnel does NOT refuse indexing")
    for path in ("/author/register", "/social-strategy"):
        res = client.get(path, base_url="https://authors.writeitgreat.com")
        check(f"{path} carries no noindex",
              "noindex" not in res.headers.get("X-Robots-Tag", ""),
              f"X-Robots-Tag={res.headers.get('X-Robots-Tag')!r} -- this is "
              f"the marketing site's own call-to-action target")

    print("\nThe duplicated confidentiality statement names its original")
    # The marketing site ships the identical text at
    # writeitgreat.com/confidentiality. One Search Console domain property
    # covers both hosts, so this is duplicate content with no declared source.
    res = client.get("/confidentiality", base_url="https://authors.writeitgreat.com")
    html = res.get_data(as_text=True)
    check("it declares a cross-domain canonical to the marketing site",
          'rel="canonical" href="https://writeitgreat.com/confidentiality"' in html,
          "the canonical is missing -- note base.html must DEFINE the "
          "head_extra block, or the child template's override renders nothing")

    if FAILURES:
        print(f"\n{len(FAILURES)} problem(s) found.", file=sys.stderr)
        return 1
    print("\nOK: crawl posture is intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
