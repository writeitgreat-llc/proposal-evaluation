"""
Recompute analytics_visit.channel with the corrected rules.

    heroku run python backfill_analytics_channel.py --dry-run -a proposal-evaluation
    heroku run python backfill_analytics_channel.py           -a proposal-evaluation

READ THIS BEFORE RUNNING IT — the honest scope is much smaller than it looks.

`analytics_visit` is a working set, not an archive. A row stops being read
after VISIT_IDLE_TIMEOUT (30 minutes) — `_current_visit()` returns a stored row
only while it is non-stale, and otherwise starts a fresh one, which classifies
with whatever code is deployed. The pruner then deletes anything older than 36
hours. So this script only changes a future number for visits that are STILL
OPEN when it runs.

    ==> It is a no-op unless it runs within ~30 minutes of the corrected
        classifier going live. Nothing schedules it and nothing records that it
        ran. If you are reading this later than that, skip it: it will report
        rows updated and change nothing anyone sees.

What it CANNOT do, and where the real repair is. Every event this app has
already forwarded carries the label it had at the time, and the outbox DELETES
rows once the dashboard accepts them, so there is nothing here to re-send.
Re-sending would not help either: the dashboard dedupes on event_uid and drops
the corrected copy, and a fresh uid would double-count the traffic. The
mislabelled history that anyone actually reads lives in wig-dashboard —
wa_events.channel, wa_sessions.channel (first-write-wins, so re-ingest will
never overwrite it) and the wa_daily_dim rollups, which are kept forever. That
is a separate script in that repo: backfill_wa_channel.py.

THE ORDER MATTERS, and getting it wrong lets old labels back in:
    1. deploy the corrected classifier here
    2. within 30 minutes, run this
    3. wait ~2 minutes for the outbox to drain
    4. only then run wig-dashboard's backfill_wa_channel.py

No app import, on purpose — `import app` runs run_migrations() at import time
and starts the re-engagement thread, which would give this one-off dyno a
second analytics drainer competing with the web dyno for the same outbox rows.
It imports analytics_channel_rules only, which is stdlib-only, so there is
exactly one copy of the classifier in play.
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from analytics_channel_rules import (  # noqa: E402
    RULES_VERSION,
    classify_stored_host,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true',
                        help='report what would change and write nothing')
    args = parser.parse_args()

    db_url = os.environ.get('DATABASE_URL', '')
    if not db_url:
        print('ERROR: DATABASE_URL not set')
        return 1
    if db_url.startswith('postgres://'):
        db_url = db_url.replace('postgres://', 'postgresql://', 1)

    import sqlalchemy
    engine = sqlalchemy.create_engine(db_url)

    print(f'Channel rules {RULES_VERSION}'
          f'{"  (DRY RUN — nothing will be written)" if args.dry_run else ""}\n')

    before = Counter()
    after = Counter()
    updates = []        # (id, old, new)
    skipped_null = 0
    truncated = 0

    with engine.begin() as conn:
        rows = conn.execute(sqlalchemy.text(
            'SELECT id, channel, referrer_host, utm_source, utm_medium, '
            'utm_campaign FROM analytics_visit'
        )).fetchall()

        for row in rows:
            visit_id, old, host, utm_source, utm_medium, utm_campaign = row
            before[old or '(null)'] += 1

            # With no stored referrer_host there is nothing to reclassify FROM,
            # and both reasons a row can be in that state mean "leave it".
            #
            # channel IS NULL: never captured. The channel is assigned on a
            # visit's FIRST PAGEVIEW, so an engagement beacon arriving before any
            # pageview is stored with no channel. Recomputing gives 'direct',
            # which converts "never captured" into the positive claim "arrived
            # with no referrer" — not a repair.
            #
            # channel set but not derivable from the utm fields: referrer_host()
            # refuses any non-http(s) scheme and stores NULL while classify() has
            # no scheme gate and used the host, so an
            # `android-app://com.google.android.gm/` referrer is stored as
            # (referrer_host=NULL, channel='organic search'). Recomputing yields
            # 'direct' and DESTROYS a correct label. The input is gone.
            if host is None and (old is None or old not in ('direct', 'campaign')):
                skipped_null += 1
                after[old or '(null)'] += 1
                continue

            if host is not None and len(host) == 120:
                # referrer_host is clamped to 120 chars and the clamp removes
                # the TAIL — which is the registrable domain every anchored
                # match depends on. A host this long reclassifies from a
                # truncated string and may land in the wrong bucket. Counted
                # rather than hidden; the original is not recoverable.
                truncated += 1

            campaign = {}
            if utm_source:
                campaign['utm_source'] = utm_source
            if utm_medium:
                campaign['utm_medium'] = utm_medium
            if utm_campaign:
                campaign['utm_campaign'] = utm_campaign

            # classify_stored_host, never classify(): the stored value is a
            # BARE host, and urlparse('linkedin.com').hostname is None, so
            # handing it to classify() returns 'direct' for every row in the
            # table without erroring once.
            new = classify_stored_host(host, campaign)
            after[new] += 1
            if new != old:
                updates.append((visit_id, old, new))

        print(f'{len(rows)} visit rows examined')
        print(f'{len(updates)} would change' if args.dry_run
              else f'{len(updates)} to update')
        if skipped_null:
            print(f'{skipped_null} left alone (no stored host: never classified, '
                  f'or a non-http referrer — recomputing would invent or '
                  f'destroy a label, not repair one)')
        if truncated:
            print(f'{truncated} had a referrer_host at the 120-char clamp; '
                  f'their reclassification may be degraded')

        print('\nchannel                before   after')
        for channel in sorted(set(before) | set(after)):
            print(f'  {channel:<20} {before.get(channel, 0):>6}  '
                  f'{after.get(channel, 0):>6}')

        moves = Counter((old or '(null)', new) for _, old, new in updates)
        if moves:
            print('\nreclassifications:')
            for (old, new), count in moves.most_common():
                print(f'  {old:>16} -> {new:<16} {count}')

        if args.dry_run or not updates:
            print('\nNothing written.' if args.dry_run else '\nNothing to do.')
            return 0

        for visit_id, _old, new in updates:
            conn.execute(
                sqlalchemy.text('UPDATE analytics_visit SET channel = :c '
                                'WHERE id = :i'),
                {'c': new, 'i': visit_id})
        print(f'\n{len(updates)} visit rows updated.')

        # Queued-but-unsent events carry their own copy of the label inside
        # body_json. Best effort only, and it is worth being clear why: the web
        # dyno's drain thread is running concurrently, it SELECTs, POSTs and
        # then DELETEs, and nothing here fences it. A row already in flight
        # ships its old label and is then deleted. In steady state this queue
        # is empty anyway — the drain empties it every ~2 minutes.
        queued = conn.execute(sqlalchemy.text(
            'SELECT id, body_json FROM analytics_outbox')).fetchall()
        rewritten = 0
        for outbox_id, body_json in queued:
            try:
                body = json.loads(body_json)
            except (TypeError, ValueError):
                continue
            host = body.get('referrer_host')
            if host is None and body.get('channel') not in (
                    None, 'direct', 'campaign'):
                continue
            campaign = {k: body[k] for k in
                        ('utm_source', 'utm_medium', 'utm_campaign')
                        if body.get(k)}
            new = classify_stored_host(host, campaign)
            if new == body.get('channel'):
                continue
            body['channel'] = new
            conn.execute(
                sqlalchemy.text('UPDATE analytics_outbox SET body_json = :b '
                                'WHERE id = :i'),
                {'b': json.dumps(body), 'i': outbox_id})
            rewritten += 1
        print(f'{rewritten} of {len(queued)} queued outbox events relabelled '
              f'(best effort — the drain thread races this).')

    print('\nDone. Next: wait ~2 minutes for the outbox to drain, then run '
          'backfill_wa_channel.py in wig-dashboard — that is where the numbers '
          'anyone reads actually live.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
