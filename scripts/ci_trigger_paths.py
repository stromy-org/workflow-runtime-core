#!/usr/bin/env python3
"""What does a workflow's `paths:` filter actually watch?

Two callers need the same answer and were each about to guess at it:

  * `audit-ci-trigger-coverage.py` asks whether the files a test suite READS are
    files that make the suite RUN. A test that asserts a path no trigger watches
    goes stale-green: it passes on the last commit that happened to touch a
    watched path, and stays passing while the thing it asserts rots underneath.
  * `ci_attribution.py` asks whether the base branch's last run is still evidence
    ABOUT the base. It is not, if the base has taken commits since that run which
    would have triggered the workflow — the run predates the break.

Both questions are "does this changed file match this workflow's filter", so the
matcher lives once, here, rather than twice and differently.

GitHub's filter-pattern grammar (the subset workflows actually use):

    *   matches zero or more characters, but NOT `/`
    **  matches zero or more characters, INCLUDING `/`
    **/ matches zero or more whole path SEGMENTS — so `**/README.md` matches a
        root `README.md` as well as `docs/README.md`. The docs gloss it as "a
        README.md file anywhere in the repository", and "anywhere" includes the
        top. Read `**` as "zero or more of any character" and you conclude the
        opposite, because `.*` + `/` still demands a slash. See ZERO-SEGMENT.
    ?   matches zero or one of the PRECEDING character — a quantifier, not a
        wildcard. `*.jsx?` is the docs' own example, and it matches `.js` and
        `.jsx`. It does NOT mean "one character other than `/`".

Everything else is literal. A pattern with no wildcard matches that one path
exactly — `paths: ['scripts']` watches a FILE called `scripts`, not the
directory, which is the trap `covers_tree` below exists to make unmissable.

ZERO-SEGMENT (`**/`), established on live evidence 2026-09-16
-------------------------------------------------------------
This module compiled `**/` to `.*/` until then, so `**/AGENTS.md` did not match
a root `AGENTS.md`. GitHub's matcher does. The proof is a push, not a doc:

    b218c01e changed exactly four files — .github/copilot-instructions.md,
    AGENTS.md, CLAUDE.md, docs/operating-rules/landing-and-completion.md —
    and `sync-on-agents-md-change.yml` RAN on it. That workflow's only
    candidate pattern is `**/AGENTS.md`, and its parent 031af8ba carries its
    own run, which makes b218c01e a single-commit push whose aggregate diff is
    those four files and nothing else.

Getting it wrong was not a rounding error, because all three callers read the
answer in a different direction and it broke each of them differently:

  * the trigger auditor UNDER-reported coverage, so a watched path read as
    UNWATCHED — a false finding that pushes an author to add a redundant entry;
  * asking the reverse question (is this entry a dependency of anything?), a
    LIVE entry read as dead, which is a prompt to delete a working trigger;
  * `ci_attribution._measure_lag` printed "NONE of them matches this workflow's
    `paths:` — so no run exists ... and none ever will" about commits for which
    a run was in fact owed. That is a false claim in evidence an operator acts
    on, and it escalates rather than reassures.

Corrected, trigger volume over 300 commits of `main` rose 483 -> 507 runs:
`sync-on-agents-md-change` 5 -> 9 (+80%), `quality-gates` 117 -> 127 (+8.5%).

A NOTE ON WHAT THIS ANSWERS ABOUT `push`
----------------------------------------
GitHub evaluates a `push` filter against the AGGREGATE diff of the push and
attributes the run to the push HEAD, not to each commit in it. So "does this
commit match" is the right question for a cost model over a commit sample, and
the WRONG question to ask of run history: a matching commit that was not a push
head has no run of its own and never will. Six markdown-only commits in this
repo look unwatched for exactly that reason and are not.

Reference: https://docs.github.com/actions/reference/workflow-syntax-for-github-actions#filter-pattern-cheat-sheet
"""

from __future__ import annotations

import functools
import re
from pathlib import Path


#: CI_INPUTS — this module's input DOMAIN, in GitHub's `paths:` grammar.
#: a shared LIBRARY module: it resolves what its caller passes and names no
#: repo path of its own. An empty declaration is a positive claim — read
#: this module, it has no inputs — and is what lets the reverse check
#: (rule 4) run at all for the workflows that reach it.
#: Verified by scripts/audit-ci-trigger-coverage.py.
CI_INPUTS: tuple[str, ...] = (
)

