#!/usr/bin/env python3
"""Resolve `.claude/instruction-routes.json` — the ONE routing table for
conditionally-loaded agent instructions.

Why this module exists
----------------------
A conditionally-loaded rule needs two independent triggers, because neither
mechanism alone covers how agents actually reach a file (measured 2026-09-04):

    trigger                         path-scoped rule fires?
    Read / Edit on a matching file  yes
    Grep over the matching dir      NO
    `cat` / `sed` via Bash          NO

This machine runs auto mode, whose standing instruction is to read files with
`cat`/`head`/`sed` rather than the Read tool. A design resting on `paths:`
frontmatter alone would therefore look correct and be dead in the dominant
working mode. So the rule frontmatter covers the tool path and
`.claude/hooks/context-router.sh` covers the Bash path.

Both read THIS table. The org has been bitten before by a classification kept
in two places (`TREE_FREE_CLASSES`, `list_lanes()`): duplicated tables drift
silently because both copies still "work". `--stamp` writes the frontmatter
from the table; `--check` fails when they disagree.

Usage
-----
    python3 scripts/instruction_routes.py --check    # CI gate
    python3 scripts/instruction_routes.py --stamp    # write frontmatter from table
    python3 scripts/instruction_routes.py --match <path-or-pattern>   # Read/Edit/Grep/Glob subject
    python3 scripts/instruction_routes.py --match-bash <command>      # Bash `command` subject
    python3 scripts/instruction_routes.py --match-content -   # Write/Edit TEXT on stdin

Portable kit (ORG-PLAN-331 C11)
-------------------------------
This file and `.claude/hooks/context-router.sh` are copied BYTE-FOR-BYTE into
every governed repo that uses path-scoped rules (`render-org-core.py`), and into
the scaffold templates. Nothing here may name stromy-org: the repo is wherever
this file sits (`scripts/..`), and each repo owns its own
`.claude/instruction-routes.json` and `.claude/rules/`. Edit the canonical copy
in stromy-org/scripts/ and re-render; never edit a satellite's copy.

A route may carry three kinds of selector:

    paths     globs, stamped into the rule's `paths:` frontmatter and matched
              against tool paths and Bash path tokens
    commands  regexes over the Bash command text
    content   regexes over the TEXT a Write/Edit is about to put in a file;
              how a new Google Cloud import in `agents/runner.py` reaches the
              Cloud procedure when neither the path nor a command names it

Agents that run no Claude hook (Codex, Gemini, Copilot, cloud) read the
`## Route index` block this script renders into AGENTS.md between
`<!-- route-index:start -->` and `<!-- route-index:end -->` markers.

`--match` and `--match-bash` are deliberately different precision levels (ORG-PLAN-331
C8). A Read/Edit/Grep/Glob call's `file_path`/`path`/`pattern` is a structured argument
the tool is unambiguously about to touch — whether or not it exists yet (a Write creates
a file that cannot exist beforehand) — so `--match` keeps the plain path-token match.
Bash `command` text has no such guarantee: a path-shaped token can be a `cd` destination
never touched again, a fragment of a quoted interpreter program, a heredoc body, or a
bare mention, and firing on all of them injected the wrong rule three times in one
session (Progress Log, 2026-09-24). `--match-bash` is the stricter reading described on
`bash_path_tokens()` below.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

#: CI_INPUTS — the routing table and the rule files it stamps and checks.
#: Verified by scripts/audit-ci-trigger-coverage.py.
CI_INPUTS: tuple[str, ...] = (".claude/instruction-routes.json", ".claude/rules/**")

#: Bumped when the portable kit (this file + context-router.sh) changes shape.
PORTABLE_KIT_VERSION = 1

REPO = Path(__file__).resolve().parent.parent
TABLE = REPO / ".claude" / "instruction-routes.json"
RULES = REPO / ".claude" / "rules"
AGENTS = REPO / "AGENTS.md"
INDEX_START = "<!-- route-index:start -->"
INDEX_END = "<!-- route-index:end -->"

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def load() -> list[dict]:
    if not TABLE.exists():
        return []
    data = json.loads(TABLE.read_text())
    if data.get("version") != 1:
        raise SystemExit(f"{TABLE}: unsupported version {data.get('version')!r}")
    return data.get("routes", [])


def rule_path(route: dict) -> Path:
    return RULES / route["rule"]


def declared_paths(route: dict) -> list[str]:
    """The globs a rule's frontmatter should carry, per the table."""
    return list(route.get("paths", []))


