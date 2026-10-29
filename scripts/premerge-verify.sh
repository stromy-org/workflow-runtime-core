#!/usr/bin/env bash
# premerge-verify.sh — does this MERGED tree still pass its tests, and did they run?
#
# `stromy-org/scripts/premerge_review.py` runs this against the MATERIALIZED
# MERGE TREE (the result of merging the PR into the base), not the PR branch, and
# holds the merge when it exits non-zero. Without this file the review records
# `verifier: null`: its PASS is then bounded to git-level checks and nothing was
# run against the merged result.
#
# THE CONTRACT (premerge_review.py):
#   * executable, at exactly `scripts/premerge-verify.sh` (the mode is read from
#     the merge tree; a non-executable file is a HOLD, not a skip)
#   * exit 0 = the merged tree is sound; non-zero = hold the merge
#   * READ-ONLY outside its own tree: it runs in a throwaway detached worktree, so
#     writing `.venv/` there is fine, but it must never push, merge or write
#     elsewhere. It may read the network (dependency resolution) and nothing more.
#   * bounded: premerge_review.py kills it at 900s and records a failure.
#
# ANTI-VACUOUS. pytest exits 0 when every collected test is skipped, and exits 5
# (no tests collected) only if nothing matched at all. A library whose suite was
# deselected, renamed out of `testpaths` or skipped wholesale would merge "green".
# This fails unless at least one test actually EXECUTED (passed), read from the
# junit report, so a zero-test run cannot read as a pass.
#
# `-m "not integration"` is the expression the org's reusable python CI composes,
# so this re-runs the branch's own green result against the merge result.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

echo "premerge-verify: $(basename "$PWD")"

# No uv means this tree cannot be evaluated: a HOLD, never a pass.
if ! command -v uv >/dev/null 2>&1; then
  echo "  FAIL uv is not installed - this tree cannot be verified, so the merge is held"
  exit 1
fi

# The shared Python CI installs all extras before its required suite. Keep the
# merged-tree suite on that same dependency surface (including optional PDF code).
sync_args=(sync --all-extras)
[ -f uv.lock ] && sync_args+=(--frozen)
if ! out="$(uv "${sync_args[@]}" 2>&1)"; then
  echo "  FAIL dependencies do not resolve (uv ${sync_args[*]})"
  printf '%s\n' "$out" | tail -n 30 | sed 's/^/       /'
  exit 1
fi
echo "  OK   dependencies resolve"

junit="$(mktemp "${TMPDIR:-/tmp}/premerge-junit.XXXXXX")" || exit 1
trap 'rm -f "$junit"' EXIT

uv run pytest -q -m "not integration" -p no:cacheprovider --junitxml="$junit" 2>&1 | tail -n 40
pytest_rc=${PIPESTATUS[0]}

if [ "$pytest_rc" -ne 0 ]; then
  echo "  FAIL test suite (pytest exit $pytest_rc; 5 = nothing collected)"
  exit 1
fi

# Executed = tests - skipped - errors - failures, summed over every <testsuite>.
executed="$(python3 - "$junit" <<'PY'
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError):
    print(0)
    raise SystemExit
suites = [root] if root.tag == "testsuite" else root.iter("testsuite")
total = 0
for s in suites:
    n = lambda k: int(s.get(k, 0) or 0)  # noqa: E731
    total += n("tests") - n("skipped") - n("errors") - n("failures")
print(total)
PY
)"

if ! [ "${executed:-0}" -ge 1 ] 2>/dev/null; then
  echo "  FAIL vacuous run - pytest exited 0 but executed ${executed:-0} tests (all skipped, deselected or none collected)"
  exit 1
fi

echo "  OK   test suite - $executed executed"
exit 0