#: The events whose `paths:` decide whether a change runs the workflow.
DEFAULT_EVENTS = ("pull_request", "push")


def load(workflow: Path) -> dict:
    """The parsed workflow, or `{}` when it is not a mapping.

    Public because the trigger auditor asks this file a second question — which
    scripts do its `run:` steps execute — and a second YAML parse in a second
    module is a second chance for the two to disagree about what the file says.
    """
    import yaml

    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def trigger_globs(workflow: Path, events: tuple[str, ...] = DEFAULT_EVENTS
                  ) -> dict[str, list[str]]:
    """`{event: [glob, ...]}` for each requested event that declares `paths:`.

    An event present with no `paths:` key runs on EVERY change to the repo, and
    is reported as the single glob `**` — unfiltered is maximal coverage, not
    missing coverage, and a caller that saw `[]` there would conclude the
    opposite. An event absent entirely is absent from the result.

    YAML 1.1 reads a bare `on:` as the boolean True, so the key is looked up
    both ways; this has bitten every hand-rolled workflow parser in the estate.
    """
    data = load(workflow)
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        on = {on: None}
    if isinstance(on, list):
        on = {k: None for k in on}
    if not isinstance(on, dict):
        return {}
    out: dict[str, list[str]] = {}
    for event in events:
        if event not in on:
            continue
        spec = on[event]
        if not isinstance(spec, dict) or "paths" not in spec:
            out[event] = ["**"]
            continue
        paths = spec.get("paths") or []
        out[event] = [str(p) for p in paths if isinstance(p, (str, int, float))]
    return out


def push_branch_matches(workflow: Path, branch: str) -> bool:
    """Does this workflow's `push` trigger fire for a push to `branch`?

    `trigger_globs` answers WHICH paths a push watches; this answers whether the
    push event fires for this branch at all (ORG-PLAN-328 C8). A workflow with
    `push: branches: [release]` watches `src/**` for release pushes only, and a
    gate that ignored the branch filter would demand a local-CI receipt for a
    `main` push no CI run will ever look at.

    GitHub's rules: no `push` → False. `push` with no branch filters → True,
    EXCEPT when it filters only tags (`tags:`/`tags-ignore:` alone), which makes
    branch pushes not fire at all. `branches:` → True iff a pattern matches;
    `branches-ignore:` → True unless a pattern matches. Patterns use the same
    glob grammar as `paths:` (`*` stays within a segment, `**` crosses them), and
    a leading `!` in `branches:` negates a previous match, in order.
    """
    data = load(workflow)
    on = data.get("on", data.get(True))
    if isinstance(on, str):
        on = {on: None}
    if isinstance(on, list):
        on = {k: None for k in on}
    if not isinstance(on, dict) or "push" not in on:
        return False
    spec = on["push"]
    if not isinstance(spec, dict):
        return True
    if "branches" in spec:
        hit = False
        for pat in spec.get("branches") or []:
            pat = str(pat)
            if pat.startswith("!"):
                if matches(branch, pat[1:]):
                    hit = False
            elif matches(branch, pat):
                hit = True
        return hit
    if "branches-ignore" in spec:
        return not any(matches(branch, str(p)) for p in spec.get("branches-ignore") or [])
    if "tags" in spec or "tags-ignore" in spec:
        return False
    return True


def _filtered_in(path: str, patterns: list[str]) -> bool:
    """GitHub's per-path filter walk: patterns in order, a leading `!` un-matches."""
    hit = False
    for pat in patterns:
        pat = str(pat)
        if pat.startswith("!"):
            if matches(path, pat[1:]):
                hit = False
        elif matches(path, pat):
            hit = True
    return hit


def push_fires(workflow: Path, branch: str, paths: list[str]) -> bool:
    """Would a push of these changed `paths` to `branch` start this workflow?

    The gate's question (ORG-PLAN-328 C14), deliberately separate from
    `trigger_globs`: that one reports a `paths-ignore` push as `**` because, for
    the trigger AUDITOR, unfiltered is maximal coverage — the safe direction for
    "is my script watched?". For a gate asking "will CI run after this push?"
    the same reading is the unsafe direction: it demanded a receipt for a
    `BACKLOG.md` push to a repo whose workflow ignores `*.md` and runs nothing.

    GitHub's semantics: `paths` fires when at least one changed path is
    filtered in; `paths-ignore` fires when at least one changed path is NOT
    ignored; neither fires on any change. An empty diff fires nothing here.
    """
    if not paths or not push_branch_matches(workflow, branch):
        return False
    on = load(workflow)
    on = on.get("on", on.get(True))
    spec = on.get("push") if isinstance(on, dict) else None
    if not isinstance(spec, dict):
        return True
    if "paths" in spec:
        return any(_filtered_in(p, spec.get("paths") or []) for p in paths)
    if "paths-ignore" in spec:
        return any(not _filtered_in(p, spec.get("paths-ignore") or []) for p in paths)
    return True