def frontmatter_paths(text: str) -> list[str] | None:
    """Parse `paths:` out of a rule file. None = no frontmatter at all."""
    m = FRONTMATTER.match(text)
    if not m:
        return None
    out: list[str] = []
    in_paths = False
    for line in m.group(1).splitlines():
        if re.match(r"^paths:\s*$", line):
            in_paths = True
            continue
        if in_paths:
            mm = re.match(r'^\s*-\s*"?([^"]+)"?\s*$', line)
            if mm:
                out.append(mm.group(1))
                continue
            if line.strip() and not line.startswith((" ", "\t", "-")):
                in_paths = False
    return out


def stamp(route: dict) -> bool:
    """Rewrite a rule file's frontmatter from the table. True if changed."""
    p = rule_path(route)
    if not p.exists():
        return False
    text = p.read_text()
    body = FRONTMATTER.sub("", text, count=1)
    globs = "\n".join(f'  - "{g}"' for g in declared_paths(route))
    new = f"---\npaths:\n{globs}\n---\n{body.lstrip()}"
    if new != text:
        p.write_text(new)
        return True
    return False


# A subject (a file path, or a whole shell command) is split into the tokens
# that could name a file. Shell punctuation, quotes and `=` separate them, so
# `--file=terraform/x` and `"clients/a b"` still yield a path-shaped token.
_TOKEN = re.compile(r"[^\s\"'=()<>|;&,`]+")
# Another lane's tree: its paths are repo paths once this prefix is removed.
_LANE_PREFIX = re.compile(r"^\.claude/(?:worktrees|lanes)/[^/]+/")


def _repo_roots() -> list[str]:
    """This checkout, plus the default checkout when this one is a lane."""
    roots = {REPO.as_posix()}
    for marker in ("/.claude/worktrees/", "/.claude/lanes/"):
        if marker in REPO.as_posix():
            roots.add(REPO.as_posix().split(marker, 1)[0])
    return sorted(roots, key=len, reverse=True)


def _abs_to_repo_token(abs_path: str) -> str | None:
    """An absolute filesystem path -> its repo-relative token, or None when it
    is outside every known repo root (this checkout, and the default checkout
    when this one is a worktree/lane). Shared by both matchers below.
    """
    roots = _repo_roots()
    # A symlinked spelling (macOS /var -> /private/var) names the same file,
    # so try the canonical path when the literal one misses.
    for spelling in dict.fromkeys((abs_path, os.path.realpath(abs_path))):
        root = next(
            (r for r in roots if spelling == r or spelling.startswith(r + "/")),
            None,
        )
        if root is not None:
            token = spelling[len(root) + 1 :]
            return _LANE_PREFIX.sub("", token.removeprefix("./")) or "."
    return None


def path_tokens(subject: str) -> list[str]:
    """The repo-relative paths a subject names.

    Precision matters as much as recall: a router that fires on "echo all MCPs
    today" or on `~/.claude/projects/x/MEMORY.md` injects a rule nobody asked
    for, and an over-firing loader gets disabled within a week. So a token is a
    repo path only when it is relative, or absolute under this repository.
    Home-relative and foreign absolute paths never are.

    This is the match used for a tool's own structured path argument
    (Read/Edit `file_path`, Grep/Glob `path`/`pattern`) — the tool is
    unambiguously about to touch that path, whether or not it exists yet, so
    no existence or verb-operand test applies here. `bash_path_tokens()`
    below is the stricter reading for free-form Bash `command` text.
    """
    out: list[str] = []
    for token in _TOKEN.findall(subject):
        if token.startswith("~"):
            continue
        if token.startswith("/"):
            resolved = _abs_to_repo_token(token)
            if resolved is None:
                continue
            out.append(resolved)
            continue
        token = _LANE_PREFIX.sub("", token.removeprefix("./"))
        if token:
            out.append(token)
    return out


