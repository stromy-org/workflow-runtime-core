#!/usr/bin/env python3
"""Generate CLAUDE.md and .github/copilot-instructions.md from canonical AGENTS.md.

AGENTS.md is the authored source of truth (cross-vendor standard, Linux Foundation
AAIF, Dec 2025). Targets, as of 2026-05-19:

    CLAUDE.md                          Claude Code (does not natively read AGENTS.md)
    .github/copilot-instructions.md    GitHub Copilot

Gemini CLI reads AGENTS.md directly via `context.fileName: ["AGENTS.md"]` in
`.gemini/settings.json` — no GEMINI.md needed. Codex CLI reads AGENTS.md
natively. If a stale `GEMINI.md` exists, this script deletes it (the convergence
pass).

Optional per-agent overrides (appended after a `---` separator):
    .agent-overrides/claude.md     → CLAUDE.md
    .agent-overrides/copilot.md    → .github/copilot-instructions.md

Usage:
    render-agent-md.py [--repo PATH] [--check] [--dry-run]

Run from any repo containing AGENTS.md, or pass --repo.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: CI_INPUTS — this script's input DOMAIN, in GitHub's `paths:` grammar.
#: the authored source and overrides it renders FROM, plus the generated
#: outputs — which `--check` reads back to detect a hand-edit. Listing an
#: output here is correct precisely because this script also reads it;
#: what must never be declared is a file it only writes.
#: Verified by scripts/audit-ci-trigger-coverage.py.
CI_INPUTS: tuple[str, ...] = (
    "AGENTS.md",
    ".agent-overrides/**",
    "CLAUDE.md",
    ".github/copilot-instructions.md",
)

GENERATED_HEADER = """<!--
  GENERATED FILE — DO NOT EDIT.
  Source of truth: AGENTS.md (cross-vendor standard).
  Override file:   .agent-overrides/{agent}.md (optional, appended below)
  Regenerate with: scripts/render-agent-md.py
-->

"""

# (agent_key, output_path_relative_to_repo, override_filename)
TARGETS = [
    ("claude", "CLAUDE.md", "claude.md"),
    ("copilot", ".github/copilot-instructions.md", "copilot.md"),
]


RATIONALE_BLOCK = re.compile(
    r"^<!-- rationale:start -->\n(.*?)^<!-- rationale:end -->\n",
    re.DOTALL | re.MULTILINE,
)


def strip_rationale(body: str) -> tuple[str, int]:
    """Excise `rationale:start/end` regions from a generated target.

    AGENTS.md's own policy is "directives only — the reasoning behind each rule
    lives behind a pointer", but 15.1% of the file measured as incident narrative
    (2026-09-04) and prose pointers do not get followed: across 142 sessions, the
    10 files in docs/operating-rules/ were opened 4 times between them.

    So rather than relocate the rationale and lose it, it stays authored inline in
    AGENTS.md — which humans, Codex and Gemini read directly — and is excised from
    the *generated* CLAUDE.md, where every character is billed on every request.

    Excision rather than comment-wrapping is deliberate: Claude Code does strip
    block-level HTML comments before injection (verified), but an HTML comment may
    not contain `--`, and this file's prose is full of `--flag` spellings. Wrapping
    would produce silently malformed output on exactly the passages that mention a
    command. A pointer comment costs nothing and cannot corrupt.
    """
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<!-- rationale excised from this generated file; see AGENTS.md -->\n"

    return RATIONALE_BLOCK.sub(repl, body), count


def build_output(agents_body: str, agent: str, override: str | None) -> str:
    header = GENERATED_HEADER.format(agent=agent)
    body = agents_body.rstrip() + "\n"
    if agent == "claude":
        body, _ = strip_rationale(body)
    else:
        # Non-Claude targets keep the rationale: they are read by humans and by
        # agents whose context budget this repo does not pay for.
        body = RATIONALE_BLOCK.sub(lambda m: m.group(1), body)
    if override:
        body += "\n---\n\n## " + agent.capitalize() + "-specific overrides\n\n"
        body += override.rstrip() + "\n"
    return header + body


def render(repo: Path, *, check: bool, dry_run: bool) -> int:
    agents_md = repo / "AGENTS.md"
    if not agents_md.is_file():
        print(f"error: {agents_md} not found", file=sys.stderr)
        return 1

    agents_body = agents_md.read_text(encoding="utf-8")
    overrides_dir = repo / ".agent-overrides"
    drift = 0

    for agent, out_rel, override_filename in TARGETS:
        out_path = repo / out_rel
        override_path = overrides_dir / override_filename
        override = override_path.read_text(encoding="utf-8") if override_path.is_file() else None
        expected = build_output(agents_body, agent, override)

        current = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
        if current == expected:
            continue

        if check:
            print(f"drift: {out_rel}", file=sys.stderr)
            drift += 1
            continue

        if dry_run:
            print(f"would write: {out_rel} ({len(expected)} bytes)")
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(expected, encoding="utf-8")
        print(f"wrote: {out_rel}")

    # Convergence: GEMINI.md is no longer generated. Delete a stale copy if
    # one exists so the working tree stays clean.
    gemini_md = repo / "GEMINI.md"
    if gemini_md.is_file():
        if check:
            print(
                "drift: GEMINI.md present (no longer generated — "
                "delete or move to .agent-overrides/)",
                file=sys.stderr,
            )
            drift += 1
        elif dry_run:
            print("would delete: GEMINI.md (no longer generated)")
        else:
            gemini_md.unlink()
            print("deleted: GEMINI.md (Gemini reads AGENTS.md via context.fileName)")

    if check:
        if drift:
            print(f"{drift} item(s) out of date — run scripts/render-agent-md.py", file=sys.stderr)
            return 1
        print("agent-md outputs up-to-date with AGENTS.md")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--repo", type=Path, default=Path.cwd())
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="exit 1 if drift detected")
    g.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    return render(args.repo.resolve(), check=args.check, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