@functools.lru_cache(maxsize=None)
def _regex(glob: str) -> re.Pattern[str]:
    """One filter pattern as an anchored regex over a repo-relative path.

    Atoms are accumulated in a list rather than concatenated, because `?`
    quantifies the atom BEFORE it and so needs to reach back and wrap one. Build
    a string and the reach-back becomes a substring hunt; keep a list and it is
    an index.

    `**/` consumes its own slash and compiles to an optional segment run, which
    is what lets `**/README.md` match at the root — see the module docstring's
    ZERO-SEGMENT note for the push that proved it.
    """
    out: list[str] = []
    i = 0
    while i < len(glob):
        ch = glob[i]
        if ch == "*":
            if glob[i + 1:i + 2] == "*":
                if glob[i + 2:i + 3] == "/":
                    # `**/` — zero or more WHOLE segments, slash included, so the
                    # pattern still matches with nothing in front of it at all.
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif ch == "?":
            # A quantifier over the preceding atom, per the docs' own `*.jsx?`
            # example. Wrapped in a non-capturing group rather than appended
            # raw: a bare `?` after the `[^/]*` that `*` emits would read as a
            # LAZY quantifier and quietly change what the pattern means. With
            # no atom to quantify there is nothing to make optional, so the `?`
            # can only be meant literally.
            if out:
                out[-1] = f"(?:{out[-1]})?"
            else:
                out.append(re.escape(ch))
        else:
            out.append(re.escape(ch))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, glob: str) -> bool:
    """Would a change to `path` satisfy this one filter pattern?"""
    return bool(_regex(glob).match(path))


def covered(path: str, globs: list[str]) -> str | None:
    """The first glob that watches this exact file, or None."""
    for glob in globs:
        if matches(path, glob):
            return glob
    return None


def covers_tree(directory: str, globs: list[str]) -> str | None:
    """The first glob that watches EVERYTHING under `directory`, or None.

    For a test that globs a directory rather than naming a file, partial
    coverage is no coverage: a narrow pattern like `maintenance/*.md` leaves the
    test free to read `maintenance/sub/thing.json` with nothing watching it. So
    only a whole-subtree pattern counts, and a caller asking about a directory
    gets None where a caller asking about one file inside it would get a hit.

    A `<prefix>/**` pattern covers `directory` when `<prefix>` names `directory`
    itself or any of its ancestors — decided by running `<prefix>` through the
    same matcher everything else uses, rather than by comparing glob text. The
    old string form (`d == glob[:-3] or d.startswith(glob[:-2])`) could not see
    a wildcard in the prefix, so `**/docs/**` failed to cover `docs` and
    `MCPs/*/skills/**` failed to cover `MCPs/x/skills`. Both errors were in the
    safe direction — cover reported as absent — but a checker that under-reports
    coverage sends authors to widen triggers that were already correct.
    """
    d = directory.rstrip("/")
    if not d:
        return None
    # `d` and every ancestor of it: `a/b/c` -> ['a/b/c', 'a/b', 'a'].
    segs = d.split("/")
    lineage = ["/".join(segs[:n]) for n in range(len(segs), 0, -1)]
    for glob in globs:
        if glob == "**":
            return glob
        if not glob.endswith("/**"):
            continue
        prefix = glob[:-3]
        if any(matches(anc, prefix) for anc in lineage):
            return glob
    return None


#: How each wildcard is instantiated when a GLOB has to be tested for coverage
#: rather than a path. Two witnesses each — one that consumes nothing and one
#: that consumes a couple of segments — because the failures worth catching are
#: a pattern that is narrower than the glob at exactly one of those two depths.
_WITNESSES = {
    "**/": ("", "wit/nes/"),
    "**": ("wit", "wit/nes"),
    "*": ("wit",),
    "?": ("w", ""),
}


