"""The release workflow must not be able to publish a tag it has not gated.

WHY THIS EXISTS
---------------
Consumers pin exact immutable tags through `[tool.uv.sources]`, so a tag IS the
shipped artifact — once a consumer has resolved one, it cannot be walked back.
Every gate therefore has to run while refusing is still possible.

Until 2026-09-16 that was not true of any of them. `release.yml` triggered on
`push: tags`, so `git push --tags` created the artifact and the workflow then
reported on it: the ancestry guard, the quality job and the tag/version check
could all fail onto a tag that was already public and already pinnable.
Measured across the org's nine internal libraries that day, three published tags
disagreed with the `pyproject.toml` at their own commit — this repo's `v0.3.1`
among them, pinned live by `Stromy` — and each also had no GitHub Release,
because the check that failed skipped the step that creates one.

So the tag is now an OUTPUT of the release: the workflow is dispatched against
the default branch, runs every gate, and creates the tag last. These tests pin
that ordering, across four axes that each fail in a way the others cannot see:

  * TRIGGER — the workflow cannot be started BY a tag. If it can, every gate
    below it is once again a report about something that already exists.
  * STRUCTURE — the ancestry gate is its own job and every other job `needs:` it,
    transitively, so no release side effect can run ahead of it. A gate placed
    as a late *step* would let earlier steps publish first.
  * ORDERING — within the publishing job, the irreversible steps (create tag,
    create Release) come after the reversible ones (lock check, build, verify).
  * BEHAVIOUR — the ancestry predicate itself really distinguishes a
    main-reachable commit from a side-branch one, run against a real git
    repository. Asserting that `merge-base --is-ancestor` appears in the file
    proves a grep, not a gate.

These bind to what the workflow DOES, not to what its jobs are called. An
earlier version of this file hard-coded the job name `guard`; renaming that job
while preserving every invariant turned the suite red for no reason, which is a
test measuring the label instead of the property.

`fetch-depth: 0` is load-bearing and asserted: ancestry is unprovable on the
default shallow checkout.
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

# The predicate that makes a job the ancestry gate, whatever that job is named.
ANCESTRY_PREDICATE = "merge-base --is-ancestor"
# The step that first makes the release real and unrecallable.
TAG_PUSH = 'git push origin "$TAG"'


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


def _code(text: str) -> str:
    """`text` with whole-line YAML comments dropped.

    Every match below is a claim that the workflow DOES something, and a comment
    does nothing. Without this, the checkout step's own comment explaining why
    `fetch-depth: 0` matters (it names the ancestry predicate) counts as a second
    site running it.
    """
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


def _job_containing(jobs: dict[str, str], needle: str) -> str:
    """The single job whose body RUNS `needle`. Fails if zero or many do."""
    hits = sorted(name for name, body in jobs.items() if needle in _code(body))
    assert len(hits) == 1, (
        f"expected exactly one job containing {needle!r}, found {hits or 'none'}"
    )
    return hits[0]


def _step_containing(job_body: str, needle: str) -> str:
    """The single `steps:` entry within a job body that contains `needle`.

    Job-level assertions are too coarse for "does THIS check fail closed?": the
    gate job legitimately contains several `exit 1`s (wrong branch, unreadable
    version, version already tagged), so asserting `exit 1 in job_body` stays
    green when the ancestry check alone is downgraded to a warning. Mutation
    testing caught exactly that, which is the same flaw as asserting a property
    the test only appears to pin.
    """
    chunks = re.split(r"\n(?=      - )", job_body)
    hits = [c for c in chunks if needle in _code(c)]
    assert len(hits) == 1, f"expected exactly one step containing {needle!r}, found {len(hits)}"
    return hits[0]


def _needs(jobs: dict[str, str]) -> dict[str, set[str]]:
    return {
        name: set(re.findall(r"[A-Za-z0-9_-]+", body.split("needs:", 1)[1].splitlines()[0]))
        if "needs:" in body
        else set()
        for name, body in jobs.items()
    }


def _gated_by(name: str, gate: str, needs: dict[str, set[str]],
              seen: frozenset[str] = frozenset()) -> bool:
    """Does `name` transitively depend on `gate`?

    Transitive on purpose, so adding a job later cannot quietly open an ungated
    path — a new job either needs the gate or needs something that does.
    """
    if name in seen:
        return False
    return gate in needs[name] or any(
        _gated_by(dep, gate, needs, seen | {name}) for dep in needs[name] if dep in needs
    )


# --- TRIGGER ---------------------------------------------------------------


def test_the_workflow_cannot_be_started_by_a_tag() -> None:
    """A tag trigger would put every gate below back after the artifact exists.

    This is the whole redesign in one assertion. With `on: push: tags`, the tag
    is an input and nothing downstream can refuse it; the workflow may only be
    reachable by a deliberate dispatch.
    """
    text = _release_text()
    on_block = text.split("\non:", 1)[1].split("\npermissions:", 1)[0]
    assert "tags:" not in on_block, (
        "release.yml is triggered by a tag push — every gate in it is then a "
        f"report about an artifact that already exists. `on:` block was:\n{on_block}"
    )
    assert "workflow_dispatch:" in on_block, (
        "release.yml must be reachable by a deliberate dispatch"
    )


# --- STRUCTURE -------------------------------------------------------------


def test_the_ancestry_gate_checks_out_full_history() -> None:
    """A shallow checkout cannot prove ancestry, so the assertion would be meaningless."""
    jobs = _top_level_jobs(_release_text())
    gate = _job_containing(jobs, ANCESTRY_PREDICATE)
    assert "fetch-depth: 0" in jobs[gate], f"the `{gate}` job must check out full history"


def test_the_ancestry_gate_fails_closed() -> None:
    """Scoped to the ancestry STEP, not the job — see `_step_containing`."""
    jobs = _top_level_jobs(_release_text())
    gate = _job_containing(jobs, ANCESTRY_PREDICATE)
    step = _step_containing(jobs[gate], ANCESTRY_PREDICATE)
    assert "exit 1" in step, (
        "a failed ancestry check must fail the job, not warn — the step reads:\n" + step
    )


def test_every_other_job_is_gated_by_the_ancestry_gate() -> None:
    """Ordering is the whole point: nothing may publish before the gate answers."""
    jobs = _top_level_jobs(_release_text())
    gate = _job_containing(jobs, ANCESTRY_PREDICATE)
    needs = _needs(jobs)
    ungated = sorted(n for n in jobs if n != gate and not _gated_by(n, gate, needs))
    assert not ungated, f"job(s) can run before the ancestry gate `{gate}`: {ungated}"


def test_the_tag_is_created_in_a_gated_job() -> None:
    """The tag must be an output of the gates, never something that bypasses them."""
    jobs = _top_level_jobs(_release_text())
    gate = _job_containing(jobs, ANCESTRY_PREDICATE)
    publisher = _job_containing(jobs, TAG_PUSH)
    assert publisher != gate, "the gate job must not also be the job that publishes"
    assert _gated_by(publisher, gate, _needs(jobs)), (
        f"`{publisher}` creates the tag without depending on the gate `{gate}`"
    )


# --- ORDERING --------------------------------------------------------------


def test_the_tag_is_pushed_after_every_reversible_check() -> None:
    """Within the publishing job, nothing recoverable may run after the tag exists.

    Position-in-body is a proxy for step order, which is exactly how Actions runs
    a `steps:` list. The checks named here are the ones whose failure should stop
    a release; each must sit above the push that makes it unrecallable.
    """
    jobs = _top_level_jobs(_release_text())
    publisher = _job_containing(jobs, TAG_PUSH)
    body = jobs[publisher]
    tag_at = body.index(TAG_PUSH)
    for earlier in ("uv lock --check", "uv build", "uv build produced no sdist+wheel pair"):
        assert earlier in body, f"`{publisher}` no longer runs {earlier!r} before tagging"
        assert body.index(earlier) < tag_at, (
            f"{earlier!r} runs AFTER the tag is pushed — it can no longer prevent anything"
        )


def test_the_release_is_created_after_the_tag() -> None:
    jobs = _top_level_jobs(_release_text())
    publisher = _job_containing(jobs, TAG_PUSH)
    body = jobs[publisher]
    assert body.index(TAG_PUSH) < body.index("gh release create"), (
        "the GitHub Release must be created after the tag it names"
    )


def test_a_version_already_tagged_is_refused() -> None:
    """The one mistake a human can still make: dispatching without bumping."""
    text = _release_text()
    assert "git ls-remote --exit-code --tags origin" in text, (
        "release.yml must refuse a version whose tag already exists"
    )


# --- BEHAVIOUR -------------------------------------------------------------


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
    assert rc == 0, "a commit on main must pass the gate"


def test_predicate_rejects_a_side_branch_tag(repo_with_side_branch: tuple[Path, str, str]) -> None:
    clone, _on_main, on_side = repo_with_side_branch
    rc = subprocess.run(  # noqa: S603
        [GIT, "merge-base", "--is-ancestor", on_side, "origin/main"], cwd=clone
    ).returncode
    assert rc != 0, "a commit that never reached main must fail the gate"
