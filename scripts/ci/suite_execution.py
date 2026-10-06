"""Enforce adopted suite floors in the canonical runner, without metadata commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from xml.etree.ElementTree import ParseError

from execution_contract import execution_verdict, junit_counts, load_contract

CI_INPUTS = (".github/test-contract.yml", "scripts/ci/test-contract.schema.json")
CI_DELEGATES_TO = ("scripts/ci/execution_contract.py", "scripts/ci/native_workflow.py")
PORTABLE_FILES = (
    "scripts/ci/suite_execution.py",
    "scripts/ci/execution_contract.py",
    "scripts/ci/test-contract.schema.json",
    "scripts/ci_trigger_paths.py",
)


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


def evaluate(repo: Path, step: str, report: Path, returncode: int) -> dict:
    contract = load_contract(repo)
    if contract is None:
        raise ValueError("suite execution requires an adopted contract")
    suites = [
        s
        for s in contract["suites"]
        if s["surface"] in {"pr", "local"} and s.get("execution_step", "tests") == step
    ]
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
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--step", choices=["tests", "contract", "browser", "node"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--returncode", type=int)
    args = parser.parse_args()
    try:
        if args.emit_template or args.check_template:
            return template_copy(
                args.emit_template or args.check_template,
                check=bool(args.check_template),
            )
        if args.step is None or args.report is None or args.returncode is None:
            parser.error("execution requires --step, --report and --returncode")
        result = evaluate(args.repo, args.step, args.report, args.returncode)
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        print(result["status"])
        return int(result["status"] == "fail")
    except (OSError, ValueError, ImportError, ParseError) as exc:
        print(f"required suite evidence unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