# ---- Bash-path precision (ORG-PLAN-331 C8) --------------------------------
#
# A command-text token counts as a subject only if it (a) resolves, after
# `cd` tracking, to a path that EXISTS inside the session's own repo
# toplevel, or (b) is the operand of a file-mutating verb — where existence
# is not required, since a formatter, `git add` or `sed -i` can legitimately
# target a path about to be created or a submodule not checked out locally.
# Even then the path must resolve inside the repo toplevel: a formatter
# invoked on `/private/tmp/x.py` must not load a repo rule. A bare `cd <dir>`
# never names a subject on its own — it only moves the tracked `cwd`.
_MUTATING_BARE_VERBS = {"tee", "mv", "cp", "rm"}
# "such as" in the plan text — representative, not exhaustive.
_FORMATTER_TOOLS = {
    "ruff", "black", "prettier", "eslint", "isort", "mypy", "pyright",
    "flake8", "yapf", "autopep8", "stylelint", "rustfmt", "gofmt",
}
_CD_VERBS = {"cd", "pushd"}
_CONTROL_SPLIT = re.compile(r"&&|\|\||[;\n|]")
_HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)(\w+)\1")
_QUOTED_PROGRAM = re.compile(
    r"\b(?:python3?|node|ruby|perl)\b[^\n]*?\s-[ce]\s+(['\"])(.*?)\1",
    re.DOTALL,
)


def _strip_opaque(command: str) -> str:
    """Remove heredoc bodies and quoted `-c`/`-e` interpreter program text.

    Neither is Bash path syntax: a heredoc body is program input and a quoted
    `-c`/`-e` argument is a whole other language's source. A path-shaped
    string living inside either (a Python glob literal, a printed filename)
    is not a shell path argument and must never become a subject.
    """
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _HEREDOC_START.search(lines[i])
        if m:
            out.append(lines[i][: m.start()])
            delim = m.group(2)
            i += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1  # consume the delimiter line itself, if found
            continue
        out.append(lines[i])
        i += 1
    stripped = "\n".join(out)
    return _QUOTED_PROGRAM.sub(lambda m: m.group(0)[: m.start(2) - m.start(0)], stripped)


def _segment_is_mutating(tokens: list[str], raw_segment: str) -> bool:
    """Is this command segment a file-mutating invocation per C8's list?"""
    tokset = set(tokens)
    if tokset & _MUTATING_BARE_VERBS or tokset & _FORMATTER_TOOLS:
        return True
    if "sed" in tokset and any(t == "-i" or t.startswith("-i") for t in tokens):
        return True
    if "git" in tokset and tokset & {"add", "rm", "mv"}:
        return True
    return ">" in raw_segment  # redirection: `>` / `>>`, fd-juggling included


