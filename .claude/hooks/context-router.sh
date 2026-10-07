#!/usr/bin/env bash
# context-router.sh — PreToolUse(Bash|Read|Edit|Write|Grep|Glob) reference loader.
#
# WHY THIS EXISTS (measured 2026-09-04).
# The org's instruction files defer detail behind pointers ("see docs/operating-rules/…").
# Across 142 real sessions, docs/operating-rules/ holds 10 files and exactly ONE was ever
# opened, 4 times total. A prose pointer is not a loading mechanism; content moved behind
# one is functionally deleted. That is the failure this hook closes.
#
# Claude Code ships a real mechanism — `paths:` frontmatter on .claude/rules/*.md, which
# loads a rule when Claude READS or EDITS a matching file. Verified working, with a
# negative control (a non-matching rule correctly stays out). But it has a hole that is
# fatal on THIS machine:
#
#     Read(matching)  -> fires        Edit(matching) -> fires
#     Grep(dir)       -> DOES NOT     Bash `cat f`   -> DOES NOT
#
# Auto mode instructs agents to read files with cat/head/sed rather than the Read tool.
# So frontmatter alone would look correct in review and never fire in practice. This hook
# is the second trigger: it watches the Bash/Grep path and injects the same rule body.
#
# CONTRACT
#   - ALWAYS allows. This is a loader, not a guard; it never blocks a tool call.
#     (git-guard.sh and generated-guard.sh own the denying. A loader that can deny
#     would acquire a whole new failure mode for zero benefit.)
#   - Injects each rule at most ONCE per session, so a long Bash-heavy session does not
#     re-pay for the same reference on every call.
#   - Reads .claude/instruction-routes.json through scripts/instruction_routes.py, the
#     same resolver that stamps the frontmatter, so the two triggers cannot drift.
#
# FAIL-OPEN: missing jq/python3/table/rule, unresolvable input, or any resolver error =>
# exit 0 silently. A loader that breaks the session gets disabled within a week, and that
# reopens the very gap it closes. Consistent with the other hooks in this directory.
set -uo pipefail

command -v jq >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

INPUT="$(cat)"
TOOL="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)" || exit 0
case "$TOOL" in
  Bash|Read|Edit|Write|Grep|Glob) ;;
  *) exit 0 ;;
esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$ROOT" ] || exit 0
[ -f "$ROOT/.claude/instruction-routes.json" ] || exit 0
[ -f "$ROOT/scripts/instruction_routes.py" ] || exit 0

# The subject is whatever names a file: an explicit path, or the command text.
SUBJECT="$(printf '%s' "$INPUT" | jq -r '
  [.tool_input.file_path? // empty,
   .tool_input.path?      // empty,
   .tool_input.pattern?   // empty,
   .tool_input.command?   // empty] | join(" ")' 2>/dev/null)" || exit 0
[ -n "${SUBJECT// /}" ] || exit 0

# A Grep/Glob over a DIRECTORY passes it bare (`stromy/agents`), which a
# `stromy/agents/**` route cannot match. Marking a real directory with a
# trailing slash fixes that without letting a bare WORD in prose or a Bash
# command match a directory glob (ORG-PLAN-331 C11).
if [ "$TOOL" = "Grep" ] || [ "$TOOL" = "Glob" ]; then
  GPATH="$(printf '%s' "$INPUT" | jq -r '.tool_input.path? // empty' 2>/dev/null)"
  if [ -n "$GPATH" ] && [ "${GPATH%/}" = "$GPATH" ] && [ -d "$GPATH" ]; then
    SUBJECT="${SUBJECT/$GPATH/$GPATH/}"
  fi
fi

SESSION="$(printf '%s' "$INPUT" | jq -r '.session_id // "nosession"' 2>/dev/null)"
STATE="$ROOT/.claude/.instruction-router/${SESSION}"
mkdir -p "$STATE" 2>/dev/null || exit 0

# Bash command text gets the stricter ORG-PLAN-331 C8 matcher (existence- or
# mutating-verb-gated); a Read/Edit/Grep/Glob's own structured path argument
# keeps the plain match, since the tool is unambiguously about to touch it
# whether or not it exists yet (a Write's target cannot exist beforehand).
if [ "$TOOL" = "Bash" ]; then
  MATCH_FLAG="--match-bash"
else
  MATCH_FLAG="--match"
fi
PAYLOAD=""

# ---- parked / unpublished work on this path (2026-10-02) --------------------
# Work that was parked (park-wip.sh, incl. local-sync's orphan salvage) or left
# unpublished by an ended session is recorded in .claude/.sessions/*.nonterminal.
# The session-start banner shows it once. The session that later PICKS UP the
# plan is the one that must see it, at the moment it touches the file, or it
# redoes the work from the stale copy and repeats the same mistake. Once per
# session per record.
if [ "$TOOL" != "Bash" ] && [ -d "$ROOT/.claude/.sessions" ]; then
  PARKED="$(python3 - "$ROOT" "$SUBJECT" "$STATE" <<'PY' 2>/dev/null
import glob, json, os, sys
root, subject, state = sys.argv[1], sys.argv[2], sys.argv[3]
# realpath both sides: git reports /private/var/… where a tool passes /var/…
rel = (os.path.relpath(os.path.realpath(subject), os.path.realpath(root))
       if os.path.isabs(subject) else subject)
out = []
for rec in sorted(glob.glob(os.path.join(root, ".claude/.sessions/*.nonterminal"))):
    owner = os.path.basename(rec)[: -len(".nonterminal")]
    try:
        top = json.load(open(rec))
    except (OSError, ValueError):
        continue
    for r in [top] + list(top.get("earlier", [])):
        if rel not in r.get("paths", []):
            continue
        key = os.path.join(state, "parked-" + (r.get("ref") or owner + r.get("at", "")).replace("/", "_"))
        if os.path.exists(key):
            continue
        open(key, "w").close()
        if r.get("ref"):
            out.append(f"- `{rel}` has PARKED unpublished edits from session {owner[:20]} ({r.get('at','?')}) "
                       f"at `{r['ref']}` — reason: {r.get('reason','')}. Before editing, look at them "
                       f"(`git show {r['ref']}:{rel}`) and resume them (`scripts/publish-plan.sh --resume {r['ref']}`) "
                       f"or fold them into your edit. Do not redo that work from the stale copy.")
        else:
            out.append(f"- `{rel}` was left UNPUBLISHED by session {owner[:20]} ({r.get('at','?')}): "
                       f"{r.get('reason','')}. Its edits may still be in the working tree; publish or "
                       f"reconcile them before starting over.")
print("\n".join(out))
PY
)"
  [ -n "$PARKED" ] && PAYLOAD="

