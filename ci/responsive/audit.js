#!/usr/bin/env node
'use strict';
/**
 * Mobile / responsive regression audit for authors.writeitgreat.com.
 *
 *   node ci/responsive/audit.js --base-url http://127.0.0.1:5000
 *
 * Options:
 *   --base-url <url>    app under test           (default http://127.0.0.1:5000)
 *   --browser <name>    chromium | webkit        (default chromium)
 *   --out <dir>         report + screenshot dir  (default ci/responsive/_report)
 *   --soft              never exit non-zero (report everything as a warning)
 *
 * Exit code 1 if any finding is produced (unless --soft).
 *
 * ===========================================================================
 * WHY THIS EXISTS
 * ===========================================================================
 * On 4 Aug 2026 a new author registering on a phone got a header three rows
 * tall that covered the "Your account has been created" banner, a wordmark
 * squeezed into three lines beside a logo that already said the same words, and
 * a nav that ran off the right edge of the screen -- "Logout" cut in half and
 * "Submit Proposal", the paid CTA, entirely off-screen.
 *
 * Nothing in CI noticed, and nothing in CI COULD have, for three separate
 * reasons that all had to be fixed together:
 *
 *   1. No browser. ci/smoke_app.py drives Flask's test_client, which returns
 *      HTML strings. There is no viewport, no CSS and no layout, so no
 *      geometric bug of any kind is visible to it.
 *
 *   2. Wrong repo. A responsive audit did exist -- in writeitgreat-llc/website,
 *      pointed at the marketing site. It has never made a request to this app.
 *
 *   3. Wrong pages. Even a browser check modelled on smoke_app.py's route list
 *      would have come back green: it only visits UNAUTHENTICATED pages, and
 *      base.html renders the nav only for a signed-in user. The public header
 *      is a bare logo. The bug lives exclusively on the signed-in interior.
 *
 * So this audit signs in. It registers a real author through the real form and
 * measures the pages that author actually sees. A version of it that skipped
 * that step would be a false green -- which is the failure mode this whole file
 * is here to prevent, so do not "simplify" it back to public pages only.
 * ===========================================================================
 */

const fs = require('fs');
const path = require('path');
const playwright = require('playwright');

/* ------------------------------- args ------------------------------- */
function arg(name, fallback) {
  const i = process.argv.indexOf('--' + name);
  return i !== -1 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}
const BASE_URL = arg('base-url', process.env.BASE_URL || 'http://127.0.0.1:5000').replace(/\/$/, '');
const BROWSER = arg('browser', 'chromium');
const OUT_DIR = path.resolve(arg('out', path.join(__dirname, '_report')));
const SOFT = process.argv.includes('--soft');

const SHOT_DIR = path.join(OUT_DIR, 'screenshots');
fs.mkdirSync(SHOT_DIR, { recursive: true });

/* 1px absorbs sub-pixel rounding in getBoundingClientRect. Nothing more. */
const TOLERANCE_PX = 1;

/* Widths chosen from what this app's authors actually hold, not from a device
 * catalogue. 390 is the iPhone that produced the bug report; 412 is the Android
 * viewport author_login.html is already written against; 320 is the narrowest
 * width still worth supporting and is where a header that can wrap has to prove
 * it wraps rather than spills. */
const VIEWPORTS = [
  { name: '320x568', width: 320, height: 568, class: 'mobile' },
  { name: '390x844', width: 390, height: 844, class: 'mobile' },
  { name: '412x915', width: 412, height: 915, class: 'mobile' },
  { name: '768x1024', width: 768, height: 1024, class: 'tablet' },
  { name: '1280x800', width: 1280, height: 800, class: 'desktop' },
];

/* ------------------------------ findings ---------------------------- */
const findings = [];
const measurements = [];

function record(check, page, viewport, message, detail) {
  findings.push({ check, page, viewport: viewport.name, message, detail: detail || null });
}

/* --------------------------- the measurement ------------------------ *
 * Serialised and run INSIDE the page, so it must be self-contained: no
 * closures over module scope, no require(). Config arrives via `opts`.
 * -------------------------------------------------------------------- */
