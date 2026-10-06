#!/usr/bin/env python3
"""Standalone org-core consistency check (ORG-PLAN-331 C4, layer L1).

Every governed stromy-org repository carries the org-core block in two places:

    AGENTS.md            between `<!-- org-core:start vN -->` and `<!-- org-core:end -->`
    .agents/org-core.md  an inert reference copy of exactly that block (no agent
                         auto-loads it)

`--check` proves the two are byte-identical and carry a valid version marker,
and fails if either is missing. It needs nothing but this clone and the Python
standard library, so it runs the same in a repo's CI, in its premerge-verify.sh
and on a laptop.

What it does NOT prove is that the reference matches the canonical source
(`global-skills/global-instructions/org-core.md`). A standalone clone cannot
see that source. The central audit (`stromy-org/scripts/render-org-core.py
--check`, `audit-org-core-fleet.py`) proves it. Neither green result substitutes
for the other: tampering with the block alone fails here, while tampering with
the block and the reference together fails only centrally.

This file is copied byte-for-byte into every governed repo and scaffold template
by `render-org-core.py`. Edit the canonical copy in stromy-org/scripts/ only.

Usage:
    check-org-core.py [--repo PATH] --check
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: CI_INPUTS — read back by `--check`; there are no other inputs.
CI_INPUTS: tuple[str, ...] = ("AGENTS.md", ".agents/org-core.md")

REFERENCE_REL = ".agents/org-core.md"
START_RE = re.compile(r"^<!-- org-core:start v(?P<version>[1-9][0-9]*) (?P<note>[^\n]*)-->\n", re.MULTILINE)
END_MARK = "<!-- org-core:end -->\n"
#: Codex cloud cuts project docs at its 32 KiB default (`project_doc_max_bytes`).
CLOUD_CODEX_BYTES = 32_768


class BlockError(ValueError):
    """The text does not hold exactly one well-formed org-core block."""


def extract_block(text: str) -> tuple[str, int]:
    """Return (block, version) for the single org-core block in `text`.

    The block runs from the start marker line through the end marker line,
    both included, so it compares byte-for-byte with the reference file.
    """
    starts = list(START_RE.finditer(text))
    if not starts:
        raise BlockError("no `<!-- org-core:start vN ... -->` marker")
    if len(starts) > 1:
        raise BlockError(f"{len(starts)} org-core start markers (expected exactly 1)")
    start = starts[0]
    end = text.find(END_MARK, start.end())
    if end < 0:
        raise BlockError("start marker without a following `<!-- org-core:end -->` line")
    if text.count(END_MARK) != 1:
        raise BlockError(f"{text.count(END_MARK)} org-core end markers (expected exactly 1)")
    return text[start.start(): end + len(END_MARK)], int(start.group("version"))


def check(repo: Path) -> list[str]:
    """Findings (empty = consistent). Never raises on a malformed repo."""
    findings: list[str] = []
    agents = repo / "AGENTS.md"
    ref = repo / REFERENCE_REL
    if not agents.is_file():
        findings.append("org-core-missing: AGENTS.md not found")
    if not ref.is_file():
        findings.append(f"org-core-missing: {REFERENCE_REL} not found")
    if findings:
        return findings

    agents_text = agents.read_text(encoding="utf-8")
    ref_text = ref.read_text(encoding="utf-8")
    try:
        block, version = extract_block(agents_text)
    except BlockError as exc:
        return [f"org-core-missing: AGENTS.md: {exc}"]
    try:
        ref_block, ref_version = extract_block(ref_text)
    except BlockError as exc:
        return [f"org-core-reference-invalid: {REFERENCE_REL}: {exc}"]

    if ref_block != ref_text:
        findings.append(f"org-core-reference-invalid: {REFERENCE_REL} holds text outside the block")
    if version != ref_version:
        findings.append(f"org-core-stale: AGENTS.md block is v{version}, {REFERENCE_REL} is v{ref_version}")
    elif block != ref_block:
        findings.append(f"org-core-tampered: AGENTS.md block differs from {REFERENCE_REL} "
                        "(edit the canonical source in global-skills and re-render; never this copy)")
    return findings


def size_warnings(repo: Path) -> list[str]:
    agents = repo / "AGENTS.md"
    if agents.is_file() and (n := agents.stat().st_size) > CLOUD_CODEX_BYTES:
        return [(f"codex-cloud-truncation: AGENTS.md is {n:,} bytes, over the {CLOUD_CODEX_BYTES:,}-byte "
                 "Codex cloud default; the tail never reaches a cloud session")]
    return []


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", type=Path, default=Path.cwd())
    p.add_argument("--check", action="store_true", required=True,
                   help="exit 1 unless the AGENTS.md block equals the inert reference")
    args = p.parse_args(argv)
    repo = args.repo.resolve()
    findings = check(repo)
    for w in size_warnings(repo):
        print(f"warning: {w}", file=sys.stderr)
    for f in findings:
        print(f"FAIL {f}", file=sys.stderr)
    if findings:
        return 1
    print("org-core: AGENTS.md block matches .agents/org-core.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
