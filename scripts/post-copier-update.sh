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
exit "$rc"