def bash_path_tokens(command: str) -> list[str]:
    """The repo-relative paths a Bash COMMAND names as real subjects.

    See the module docstring and the block comment above for why this is
    stricter than `path_tokens()`.
    """
    command = _strip_opaque(command)
    cwd: str | None = REPO.as_posix()  # a session starts at the repo root
    subjects: list[str] = []

    for raw_segment in _CONTROL_SPLIT.split(command):
        tokens = _TOKEN.findall(raw_segment)
        if not tokens:
            continue

        if tokens[0] in _CD_VERBS:
            target = next((t for t in tokens[1:] if not t.startswith("-")), None)
            if target is None:
                continue
            if target.startswith("~"):
                cwd = None
            elif target.startswith("/"):
                cwd = os.path.normpath(target)
            elif cwd is not None:
                cwd = os.path.normpath(os.path.join(cwd, target))
            # A bare `cd` never names a subject itself — it only moves `cwd`.
            continue

        mutating = _segment_is_mutating(tokens, raw_segment)
        for token in tokens:
            if token.startswith("~"):
                continue
            if token.startswith("/"):
                resolved = _abs_to_repo_token(token)
            elif cwd is not None:
                resolved = _abs_to_repo_token(os.path.normpath(os.path.join(cwd, token)))
            else:
                resolved = None
            if resolved is None:
                continue
            if mutating or (REPO / resolved).exists():
                subjects.append(resolved)
    return subjects


def matches(route: dict, subject: str, *, kind: str = "path") -> bool:
    """Does this route fire for a file path (`kind="path"`) or a Bash command
    (`kind="bash"`)? See `path_tokens()`/`bash_path_tokens()` for why they differ."""
    tokens = bash_path_tokens(subject) if kind == "bash" else path_tokens(subject)
    for g in declared_paths(route):
        candidates = [g]
        # `**/x` must also match a bare root-level `x`. fnmatch does not treat `**`
        # specially, so "pyproject.toml" fails against "**/pyproject.toml" — which is
        # how python-repos.md was declared and silently would not have fired at the
        # repo root. Fixing the class here rather than adding a bare duplicate to
        # every route: a route author should not have to know this.
        if g.startswith("**/"):
            candidates.append(g[3:])
        # Match whole path tokens, never raw substrings of the command: the old
        # `prefix in subject` fallback fired 11 of 16 routes on prose mentions
        # (ORG-PLAN-324 C5). A directory glob such as `terraform/**` still fires
        # for `terraform/` and anything under it.
        for token in tokens:
            if any(fnmatch.fnmatch(token, c) for c in candidates):
                return True
    for rx in route.get("commands", []):
        if re.search(rx, subject):
            return True
    return False


def matches_content(route: dict, text: str) -> bool:
    """Does a Write/Edit's TEXT carry one of this route's `content` selectors?"""
    return any(re.search(rx, text, re.MULTILINE) for rx in route.get("content", []))


def route_index(routes: list[dict]) -> str:
    """The AGENTS.md `## Route index` block: which procedure to open for which work."""
    lines = [INDEX_START, "## Route index", "",
             ("Claude sessions load these procedures automatically. Every other agent opens "
              "the file named here **before** doing the matching work. The always-on rules "
              "above apply either way."), "",
             "| Open | When you touch |", "|---|---|"]
    for r in routes:
        when = [f"`{g}`" for g in r.get("paths", [])]
        when += [f"a command matching `{rx}`" for rx in r.get("commands", [])]
        when += [f"code containing `{rx}`" for rx in r.get("content", [])]
        cell = ", ".join(when).replace("|", "\\|")
        lines.append(f"| `.claude/rules/{r['rule']}` | {cell} |")
    lines.append(INDEX_END)
    return "\n".join(lines) + "\n"


def stamp_index(routes: list[dict], *, write: bool) -> str | None:
    """Re-render the AGENTS.md route index when its markers exist.

    Returns a problem string when the block is stale (check mode), else None.
    A repo opts in by adding the two marker lines; the control-plane root
    keeps its hand-written subject-area table and carries no markers.
    """
    if not AGENTS.is_file():
        return None
    text = AGENTS.read_text()
    if INDEX_START not in text:
        return None
    start = text.index(INDEX_START)
    end = text.find(INDEX_END, start)
    if end < 0:
        return f"AGENTS.md: {INDEX_START} without {INDEX_END}"
    tail = text[end + len(INDEX_END):]
    new = text[:start] + route_index(routes) + tail.removeprefix("\n")
    if new == text:
        return None
    if write:
        AGENTS.write_text(new)
        return None
    return "AGENTS.md: route index is stale (run --stamp)"


