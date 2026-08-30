"""The release workflow must refuse a tag that is not reachable from main (ORG-PLAN-209).

WHY THIS EXISTS
---------------
`Verify tag matches pyproject version` proves the tag is *self-consistent*. It says
nothing about whether the commit it points at was ever reviewed or CI-gated: a
`git tag vX.Y.Z` on a side branch whose pyproject carries the same bump passes it and
publishes. Consumers pin exact immutable tags through `[tool.uv.sources]`, so a tag is
the shipped artifact — once a consumer has resolved it, it cannot be walked back.

Two axes, deliberately separate, because each fails in a way the other cannot see:

  * STRUCTURE — the guard is its own job and every other job `needs:` it, so no release
    side effect (build, GitHub Release, consumer-bump dispatch) can run ahead of it.
    A guard placed as a late *step* would let earlier steps publish first.
  * BEHAVIOUR — the predicate itself actually distinguishes a main-reachable commit
    from a side-branch one, run against a real git repository. Asserting only that the
    string `merge-base --is-ancestor` appears in the file proves a grep, not a gate.

`fetch-depth: 0` is load-bearing and asserted: ancestry is unprovable on the default
shallow checkout.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Resolved once so the subprocess calls below use an absolute executable path
# (ruff S607) rather than relying on whatever PATH the runner happens to carry.
GIT = shutil.which("git") or "/usr/bin/git"

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
GUARD_JOB = "guard"


def _release_text() -> str:
    assert RELEASE_YML.is_file(), f"{RELEASE_YML} is missing"
    return RELEASE_YML.read_text(encoding="utf-8")


def _top_level_jobs(text: str) -> dict[str, str]:
    """Split the `jobs:` mapping into {job-name: body}.

    Hand-rolled rather than via PyYAML so this test adds no dependency to a library
    whose dev group is deliberately small. Job keys are the only 2-space-indented
    `name:`-shaped lines inside the `jobs:` block, which is unambiguous here.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    except StopIteration:  # pragma: no cover - a release.yml with no jobs is a hard fail
        pytest.fail("release.yml declares no `jobs:` block")

    jobs: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        if line.strip() and not line.startswith(" "):
            break  # left the jobs mapping
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            jobs[current] = []
        elif current is not None:
            jobs[current].append(line)
    return {name: "\n".join(body) for name, body in jobs.items()}


def test_guard_job_exists() -> None:
    jobs = _top_level_jobs(_release_text())
    assert GUARD_JOB in jobs, f"release.yml has no `{GUARD_JOB}` job; jobs are {sorted(jobs)}"


def test_guard_checks_out_full_history() -> None:
    """A shallow checkout cannot prove ancestry, so the assertion would be meaningless."""
    body = _top_level_jobs(_release_text())[GUARD_JOB]
    assert "fetch-depth: 0" in body, "the guard job must check out full history"


def test_guard_runs_the_ancestry_predicate_and_fails_closed() -> None:
    body = _top_level_jobs(_release_text())[GUARD_JOB]
    assert 'git merge-base --is-ancestor "$GITHUB_SHA" origin/main' in body
    assert "exit 1" in body, "a failed ancestry check must fail the job, not warn"


def test_every_other_job_is_gated_by_the_guard() -> None:
    """Ordering is the whole point: nothing may publish before the guard answers.

    Written transitively so that adding a job later cannot quietly open an ungated
    path — a new job either needs the guard or needs something that does.
    """
    jobs = _top_level_jobs(_release_text())
    needs = {
        name: set(re.findall(r"[A-Za-z0-9_-]+", body.split("needs:", 1)[1].splitlines()[0]))
        if "needs:" in body
        else set()
        for name, body in jobs.items()
    }

    def gated(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen:
            return False
        return GUARD_JOB in needs[name] or any(
            gated(dep, seen | {name}) for dep in needs[name] if dep in needs
        )

    ungated = sorted(n for n in jobs if n != GUARD_JOB and not gated(n))
    assert not ungated, f"job(s) can run before the ancestry guard: {ungated}"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo_with_side_branch(tmp_path: Path) -> tuple[Path, str, str]:
    """A real repo whose `origin/main` holds one commit and a side branch another."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "test")
    (upstream / "f.txt").write_text("one\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-qm", "on main")
    on_main = _git(upstream, "rev-parse", "HEAD")

    _git(upstream, "checkout", "-q", "-b", "side")
    (upstream / "f.txt").write_text("two\n")
    _git(upstream, "commit", "-qam", "on a side branch")
    on_side = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "-q", "main")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(upstream), str(clone))
    _git(clone, "fetch", "-q", "origin", "side")
    return clone, on_main, on_side


def test_predicate_accepts_a_main_reachable_tag(repo_with_side_branch: tuple[Path, str, str]) -> None:
    clone, on_main, _on_side = repo_with_side_branch
    rc = subprocess.run(  # noqa: S603
        [GIT, "merge-base", "--is-ancestor", on_main, "origin/main"], cwd=clone
    ).returncode
    assert rc == 0, "a commit on main must pass the guard"


def test_predicate_rejects_a_side_branch_tag(repo_with_side_branch: tuple[Path, str, str]) -> None:
    clone, _on_main, on_side = repo_with_side_branch
    rc = subprocess.run(  # noqa: S603
        [GIT, "merge-base", "--is-ancestor", on_side, "origin/main"], cwd=clone
    ).returncode
    assert rc != 0, "a commit that never reached main must fail the guard"