def witnesses(glob: str, cap: int = 64) -> list[str]:
    """Concrete paths a glob can produce, enough to test another glob against it.

    A declared input like `**/*.md` is a LANGUAGE, not a path, so asking
    `matches("**/*.md", "AGENTS.md")` answers nothing — it compares the glob's
    own characters. Proper containment (is L(G) a subset of L(P)?) is decidable
    for this grammar but fiddly, and the fiddly part buys almost nothing here:
    the failure that actually happens is a workflow pattern that covers the
    glob at one depth and not another (`AGENTS.md` listed against a script that
    reads `**/AGENTS.md`). Instantiating each wildcard shallow AND deep catches
    exactly that, and a caller requiring ALL witnesses to be covered can only
    ever be wrong by demanding too much — which surfaces as a finding a human
    reads, never as a silent pass.

    Capped, because the product is exponential in the wildcard count and a
    20-wildcard pattern is not a thing this repo has.
    """
    out = [""]
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            key, step = "**/", 3
        elif glob.startswith("**", i):
            key, step = "**", 2
        elif glob[i] in ("*", "?"):
            key, step = glob[i], 1
        else:
            out = [o + glob[i] for o in out]
            i += 1
            continue
        out = [o + w for o in out for w in _WITNESSES[key]]
        i += step
        if len(out) > cap:
            out = out[:cap]
    # A witness may collapse to something with a doubled or trailing slash when
    # a zero-width instantiation meets a literal separator; normalise so the
    # result is a path a filter could really see.
    seen: list[str] = []
    for o in out:
        norm = re.sub(r"/{2,}", "/", o).strip("/")
        if norm and norm not in seen:
            seen.append(norm)
    return seen


def glob_covered(glob: str, patterns: list[str]) -> bool:
    """Would EVERY path this glob can name satisfy at least one of `patterns`?

    Approximated by `witnesses` — see there for why the approximation errs only
    toward reporting a finding.
    """
    wits = witnesses(glob)
    if not wits:
        return False
    return all(covered(w, patterns) is not None for w in wits)


def globs_overlap(a: str, b: str) -> bool:
    """Can any one path satisfy BOTH globs?

    The question the REVERSE direction asks. "Is this `paths:` entry a
    dependency of anything?" is not containment either way round — a trigger
    `scripts/**` is justified by a dependency on `scripts/audit-doc-links.py`
    (entry broader) and equally by one on `scripts/**/*.py` (entry narrower).
    What disqualifies an entry is reaching NOTHING the workflow depends on, so
    the test is intersection, not subset.

    Approximated through `witnesses` in both directions, which can only ever
    over-report overlap — and over-reported overlap means an entry is kept, so
    the error falls on the side of leaving a trigger in place rather than
    recommending the deletion of a live one.
    """
    return (any(matches(w, b) for w in witnesses(a))
            or any(matches(w, a) for w in witnesses(b)))


def any_match(paths: list[str], globs: list[str]) -> list[tuple[str, str]]:
    """Every (path, glob) pair where a changed path satisfies a filter."""
    hits = []
    for path in paths:
        glob = covered(path, globs)
        if glob:
            hits.append((path, glob))
    return hits


def main(argv: list[str] | None = None) -> int:
    """CLI: `push-fires <workflow> [--branch main]`, changed paths on stdin.

    Prints `fires` or `quiet` and exits 0; exits 2 when the workflow cannot be
    read. The caller is the `ci-dedup` composite (ORG-PLAN-339 C1): when `main`
    moved between a PR's head and the merge push, the push run may still reuse
    the PR's green run if none of the paths THIS workflow watches moved — so
    the question it asks is exactly "would a push of the intervening diff start
    this workflow?", which is `push_fires`. Stdlib + PyYAML only, because the
    composite runs it under the runner's system python.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="ci_trigger_paths.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("push-fires", help="would a push of stdin's paths start the workflow?")
    pf.add_argument("workflow")
    pf.add_argument("--branch", default="main")
    args = ap.parse_args(argv)

    paths = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
    try:
        fires = push_fires(Path(args.workflow), args.branch, paths)
    except Exception as exc:  # noqa: BLE001 — any unreadable workflow is "cannot say"
        print(f"ci_trigger_paths: cannot read {args.workflow}: {exc}", file=sys.stderr)
        return 2
    print("fires" if fires else "quiet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