def check() -> int:
    routes = load()
    problems: list[str] = []
    warnings: list[str] = []
    if not routes:
        scoped = [p.name for p in sorted(RULES.glob("*.md")) if frontmatter_paths(p.read_text())]
        if scoped:
            for name in scoped:
                print(f"instruction-routing: FAIL {name}: path-scoped but no instruction-routes.json "
                      "route names it, so the Bash/Grep path will never load it")
            return 1
        for p in sorted(RULES.glob("*.md")):
            print(f"instruction-routing: warning {p.name}: unscoped, loads into every session")
        print("instruction-routing: no routes declared (nothing to check)")
        return 0

    seen: set[str] = set()
    for r in routes:
        name = r.get("rule", "<unnamed>")
        if name in seen:
            problems.append(f"{name}: declared twice in the table")
        seen.add(name)
        for rx in [*r.get("commands", []), *r.get("content", [])]:
            try:
                re.compile(rx)
            except re.error as exc:
                problems.append(f"{name}: selector {rx!r} is not a valid regex ({exc})")

        p = rule_path(r)
        if not p.exists():
            problems.append(f"{name}: route declared but {p.relative_to(REPO)} is missing")
            continue
        if not declared_paths(r):
            problems.append(f"{name}: route has no `paths` — it would never fire")
            continue

        fm = frontmatter_paths(p.read_text())
        if fm is None:
            problems.append(
                f"{name}: no frontmatter — the rule loads UNCONDITIONALLY, "
                f"defeating the route (run --stamp)")
        elif sorted(fm) != sorted(declared_paths(r)):
            problems.append(
                f"{name}: frontmatter {fm} != table {declared_paths(r)} (run --stamp)")

    # A rule file carrying `paths:` but absent from the table is unreachable by
    # the Bash router — the exact half-covered state this module exists to prevent.
    for p in sorted(RULES.glob("*.md")):
        fm = frontmatter_paths(p.read_text())
        if fm and p.name not in seen:
            problems.append(
                f"{p.name}: path-scoped but NOT in instruction-routes.json — "
                f"the Bash path will never load it")
        elif fm is None and p.name not in seen:
            # Still delivered (Claude loads it in every session): a budget
            # finding for the census (`unscoped-rule`), never a delivery hole.
            warnings.append(f"{p.name}: unscoped, loads into every session; give it "
                            f"`paths:` and a route, or fold it into AGENTS.md")

    stale_index = stamp_index(routes, write=False)
    if stale_index:
        problems.append(stale_index)

    for w in warnings:
        print(f"instruction-routing: warning {w}")
    if problems:
        print("instruction-routing: FAIL")
        for x in problems:
            print(f"  - {x}")
        return 1
    print(f"instruction-routing: OK ({len(routes)} route(s), frontmatter in sync)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--stamp", action="store_true")
    ap.add_argument("--match", help="Read/Edit/Grep/Glob-style subject (a path or pattern)")
    ap.add_argument("--match-bash", help="Bash `command` subject (stricter precision, C8)")
    ap.add_argument("--match-content",
                    help="Write/Edit text to scan for `content` selectors; `-` reads stdin")
    args = ap.parse_args()

    if args.match_content is not None:
        text = sys.stdin.read() if args.match_content == "-" else args.match_content
        for r in load():
            if matches_content(r, text):
                print(r["rule"])
        return 0

    if args.match_bash is not None:
        for r in load():
            if matches(r, args.match_bash, kind="bash"):
                print(r["rule"])
        return 0
    if args.match:
        for r in load():
            if matches(r, args.match, kind="path"):
                print(r["rule"])
        return 0
    if args.stamp:
        routes = load()
        for r in routes:
            if stamp(r):
                print(f"stamped {r['rule']}")
        stamp_index(routes, write=True)
        return 0
    return check()


if __name__ == "__main__":
    sys.exit(main())
