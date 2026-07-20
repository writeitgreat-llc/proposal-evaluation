#!/usr/bin/env bash
#
# Apply branch protection to main. Run once per repo; safe to re-run (it PUTs the
# whole policy, so re-running resets any drift back to exactly what is here).
#
#   ci/setup_branch_protection.sh                      # apply to BOTH repos
#   ci/setup_branch_protection.sh website              # just the marketing site
#   ci/setup_branch_protection.sh proposal-evaluation  # just the proposal tool
#   ci/setup_branch_protection.sh <owner/repo> "Check A" "Check B" ...
#
#   env: REVIEWS=1        required approving reviews (default 1; 0 for none)
#        BRANCH=main      protected branch
#        DRY_RUN=1        print the payload and the equivalent gh command, change nothing
#
# You need ADMIN on the repo. Ray is admin on both writeitgreat-llc repos.
#
# ===========================================================================
# !! THE ONE THING THAT BREAKS THIS: CHECK NAMES ARE COUPLED TO JOB NAMES !!
#
# A "required status check context" is the JOB NAME GitHub reports, which is:
#     the job's `name:` field, if it has one
#     otherwise the job's key under `jobs:`
#
# These strings are copied from .github/workflows/ci.yml in each repo:
#
#   writeitgreat-llc/website              .github/workflows/ci.yml
#     jobs.intake-suite.name   ->  "Intake regression suite"
#     jobs.static-checks.name  ->  "Engine parity and template integrity"
#     jobs.app-boot.name       ->  "App boots on Python 3.11"
#
#   writeitgreat-llc/proposal-evaluation  .github/workflows/ci.yml
#     jobs.proposal-ci.name    ->  "proposal-ci"
#
# If anyone edits a `name:` in ci.yml, EVERY PR IN THAT REPO WILL HANG at
# "Expected - Waiting for status to be reported", because GitHub is waiting on a
# check name that nothing will ever report. There is no error message; the PR is
# just permanently un-mergeable. Fix = re-run this script with the new names.
#
# Second gotcha: matrix jobs report as "name (matrix-value)". Neither repo uses
# a matrix today. If one is added, the context names change shape.
#
# Third gotcha: never add a `paths:` filter to a CI workflow whose job is a
# required check. A path-filtered workflow does not report "skipped", it reports
# nothing, and the PR hangs exactly as above.
# ===========================================================================
set -euo pipefail

BRANCH="${BRANCH:-main}"
REVIEWS="${REVIEWS:-1}"
DRY_RUN="${DRY_RUN:-0}"

WEBSITE_REPO="writeitgreat-llc/website"
WEBSITE_CHECKS=(
  "Intake regression suite"
  "Engine parity and template integrity"
  "App boots on Python 3.11"
)

PROPOSAL_REPO="writeitgreat-llc/proposal-evaluation"
PROPOSAL_CHECKS=(
  "proposal-ci"
)

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: the GitHub CLI (gh) is not installed. See https://cli.github.com" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: not logged in to GitHub. Run: gh auth login" >&2
  exit 1
fi

protect() {
  local repo="$1"; shift
  local checks=("$@")

  echo ""
  echo "==================================================================="
  echo "Repo:     ${repo}"
  echo "Branch:   ${BRANCH}"
  echo "Reviews:  ${REVIEWS}"
  echo "Required status checks:"
  printf '  - %s\n' "${checks[@]}"
  echo "==================================================================="

  # Build the checks array as JSON, quoting safely (names contain spaces).
  local checks_json
  checks_json="$(printf '%s\n' "${checks[@]}" \
    | python3 -c 'import json,sys; print(json.dumps([{"context": line.rstrip("\n")} for line in sys.stdin if line.strip()]))')"

  # strict:true == "Require branches to be up to date before merging". It forces
  # the PR to be rebased on current main before it can land, so main is never
  # broken by two PRs that were individually green but conflict together.
  local payload
  payload="$(python3 -c '
import json, sys
checks = json.loads(sys.argv[1])
reviews = int(sys.argv[2])
print(json.dumps({
    "required_status_checks": {"strict": True, "checks": checks},
    "enforce_admins": False,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": True,
        "require_code_owner_reviews": False,
        "required_approving_review_count": reviews,
        "require_last_push_approval": False,
    },
    "restrictions": None,
    "required_linear_history": False,
    "allow_force_pushes": False,
    "allow_deletions": False,
    "block_creations": False,
    "required_conversation_resolution": True,
    "lock_branch": False,
    "allow_fork_syncing": False,
}, indent=2))
' "$checks_json" "$REVIEWS")"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY_RUN=1 -- this is the exact call that would be made:"
    echo ""
    echo "  gh api --method PUT \\"
    echo "    -H \"Accept: application/vnd.github+json\" \\"
    echo "    -H \"X-GitHub-Api-Version: 2022-11-28\" \\"
    echo "    repos/${repo}/branches/${BRANCH}/protection \\"
    echo "    --input - <<'JSON'"
    echo "$payload"
    echo "JSON"
    return 0
  fi

  echo "$payload" | gh api \
    --method PUT \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "repos/${repo}/branches/${BRANCH}/protection" \
    --input - > /dev/null

  echo "Applied. Read back from GitHub:"
  gh api \
    -H "Accept: application/vnd.github+json" \
    "repos/${repo}/branches/${BRANCH}/protection" \
    --jq '{
      required_checks:        (.required_status_checks.checks | map(.context)),
      strict_up_to_date:      .required_status_checks.strict,
      required_reviews:       .required_pull_request_reviews.required_approving_review_count,
      dismiss_stale_reviews:  .required_pull_request_reviews.dismiss_stale_reviews,
      force_pushes_allowed:   .allow_force_pushes.enabled,
      deletions_allowed:      .allow_deletions.enabled,
      conversations_required: .required_conversation_resolution.enabled,
      admins_enforced:        .enforce_admins.enabled
    }'
}

case "${1:-both}" in
  both)
    protect "$WEBSITE_REPO"  "${WEBSITE_CHECKS[@]}"
    protect "$PROPOSAL_REPO" "${PROPOSAL_CHECKS[@]}"
    ;;
  website)
    protect "$WEBSITE_REPO"  "${WEBSITE_CHECKS[@]}"
    ;;
  proposal-evaluation|proposal)
    protect "$PROPOSAL_REPO" "${PROPOSAL_CHECKS[@]}"
    ;;
  */*)
    repo="$1"; shift
    if [[ $# -eq 0 ]]; then
      echo "ERROR: give at least one required check context after the repo." >&2
      exit 2
    fi
    protect "$repo" "$@"
    ;;
  *)
    echo "usage: $0 [both|website|proposal-evaluation|<owner/repo> <check> ...]" >&2
    exit 2
    ;;
esac

echo ""
echo "-------------------------------------------------------------------"
echo "Done."
echo ""
echo "enforce_admins is FALSE on purpose. With three people, an admin has to be"
echo "able to land an emergency fix at 2am without being locked out by his own"
echo "policy. It is an escape hatch, not the normal path, and GitHub records"
echo "every single use of it in the branch's audit trail."
echo ""
echo "Verify the check names really match by opening one throwaway PR and"
echo "confirming the required checks turn green rather than sitting at"
echo "\"Expected - Waiting for status to be reported\"."
echo "-------------------------------------------------------------------"