function measurePage(opts) {
  const tol = opts.tolerancePx;

  /* Park the page at the top, in the SAME task that reads the rects.
     getBoundingClientRect is viewport-relative, and a sticky header is supposed
     to overlay the page once you scroll past it -- so "the header reserves its
     height" is only a claim about the top of the document. Two things scroll
     the page out from under this audit otherwise:
       - author_register.html autofocuses its first input, so the browser scrolls
         it into view on load, and scrolls it into view AGAIN after a resize;
       - Chromium's scroll anchoring nudges scrollTop when content above the
         viewport reflows, which a width change does constantly.
     Doing it in an earlier round-trip is not enough: the focus scroll reasserts
     between the two evaluate() calls. Blur first, then scroll, then measure.
     scrollY is returned so a future recurrence is visible in the report rather
     than showing up as a nonsense 1000px "overlap". */
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  window.scrollTo(0, 0);

  const vw = window.innerWidth;

  function describe(el) {
    let s = el.tagName.toLowerCase();
    if (el.id) return '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      const c = el.className.trim().split(/\s+/).filter(Boolean).slice(0, 3);
      if (c.length) s += '.' + c.join('.');
    }
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40);
    return text ? s + ' "' + text + '"' : s;
  }

  const header = document.querySelector('header');
  const main = document.querySelector('main');
  const headerRect = header ? header.getBoundingClientRect() : null;
  const mainRect = main ? main.getBoundingClientRect() : null;

  /* Every element that crosses either vertical edge of the viewport. Reported
   * by selector, because "the document is 40px too wide" tells nobody what to
   * fix. Skips anything invisible or deliberately parked off-screen: the
   * register form's honeypot lives at left:-9999px on purpose. */
  const overflowing = [];
  document.querySelectorAll('body *').forEach(function (el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return;
    if (style.position === 'fixed') return;          // overlays/banners are not page flow
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.left < -tol && r.right < 0) return;        // fully parked off-screen (honeypot)
    if (r.right > vw + tol || r.left < -tol) {
      overflowing.push({
        selector: describe(el),
        left: Math.round(r.left),
        right: Math.round(r.right),
      });
    }
  });

  /* Nav links specifically: an unreachable control is worse than an ugly one,
   * and this is the set the bug report was about. */
  const navOutOfBounds = [];
  document.querySelectorAll('.nav-links a').forEach(function (a) {
    const r = a.getBoundingClientRect();
    if (r.right > vw + tol || r.left < -tol) {
      navOutOfBounds.push({
        text: (a.textContent || '').trim().slice(0, 30),
        left: Math.round(r.left),
        right: Math.round(r.right),
      });
    }
  });

  /* Flash banners: are they clear of the header, and can they be dismissed? */
  const flashes = [];
  document.querySelectorAll('.flash').forEach(function (f) {
    const r = f.getBoundingClientRect();
    const close = f.querySelector('.flash-close');
    const cr = close ? close.getBoundingClientRect() : null;
    flashes.push({
      text: (f.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 60),
      top: Math.round(r.top),
      hasClose: !!close,
      closeWidth: cr ? Math.round(cr.width) : 0,
      closeHeight: cr ? Math.round(cr.height) : 0,
      closeInsideBanner: cr ? (cr.right <= r.right + 1 && cr.top >= r.top - 1) : false,
    });
  });

  return {
    url: location.pathname,
    viewportWidth: vw,
    scrollY: Math.round(window.scrollY),
    documentScrollWidth: document.documentElement.scrollWidth,
    headerHeight: headerRect ? Math.round(headerRect.height) : null,
    headerBottom: headerRect ? Math.round(headerRect.bottom) : null,
    mainTop: mainRect ? Math.round(mainRect.top) : null,
    overflowing: overflowing,
    navOutOfBounds: navOutOfBounds,
    navLinkCount: document.querySelectorAll('.nav-links a').length,
    flashes: flashes,
  };
}

