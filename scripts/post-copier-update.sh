#!/usr/bin/env bash
set -uo pipefail
rc=0
run() { "$@" || rc=1; }

if [ -d .claude/skills ]; then
  mkdir -p .agents .github
  rm -rf .agents/skills .github/skills
  ln -s ../.claude/skills .agents/skills
  ln -s ../.claude/skills .github/skills
fi
[ ! -f scripts/render-agent-md.py ] || run python3 scripts/render-agent-md.py
[ ! -f pyproject.toml ] || run uv lock
# Required safety seed (ORG-PLAN-374). Seed-once files never take a template
# update, so a repo's own copy is never overwritten -- and a repo scaffolded
# before the seed existed never receives it. When the suite contract is
# ABSENT it is rendered from the updated template with this repo's own
# answers. A present file is never touched, however edited; opting out is a
# declared `not_applicable_reason`, never a deleted file.
seed_required() {
  local missing=() f src ref tmp
  [ -f .copier-answers.yml ] || return 0  # not a Copier-managed tree
  for f in .github/test-contract.yml; do
    [ -e "$f" ] || missing+=("$f")
  done
  [ "${#missing[@]}" -eq 0 ] && return 0
  src=$(awk '/^_src_path:/{print $2}' .copier-answers.yml | tr -d "\"'")
  ref=$(awk '/^_commit:/{print $2}' .copier-answers.yml)
  [ -n "$src" ] && [ -n "$ref" ] || { echo "seed: template source/version unknown" >&2; return 1; }
  tmp=$(mktemp -d) || return 1
  grep -Ev '^(_|#)' .copier-answers.yml > "$tmp/answers.yml"
  if ! uvx copier@9.18.2 copy --trust --defaults --skip-tasks --quiet --vcs-ref "$ref" \
      --data-file "$tmp/answers.yml" "$src" "$tmp/render" >/dev/null; then
    rm -rf "$tmp"; echo "seed: template render failed" >&2; return 1
  fi
  for f in "${missing[@]}"; do
    if [ -f "$tmp/render/$f" ]; then
      mkdir -p "$(dirname "$f")" && cp "$tmp/render/$f" "$f" && echo "seeded missing $f from $ref"
    else
      echo "seed: $f absent from the $ref render" >&2; rm -rf "$tmp"; return 1
    fi
  done
  rm -rf "$tmp"
}
seed_required || rc=1
exit "$rc"
