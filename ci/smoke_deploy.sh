#!/usr/bin/env bash
#
# Post-deploy smoke check: hit the live URL until it answers 200, or fail loudly.
#
#   usage: ci/smoke_deploy.sh <url> [max_attempts] [initial_delay_seconds]
#
# Heroku dynos take time to boot, and the release phase (migrations) runs before
# the web dyno starts at all, so the first request after a deploy will usually
# fail. We retry with exponential backoff capped at 30s. Anything that is still
# not 200 after the last attempt is a broken deploy and must fail the job.
#
# Deliberately treats 3xx/4xx/5xx all as "not yet OK" -- a 404 on the homepage is
# just as broken as a 503, and an unexpected redirect on the root URL means
# something is misconfigured.
#
set -uo pipefail

URL="${1:-}"
MAX_ATTEMPTS="${2:-10}"
DELAY="${3:-10}"
MAX_DELAY=30

if [[ -z "$URL" ]]; then
  echo "usage: $0 <url> [max_attempts] [initial_delay_seconds]" >&2
  exit 2
fi

echo "Post-deploy smoke check: $URL"
echo "Up to $MAX_ATTEMPTS attempts, starting with a ${DELAY}s wait (dyno boot + release phase)."

LAST_CODE="none"
LAST_BODY=""

for (( attempt=1; attempt<=MAX_ATTEMPTS; attempt++ )); do
  echo "  waiting ${DELAY}s before attempt ${attempt}/${MAX_ATTEMPTS}..."
  sleep "$DELAY"

  # --max-time guards against a hung dyno holding the job open.
  # -L follows redirects so a canonical-domain redirect is not a false failure,
  # but the FINAL response still has to be a 200.
  LAST_BODY="$(curl -sS -L --max-time 30 -o /tmp/smoke_body.$$ -w '%{http_code}' "$URL" 2>/tmp/smoke_err.$$)"
  CURL_STATUS=$?
  LAST_CODE="$LAST_BODY"

  if [[ $CURL_STATUS -ne 0 ]]; then
    echo "  attempt ${attempt}: curl failed (exit $CURL_STATUS): $(cat /tmp/smoke_err.$$ 2>/dev/null | head -3)"
  elif [[ "$LAST_CODE" == "200" ]]; then
    BYTES=$(wc -c < /tmp/smoke_body.$$ | tr -d ' ')
    echo ""
    echo "=== SMOKE OK === $URL returned 200 (${BYTES} bytes) on attempt ${attempt}."
    rm -f /tmp/smoke_body.$$ /tmp/smoke_err.$$
    exit 0
  else
    echo "  attempt ${attempt}: HTTP ${LAST_CODE}"
  fi

  # Exponential backoff, capped.
  DELAY=$(( DELAY * 2 ))
  if (( DELAY > MAX_DELAY )); then DELAY=$MAX_DELAY; fi
done

echo ""
echo "=====================================================================" >&2
echo "=== DEPLOY SMOKE FAILED ===" >&2
echo "$URL never returned 200 after ${MAX_ATTEMPTS} attempts." >&2
echo "Last status: ${LAST_CODE}" >&2
echo "" >&2
echo "The code IS already deployed to Heroku -- this job failing does not roll" >&2
echo "it back. Investigate now:" >&2
echo "  heroku logs --tail --app <app-name>" >&2
echo "  heroku releases --app <app-name>" >&2
echo "  heroku releases:rollback --app <app-name>   # if it is badly broken" >&2
echo "=====================================================================" >&2

if [[ -s /tmp/smoke_body.$$ ]]; then
  echo "First 40 lines of the last response body:" >&2
  head -40 /tmp/smoke_body.$$ >&2
fi

rm -f /tmp/smoke_body.$$ /tmp/smoke_err.$$
exit 1