--- parked work on this path (context-router) ---
${PARKED}"
fi

MATCHED="$(python3 "$ROOT/scripts/instruction_routes.py" "$MATCH_FLAG" "$SUBJECT" 2>/dev/null)" || MATCHED=""

# Content selectors (ORG-PLAN-331 C11): the TEXT a Write/Edit is about to put
# in a file. A new Google Cloud import in `agents/runner.py` names no Cloud
# path and runs no Cloud command, so only its text can route the procedure.
# Passed on stdin, never argv, so a large file cannot hit the argument limit.
if [ "$TOOL" = "Write" ] || [ "$TOOL" = "Edit" ]; then
  CONTENT_MATCHED="$(printf '%s' "$INPUT" | jq -r '
    [.tool_input.content? // empty, .tool_input.new_string? // empty] | join("\n")' 2>/dev/null \
    | python3 "$ROOT/scripts/instruction_routes.py" --match-content - 2>/dev/null)" || CONTENT_MATCHED=""
  if [ -n "$CONTENT_MATCHED" ]; then
    MATCHED="$(printf '%s\n%s' "$MATCHED" "$CONTENT_MATCHED")"
  fi
fi
while IFS= read -r RULE; do
  [ -n "$RULE" ] || continue
  # once per session per rule
  [ -e "$STATE/$RULE" ] && continue
  BODY_FILE="$ROOT/.claude/rules/$RULE"
  [ -f "$BODY_FILE" ] || continue
  : > "$STATE/$RULE"
  # strip the paths: frontmatter — it is routing metadata, not instruction content
  BODY="$(python3 -c '
import re,sys
t=open(sys.argv[1]).read()
print(re.sub(r"\A---\n.*?\n---\n","",t,count=1,flags=re.S).strip())
' "$BODY_FILE" 2>/dev/null)" || continue
  [ -n "$BODY" ] || continue
  PAYLOAD="${PAYLOAD}

--- loaded by context-router (matched \`${SUBJECT:0:80}\`) ---
${BODY}"

  # Record the firing. InstructionsLoaded does NOT observe hook-injected content, so
  # without this row a route that fired correctly is indistinguishable from one that
  # never fired — measured on the first end-to-end probe, which passed while the log
  # stayed empty. instruction-load-report.py reads both sources.
  LOGDIR="$ROOT/.claude/.instruction-log"
  if mkdir -p "$LOGDIR" 2>/dev/null; then
    printf '%s\trouter\t%s\t%s\n' \
      "$(date +%Y-%m-%dT%H:%M:%S)" "${SESSION:0:8}" ".claude/rules/$RULE" \
      >> "$LOGDIR/$(date +%F).tsv" 2>/dev/null || true
  fi
done <<< "$MATCHED"

# Plain `-n`, not `${PAYLOAD// /}`: bash 3.2's pattern-substitution engine is
# pathologically slow on a large multibyte string under this session's C.UTF-8
# locale — a real multi-rule PAYLOAD (~24KB from 4 combined rule bodies, the
# common case for an MCPs/*.py touch) took 20+ seconds on this single check
# (ORG-PLAN-331 C8, found via the new `ruff check MCPs/**/*.py` positive
# control). PAYLOAD is only ever empty or real rule content — never
# whitespace-only, since every appended BODY already passed `[ -n "$BODY" ]` —
# so the substitution bought nothing this hook needs.
[ -n "$PAYLOAD" ] || exit 0

python3 - "$PAYLOAD" <<'PY'
import json, sys
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "allow",
    "additionalContext": sys.argv[1].strip(),
}}))
PY
exit 0
