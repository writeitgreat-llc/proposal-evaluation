#!/usr/bin/env bash
#
# Push the current commit to Heroku over plain git. No third-party actions.
#
#   usage: ci/deploy_heroku.sh <heroku-app-name>
#   env:   HEROKU_API_KEY       (required) -- repo secret
#          HEROKU_FORCE_PUSH    (optional) -- "true" to overwrite a diverged
#                                             Heroku history. Off by default.
#
# Requires a full-history checkout (actions/checkout with fetch-depth: 0).
# Heroku's builder needs real commits; a shallow clone is rejected.
#
# WHY NOT `git push heroku main` AND HOPE:
#   * Heroku only BUILDS a push to the app's default branch. These apps were
#     created at different times, so one may be "main" and the other "master".
#     Pushing to the wrong one is accepted by the remote and then silently
#     skipped -- a green deploy job that deployed nothing. We look the branch up.
#   * If someone deploys to Heroku straight from their laptop, Heroku's history
#     moves ahead of GitHub's and this push is rejected as non-fast-forward.
#     We detect that FIRST and print exactly which commits only exist on Heroku,
#     rather than either failing cryptically or force-pushing over someone's work.
#
set -euo pipefail

APP_NAME="${1:-}"
FORCE_PUSH="${HEROKU_FORCE_PUSH:-false}"

if [[ -z "$APP_NAME" ]]; then
  echo "usage: $0 <heroku-app-name>" >&2
  exit 2
fi

if [[ -z "${HEROKU_API_KEY:-}" ]]; then
  echo "ERROR: HEROKU_API_KEY is not set." >&2
  echo "       Create one with:  heroku authorizations:create -d 'GitHub Actions deploy'" >&2
  echo "       Then:             gh secret set HEROKU_API_KEY --repo <owner/repo>" >&2
  exit 1
fi

# Never let the token reach the log, even if git decides to echo the remote URL.
redact() { sed -e "s#${HEROKU_API_KEY}#***HEROKU_API_KEY***#g"; }

REMOTE_URL="https://heroku:${HEROKU_API_KEY}@git.heroku.com/${APP_NAME}.git"

git remote remove heroku >/dev/null 2>&1 || true
git remote add heroku "$REMOTE_URL"

echo "Heroku app: ${APP_NAME}"
echo "Deploying commit: $(git rev-parse HEAD)"
echo "                  $(git log -1 --pretty=%s)"

# Sanity: a shallow clone will not deploy. Catch it here with a clear message.
if [[ "$(git rev-parse --is-shallow-repository)" == "true" ]]; then
  echo "ERROR: this is a shallow clone. Heroku needs full history." >&2
  echo "       Set 'fetch-depth: 0' on the actions/checkout step." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. Work out which branch Heroku actually builds (main vs master).
# ---------------------------------------------------------------------------
HEROKU_BRANCH=""
if git ls-remote --heads heroku main 2>/dev/null | grep -q 'refs/heads/main'; then
  HEROKU_BRANCH="main"
elif git ls-remote --heads heroku master 2>/dev/null | grep -q 'refs/heads/master'; then
  HEROKU_BRANCH="master"
else
  # Brand-new app with no releases yet. Heroku accepts main on new apps.
  HEROKU_BRANCH="main"
  echo "NOTE: the Heroku remote has no main/master yet (no releases). Using 'main'."
fi
echo "Heroku deploy branch: ${HEROKU_BRANCH}"

# ---------------------------------------------------------------------------
# 2. Detect divergence BEFORE pushing.
# ---------------------------------------------------------------------------
if git fetch --quiet heroku "$HEROKU_BRANCH" 2>/dev/null; then
  ONLY_ON_HEROKU="$(git rev-list --count FETCH_HEAD ^HEAD 2>/dev/null || echo 0)"
  if [[ "$ONLY_ON_HEROKU" -gt 0 ]]; then
    echo "" >&2
    echo "=====================================================================" >&2
    echo "=== HEROKU AND GITHUB HAVE DIVERGED ===" >&2
    echo "${ONLY_ON_HEROKU} commit(s) exist on Heroku that are NOT on GitHub main:" >&2
    git log --oneline --no-decorate FETCH_HEAD ^HEAD | head -20 >&2
    echo "" >&2
    echo "That means somebody deployed straight to Heroku instead of opening a PR." >&2
    echo "Deploying now would overwrite production code that is not in GitHub." >&2
    echo "" >&2
    echo "To fix it, from a local clone:" >&2
    echo "  git remote add heroku https://git.heroku.com/${APP_NAME}.git" >&2
    echo "  git fetch heroku ${HEROKU_BRANCH}" >&2
    echo "  git checkout -b rescue-heroku-work heroku/${HEROKU_BRANCH}" >&2
    echo "  # open a PR from rescue-heroku-work into main, review it, merge it" >&2
    echo "" >&2
    echo "If you are certain the Heroku-only commits are worthless, re-run this" >&2
    echo "workflow with HEROKU_FORCE_PUSH=true to overwrite them." >&2
    echo "=====================================================================" >&2
    if [[ "$FORCE_PUSH" != "true" ]]; then
      exit 1
    fi
    echo "HEROKU_FORCE_PUSH=true -- overwriting Heroku history anyway." >&2
  else
    echo "Heroku is behind or level with GitHub main. Safe to fast-forward."
  fi
else
  echo "NOTE: could not fetch ${HEROKU_BRANCH} from Heroku (likely a new app). Continuing."
fi

# ---------------------------------------------------------------------------
# 3. Push.
# ---------------------------------------------------------------------------
# Deliberately a plain string, not an array: `"${ARR[@]}"` on an EMPTY array is
# an unbound-variable error under `set -u` in bash 3.x (which is what macOS
# ships, so this script would break for anyone running it locally).
# FORCE_FLAG is always defined, and "--force" contains no spaces or globs, so
# leaving it unquoted is correct here.
FORCE_FLAG=""
if [[ "$FORCE_PUSH" == "true" ]]; then
  FORCE_FLAG="--force"
fi

echo ""
echo "Pushing HEAD -> heroku/${HEROKU_BRANCH} ..."
set +e
# shellcheck disable=SC2086
git push ${FORCE_FLAG} heroku "HEAD:refs/heads/${HEROKU_BRANCH}" 2>&1 | redact
PUSH_STATUS=${PIPESTATUS[0]}
set -e

git remote remove heroku >/dev/null 2>&1 || true

if [[ "$PUSH_STATUS" -ne 0 ]]; then
  echo "" >&2
  echo "=== HEROKU PUSH FAILED (exit ${PUSH_STATUS}) ===" >&2
  echo "The build or the release phase rejected this commit. The previous release" >&2
  echo "is still live -- Heroku does not swap dynos until the build succeeds." >&2
  echo "  heroku logs --tail --app ${APP_NAME}" >&2
  echo "  heroku releases --app ${APP_NAME}" >&2
  exit "$PUSH_STATUS"
fi

echo ""
echo "=== PUSHED TO HEROKU === ${APP_NAME} (${HEROKU_BRANCH})"