/* ---------------------------- assertions ---------------------------- */
async function auditAt(page, label, viewport, opts) {
  const expectFlash = (opts || {}).expectFlash === true;

  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  // Let the wrap settle before measuring: a flex reflow is not synchronous with
  // the resize as far as getBoundingClientRect is concerned. (Scroll position is
  // handled inside measurePage itself -- see the note there for why it has to
  // be in the same task as the measurement.)
  await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));

  const m = await page.evaluate(measurePage, { tolerancePx: TOLERANCE_PX });
  m.page = label;
  m.viewport = viewport.name;
  measurements.push(m);

  let failed = false;

  /* 1. Horizontal bleed — the classic symptom. */
  if (m.documentScrollWidth > m.viewportWidth + TOLERANCE_PX) {
    failed = true;
    record('bleed', label, viewport,
      `document is ${m.documentScrollWidth}px wide in a ${m.viewportWidth}px viewport — the page scrolls sideways`,
      m.overflowing.slice(0, 8));
  }

  /* 2. Anything past the viewport edge — the cause, named. Reported even when
   *    (1) is clean, because an ancestor with overflow:hidden can clip a
   *    control out of reach while leaving scrollWidth looking healthy. */
  if (m.overflowing.length) {
    failed = true;
    record('overflow', label, viewport,
      `${m.overflowing.length} element(s) cross the viewport edge`,
      m.overflowing.slice(0, 8));
  }

  /* 3. Unreachable navigation. This is the "Logout bleeds off screen" report. */
  if (m.navOutOfBounds.length) {
    failed = true;
    record('nav-unreachable', label, viewport,
      `${m.navOutOfBounds.length} of ${m.navLinkCount} nav link(s) sit outside the viewport`,
      m.navOutOfBounds);
  }

  /* 4. The header must RESERVE its height, not float over the page.
   *    This is the invariant that makes the covered-banner bug impossible, and
   *    it holds no matter how many rows the nav wraps to or which role is
   *    signed in -- which a hard-coded padding-top never could. */
  if (m.headerBottom !== null && m.mainTop !== null && m.mainTop < m.headerBottom - TOLERANCE_PX) {
    failed = true;
    record('header-overlaps-main', label, viewport,
      `header ends at ${m.headerBottom}px but <main> starts at ${m.mainTop}px — ` +
      `the top ${m.headerBottom - m.mainTop}px of the page is underneath the header`,
      { headerHeight: m.headerHeight, navLinkCount: m.navLinkCount });
  }

  /* 5. Flash banners: visible, and dismissible.
   *    expectFlash guards against the quietest failure this audit could have --
   *    a run where the banner never rendered at all, so every flash assertion
   *    passes vacuously and the audit reports green on the exact thing it was
   *    written to check. */
  if (expectFlash && m.flashes.length === 0) {
    failed = true;
    record('flash-missing', label, viewport,
      'expected a flashed banner on this page and found none — the audit cannot ' +
      'have checked anything about flash banners here');
  }
  for (const f of m.flashes) {
    if (m.headerBottom !== null && f.top < m.headerBottom - TOLERANCE_PX) {
      failed = true;
      record('flash-covered', label, viewport,
        `flash banner starts at ${f.top}px, underneath a header that ends at ${m.headerBottom}px`,
        { text: f.text });
    }
    if (!f.hasClose) {
      failed = true;
      record('flash-not-dismissible', label, viewport,
        'flash banner has no .flash-close button', { text: f.text });
    } else if (f.closeWidth < 24 || f.closeHeight < 24) {
      failed = true;
      record('flash-close-too-small', label, viewport,
        `dismiss button is ${f.closeWidth}x${f.closeHeight}px — too small to hit on a phone`,
        { text: f.text });
    } else if (!f.closeInsideBanner) {
      failed = true;
      record('flash-close-misplaced', label, viewport,
        'dismiss button is not inside the top-right of its banner', { text: f.text });
    }
  }

  if (failed) {
    const shot = path.join(SHOT_DIR, `${label}-${viewport.name}.png`.replace(/[^\w.-]/g, '_'));
    await page.screenshot({ path: shot, fullPage: false });
  }

  const status = failed ? 'FAIL' : ' ok ';
  console.log(`  ${status} ${label.padEnd(24)} ${viewport.name.padEnd(10)} ` +
    `header ${String(m.headerHeight).padStart(3)}px  nav ${m.navLinkCount}  flashes ${m.flashes.length}`);
}

