"""Seed-once files never take an update, so a repo scaffolded before its suite
contract existed never receives it. post-copier-update.sh renders ONLY an absent
required seed from the updated template with the repo's answers; a present file
is never touched. `uvx` is stubbed: it records its arguments and writes a fake
render, so this runs without network.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "post-copier-update.sh"
STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$UVX_LOG"
for a in "$@"; do last=$a; done
mkdir -p "$last/.github"
echo rendered-contract > "$last/.github/test-contract.yml"
"""
ANSWERS = "# managed\n_commit: v9.9.9\n_src_path: gh:example/tpl\nproject_name: x\n"


def repo(tmp_path: Path, answers: str) -> dict[str, str]:
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / "bin").mkdir()
    # Only the seed step is exercised: render-agent-md and uv lock are absent.
    shutil.copy(SCRIPT, tmp_path / "scripts" / "post-copier-update.sh")
    (tmp_path / ".copier-answers.yml").write_text(answers)
    stub = tmp_path / "bin" / "uvx"
    stub.write_text(STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "UVX_LOG": str(tmp_path / "uvx.log")}


def run(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run([bash, "scripts/post-copier-update.sh"], cwd=tmp_path, env=env,  # noqa: S603
                          capture_output=True, text=True, timeout=60, check=False)


def test_renders_an_absent_suite_contract(tmp_path: Path) -> None:
    env = repo(tmp_path, ANSWERS)
    result = run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".github" / "test-contract.yml").read_text() == "rendered-contract\n"
    args = (tmp_path / "uvx.log").read_text().splitlines()
    assert {"v9.9.9", "gh:example/tpl", "--skip-tasks"} <= set(args)


def test_never_renders_when_the_contract_is_present(tmp_path: Path) -> None:
    env = repo(tmp_path, ANSWERS)
    (tmp_path / ".github" / "test-contract.yml").write_text("owned\n")
    result = run(tmp_path, env)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "uvx.log").exists()
    assert (tmp_path / ".github" / "test-contract.yml").read_text() == "owned\n"


def test_unknown_template_version_fails_visibly(tmp_path: Path) -> None:
    env = repo(tmp_path, "project_name: x\n")
    result = run(tmp_path, env)
    assert result.returncode == 1
    assert "template source/version unknown" in result.stderr
    assert not (tmp_path / "uvx.log").exists()
