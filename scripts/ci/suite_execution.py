"""Enforce adopted suite floors in the canonical runner, without metadata commands."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from xml.etree.ElementTree import ParseError

from execution_contract import execution_verdict, junit_counts, load_contract

CI_DELEGATES_TO = ("scripts/ci/execution_contract.py", "scripts/ci/native_workflow.py")
PORTABLE_FILES = (
    "scripts/ci/suite_execution.py",
    "scripts/ci/suite_evidence.py",
    "scripts/ci/native_producer.py",
    "scripts/ci/workflow_identity.py",
    "scripts/ci/execution_contract.py",
    "scripts/ci/test-contract.schema.json",
    "scripts/ci_trigger_paths.py",
    "scripts/workflow_yaml.py",
)
CI_INPUTS = (
    ".github/test-contract.yml",
    ".github/owning-suite-bundle.json",
    "scripts/ci/suite_execution.py",
    "scripts/ci/suite_evidence.py",
    "scripts/ci/native_producer.py",
    "scripts/ci/workflow_identity.py",
    "scripts/ci/execution_contract.py",
    "scripts/ci/test-contract.schema.json",
    "scripts/ci_trigger_paths.py",
    "scripts/workflow_yaml.py",
)
NATIVE_MANIFEST = '.github/owning-suite-bundle.json'
NATIVE_BUNDLE_VERSION = 1


def canonical_files(root, sha):
    """Export committed owner bytes only after they are published on main."""
    def git(*args):
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                                check=False, timeout=10)
        if result.returncode:
            raise ValueError('canonical owning-suite source unavailable')
        return result.stdout
    if (not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{40}', sha)
            or not re.fullmatch(rb'(?:https://github\.com/|git@github\.com:)stromy-org/stromy-org(?:\.git)?\s*',
                                git('config', '--get', 'remote.origin.url'))):
        raise ValueError('canonical owning-suite source identity unavailable')
    git('merge-base', '--is-ancestor', sha, 'origin/main')
    files = {name: git('show', sha + ':' + name) for name in PORTABLE_FILES}
    if any(not body or len(body) > 512_000 for body in files.values()):
        raise ValueError('canonical owning-suite file unavailable or over bound')
    module = ast.parse(files['scripts/ci/suite_execution.py'])
    if not any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value == NATIVE_BUNDLE_VERSION
               and any(isinstance(target, ast.Name) and target.id == 'NATIVE_BUNDLE_VERSION'
                       for target in node.targets) for node in module.body):
        raise ValueError('canonical source predates the owning-suite bundle protocol')
    return files


def native_bundle(repo, *, emit=False, source_sha=None, root=None):
    """One canonical export for consumer-owned CI, plus a portable drift seal."""
    root = root or Path(__file__).resolve().parents[2]
    repo = repo.resolve()
    path = repo / NATIVE_MANIFEST
    if path.is_symlink() or not path.resolve().is_relative_to(repo):
        raise ValueError('owning-suite source seal escapes consumer tree')
    if emit:
        files = canonical_files(root, source_sha)
        if path.exists():
            native_bundle(repo)  # Never overwrite locally modified vendor files.
        elif any((repo / name).exists() for name in PORTABLE_FILES):
            raise ValueError('existing owning-suite files have no verifiable source seal')
        value = {'schema_version': 1, 'source_repo': 'stromy-org/stromy-org', 'source_sha': source_sha,
                 'files': {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}}
    else:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32_000:
            raise ValueError('owning-suite source seal unavailable')
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'source_repo', 'source_sha', 'files'}
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['source_repo'] != 'stromy-org/stromy-org'
                or not re.fullmatch(r'[a-f0-9]{40}', str(value['source_sha']))
                or not isinstance(value['files'], dict) or set(value['files']) != set(PORTABLE_FILES)):
            raise ValueError('owning-suite source seal identity differs')
        files = canonical_files(root, source_sha) if source_sha is not None else None
        if source_sha is not None and source_sha != value['source_sha']:
            raise ValueError('owning-suite source differs from requested canonical revision')
    for name in PORTABLE_FILES:
        destination = repo / name
        if destination.is_symlink() or not destination.resolve().is_relative_to(repo):
            raise ValueError('owning-suite file escapes consumer tree')
        if not emit and (not destination.is_file() or destination.stat().st_size > 512_000
                    or hashlib.sha256(destination.read_bytes()).hexdigest() != value['files'][name]
                    or files is not None and destination.read_bytes() != files[name]):
            raise ValueError('owning-suite file differs from its source seal')
    if emit:
        assert files is not None
        for name, body in files.items():
            destination = repo / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, indent=2) + '\n')
    return value


def template_copy(template: Path, *, check: bool) -> int:
    """Copier chrome comes from this owner, byte for byte, never a policy fork."""
    source = Path(__file__).resolve().parents[2]
    if not (template / "copier.yml").is_file() or not (template / "template").is_dir():
        raise ValueError("expected an existing Copier template")
    drift = []
    files = [(name, template / "template" / name) for name in PORTABLE_FILES]
    if template.name in {"python-library-template", "workflow-service-template"}:
        files.append(
            ("scripts/ci/native_workflow.py", template / ".github/check-rendered-ci.py")
        )
    for name, destination in files:
        expected = (source / name).read_bytes()
        if not destination.is_file() or destination.read_bytes() != expected:
            drift.append(name)
            if not check:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(expected)
    print(json.dumps({"template": str(template), "drift": drift, "check": check}))
    return int(check and bool(drift))


def evaluate(repo: Path, step: str, report: Path, returncode: int,
             *, surface: str | None = None, suite_id: str | None = None) -> dict:
    contract = load_contract(repo)
    if contract is None:
        raise ValueError("suite execution requires an adopted contract")
    suites = [
        s
        for s in contract["suites"]
        if s["surface"] in ({surface} if surface else {"pr", "local"})
        and s.get("execution_step", "tests") == step
        and (suite_id is None or s["id"] == suite_id)
    ]
    if suite_id is not None and not suites:
        raise ValueError("selected suite is absent from the adopted execution surface")
    empty = {"collected": 0, "executed": 0, "skipped": 0, "failed": 0}
    counts = (
        junit_counts(report, suites, repo=repo)
        if report.is_file()
        else {s["id"]: dict(empty) for s in suites}
    )
    verdicts = {s["id"]: execution_verdict(s, counts[s["id"]]) for s in suites}
    passed = returncode in {0, 5} and all(
        v in {"pass", "not-applicable"} for v in verdicts.values()
    )
    status = (
        "fail"
        if not passed
        else (
            "skip-not-applicable"
            if all(v == "not-applicable" for v in verdicts.values())
            else "pass"
        )
    )
    return {
        "step": step,
        "status": status,
        "suite_counts": counts,
        "suite_verdicts": verdicts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    copies = parser.add_mutually_exclusive_group()
    copies.add_argument("--emit-template", type=Path)
    copies.add_argument("--check-template", type=Path)
    copies.add_argument('--emit-native', type=Path)
    copies.add_argument('--check-native', type=Path)
    parser.add_argument('--source-sha', help='published canonical source commit for an owning bundle')
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--step", choices=["tests", "contract", "browser", "node"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--returncode", type=int)
    parser.add_argument("--surface", choices=["pr", "local", "scheduled"])
    parser.add_argument("--suite")
    args = parser.parse_args()
    try:
        if args.emit_native or args.check_native:
            value = native_bundle(args.emit_native or args.check_native, emit=bool(args.emit_native),
                                  source_sha=args.source_sha)
            print(json.dumps({'source_sha': value['source_sha'], 'files': len(value['files']), 'status': 'pass'}))
            return 0
        if args.emit_template or args.check_template:
            return template_copy(
                args.emit_template or args.check_template,
                check=bool(args.check_template),
            )
        if args.step is None or args.report is None or args.returncode is None:
            parser.error("execution requires --step, --report and --returncode")
        result = evaluate(args.repo, args.step, args.report, args.returncode,
                          surface=args.surface, suite_id=args.suite)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        print(result["status"])
        return int(result["status"] == "fail")
    except (OSError, ValueError, ImportError, ParseError) as exc:
        print(f"required suite evidence unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