/* ------------------------------- run -------------------------------- */
(async () => {
  console.log(`Responsive audit — ${BROWSER} — ${BASE_URL}`);

  const browser = await playwright[BROWSER].launch();
  const context = await browser.newContext();
  const page = await context.newPage();
  let exitCode = 0;

  try {
    /* ---- 1. Public pages. The nav is empty here (base.html gates it on
     *         current_user.is_authenticated), so this leg proves the header is
     *         clean in its SIMPLEST form. It is deliberately not the only leg;
     *         see the note at the top of this file. ---- */
    console.log('\nPublic pages (signed out — nav renders empty):');
    for (const p of ['/author/login', '/author/register']) {
      await page.goto(BASE_URL + p, { waitUntil: 'load', timeout: 30000 });
      for (const vp of VIEWPORTS) await auditAt(page, 'public' + p, vp);
    }

    /* ---- 2. Register for real, at the width the bug was reported at.
     *         This is the exact journey from the report: submit the form, get
     *         redirected to the dashboard, and land on a success banner. No
     *         Turnstile challenge appears because TURNSTILE_* is unset in CI
     *         (app.py: turnstile_enabled()). ---- */
    console.log('\nRegistering an author through the real form…');
    const email = `ci-responsive-${Date.now()}@example.invalid`;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(BASE_URL + '/author/register', { waitUntil: 'load', timeout: 30000 });
    await page.fill('input[name="name"]', 'CI Responsive Audit');
    await page.fill('input[name="email"]', email);
    await page.fill('input[name="password"]', 'ci-audit-password');
    await page.fill('input[name="confirm_password"]', 'ci-audit-password');
    await Promise.all([
      page.waitForURL(/\/author\/dashboard/, { timeout: 30000 }),
      page.click('form button[type="submit"], form input[type="submit"]'),
    ]);
    console.log(`  registered ${email} → ${new URL(page.url()).pathname}`);

    /* ---- 3. The dashboard, carrying the "check your email" banner. This is
     *         the screen from the bug report, in the state it was reported in.
     *         Resized rather than reloaded on purpose: a flashed message is
     *         consumed by the render that shows it, so a reload here would
     *         throw away the very thing being measured. ---- */
    console.log('\nSigned-in author dashboard (with the post-register banner):');
    for (const vp of VIEWPORTS) {
      await auditAt(page, 'author/dashboard+flash', vp, { expectFlash: true });
    }

    /* ---- 4. The rest of the signed-in interior, banner-free. ---- */
    console.log('\nSigned-in author pages:');
    for (const p of ['/author/dashboard', '/author/coaching', '/author/marketing-platform']) {
      const resp = await page.goto(BASE_URL + p, { waitUntil: 'load', timeout: 30000 });
      if (resp && resp.status() >= 400) {
        record('page-error', 'author' + p, VIEWPORTS[0],
          `page returned HTTP ${resp.status()}`);
        exitCode = 1;
        continue;
      }
      for (const vp of VIEWPORTS) await auditAt(page, 'author' + p, vp);
    }
  } catch (err) {
    console.error('\nAudit crashed:', err && err.stack ? err.stack : err);
    record('audit-error', 'n/a', { name: 'n/a' }, String(err && err.message ? err.message : err));
    exitCode = 1;
  } finally {
    await browser.close();
  }

  fs.writeFileSync(
    path.join(OUT_DIR, 'report.json'),
    JSON.stringify({ baseUrl: BASE_URL, browser: BROWSER, measurements, findings }, null, 2));

  console.log(`\n${measurements.length} measurement(s), ${findings.length} finding(s).`);
  if (findings.length) {
    console.log('');
    for (const f of findings) {
      console.log(`  [${f.check}] ${f.page} @ ${f.viewport}`);
      console.log(`      ${f.message}`);
      if (f.detail) console.log(`      ${JSON.stringify(f.detail)}`);
    }
    exitCode = 1;
  }

  console.log(`\nReport: ${path.join(OUT_DIR, 'report.json')}`);
  if (SOFT && exitCode !== 0) {
    console.log('--soft given: reporting findings but exiting 0.');
    exitCode = 0;
  }
  process.exit(exitCode);
})();
