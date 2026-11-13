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
NATIVE_READER = 'scripts/ci/native_suite.py'
INPUT_REUSE_FILES = ('scripts/ci/input_fingerprint.py', 'scripts/ci/dedup_evidence.py',
                     '.github/actions/ci-dedup/action.yml')
# Binds a `--emit-units` bundle to unit records; it imports input_fingerprint,
# so it only ships on top of the input-reuse profile (ORG-PLAN-505 C2).
UNIT_REUSE_FILES = ('scripts/ci/reuse.py',)
LEGACY_NATIVE_MANIFEST = 'scripts/ci/native-suite-bundle.json'
LEGACY_NATIVE_FILES = ('native_suite.py', 'suite_evidence.py', 'workflow_identity.py',
                       'execution_contract.py', 'test-contract.schema.json')
LEGACY_FLOOR_FILES = ('scripts/ci/suite_execution.py', 'scripts/ci/execution_contract.py',
                      'scripts/ci/test-contract.schema.json', 'scripts/ci_trigger_paths.py')
# The static input owner accepts literals only; a starred constant makes the
# declaration unknown and conservatively binds unrelated control-plane records.
CI_INPUTS = (
    ".github/test-contract.yml",
    ".github/owning-suite-bundle.json",
    "scripts/ci/suite_execution.py",
    "scripts/ci/suite_evidence.py",
    "scripts/ci/native_producer.py",
    "scripts/ci/native_suite.py",
    "scripts/ci/input_fingerprint.py",
    "scripts/ci/dedup_evidence.py",
    ".github/actions/ci-dedup/action.yml",
    "scripts/ci/reuse.py",
    "scripts/ci/workflow_identity.py",
    "scripts/ci/execution_contract.py",
    "scripts/ci/test-contract.schema.json",
    "scripts/ci_trigger_paths.py",
    "scripts/workflow_yaml.py",
)
NATIVE_MANIFEST = '.github/owning-suite-bundle.json'
NATIVE_BUNDLE_VERSION = 1
NATIVE_READER_BUNDLE_VERSION = 1
INPUT_REUSE_BUNDLE_VERSION = 1
UNIT_REUSE_BUNDLE_VERSION = 1


def canonical_git(root, sha):
    """One source identity and main-ancestry check for immutable owner reads."""
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
    return git


def native_names(*, include_reader=False, include_input_reuse=False, include_unit_reuse=False):
    if include_unit_reuse and not include_input_reuse:
        raise ValueError('unit reuse requires the input-reuse profile it imports')
    return (*PORTABLE_FILES, *((NATIVE_READER,) if include_reader else ()),
            *(INPUT_REUSE_FILES if include_input_reuse else ()),
            *(UNIT_REUSE_FILES if include_unit_reuse else ()))


def seal_profiles():
    """Every file set a valid owning-suite seal may name."""
    return [set(native_names(include_reader=reader, include_input_reuse=reuse, include_unit_reuse=units))
            for reader in (False, True) for reuse in (False, True) for units in (False, True)
            if reuse or not units]


def canonical_files(root, sha, *, include_reader=False, include_input_reuse=False,
                    include_unit_reuse=False):
    """Export committed owner bytes only after they are published on main."""
    git = canonical_git(root, sha)
    names = native_names(include_reader=include_reader, include_input_reuse=include_input_reuse,
                         include_unit_reuse=include_unit_reuse)
    files = {name: git('show', sha + ':' + name) for name in names}
    if any(not body or len(body) > 512_000 for body in files.values()):
        raise ValueError('canonical owning-suite file unavailable or over bound')
    module = ast.parse(files['scripts/ci/suite_execution.py'])
    if not any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value == NATIVE_BUNDLE_VERSION
               and any(isinstance(target, ast.Name) and target.id == 'NATIVE_BUNDLE_VERSION'
                       for target in node.targets) for node in module.body):
        raise ValueError('canonical source predates the owning-suite bundle protocol')
    if include_reader and not any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value == NATIVE_READER_BUNDLE_VERSION
               and any(isinstance(target, ast.Name) and target.id == 'NATIVE_READER_BUNDLE_VERSION'
                       for target in node.targets) for node in module.body):
        raise ValueError('canonical source predates the native-reader bundle protocol')
    if include_input_reuse and not any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value == INPUT_REUSE_BUNDLE_VERSION
               and any(isinstance(target, ast.Name) and target.id == 'INPUT_REUSE_BUNDLE_VERSION'
                       for target in node.targets) for node in module.body):
        raise ValueError('canonical source predates the input-reuse bundle protocol')
    if include_unit_reuse and not any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
               and node.value.value == UNIT_REUSE_BUNDLE_VERSION
               and any(isinstance(target, ast.Name) and target.id == 'UNIT_REUSE_BUNDLE_VERSION'
                       for target in node.targets) for node in module.body):
        raise ValueError('canonical source predates the unit-reuse bundle protocol')
    return files


def legacy_native_bundle(repo, root, names, *, source_sha=None):
    """Adopt the old deploy reader only after proving all overwritten bytes.

    Its five-file seal did not cover the older template's floor helpers.
    Existing additional destinations must independently equal that exact
    published owner revision too; unsealed edits are never overwritten.
    """
    path = repo / LEGACY_NATIVE_MANIFEST
    if (path.is_symlink() or not path.resolve().is_relative_to(repo)
            or not path.is_file() or path.stat().st_size > 32_000):
        raise ValueError('legacy native reader seal unavailable')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict)
            or set(value) != {'schema_version', 'source_repository', 'source_sha', 'files'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['source_repository'] != 'stromy-org/stromy-org'
            or not re.fullmatch(r'[a-f0-9]{40}', str(value['source_sha']))
            or not isinstance(value['files'], dict) or set(value['files']) != set(LEGACY_NATIVE_FILES)):
        raise ValueError('legacy native reader source identity differs')
    # A pre-protocol seal may name a squash-merged branch commit. A caller
    # may name the published main snapshot only as an independent byte proof;
    # every overwritten file and every old seal digest must still match it.
    proof = source_sha or value['source_sha']
    git = canonical_git(root, proof)
    required = {'scripts/ci/' + name for name in LEGACY_NATIVE_FILES}
    for name in names:
        destination = repo / name
        if not destination.exists() and not destination.is_symlink() and name not in required:
            continue
        if (destination.is_symlink() or not destination.resolve().is_relative_to(repo)
                or not destination.is_file() or destination.stat().st_size > 512_000):
            raise ValueError('legacy native helper missing or outside consumer tree')
        body = destination.read_bytes()
        if body != git('show', proof + ':' + name):
            raise ValueError('existing helper differs from its published legacy source')
        if name in required and hashlib.sha256(body).hexdigest() != value['files'][Path(name).name]:
            raise ValueError('legacy native helper differs from its source seal')
    return path


def legacy_floor_bundle(repo, root, names, source_sha):
    """Prove the complete old four-file floor profile before first adoption."""
    git = canonical_git(root, source_sha)
    module = ast.parse(git('show', source_sha + ':scripts/ci/suite_execution.py'))
    declarations = [ast.literal_eval(node.value) for node in module.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == 'PORTABLE_FILES'
                        for target in node.targets)]
    if declarations != [LEGACY_FLOOR_FILES]:
        raise ValueError('published legacy floor profile differs')
    for name in set(names) | set(LEGACY_FLOOR_FILES):
        path = repo / name
        if not path.exists() and not path.is_symlink() and name not in LEGACY_FLOOR_FILES:
            continue
        if (name not in LEGACY_FLOOR_FILES or path.is_symlink()
                or not path.resolve().is_relative_to(repo) or not path.is_file()
                or path.stat().st_size > 512_000):
            raise ValueError('legacy floor helper missing, escaping or outside its profile')
        if path.read_bytes() != git('show', source_sha + ':' + name):
            raise ValueError('legacy floor helper differs from its published source')


def native_bundle(repo, *, emit=False, source_sha=None, root=None, include_reader=False,
                  legacy_source_sha=None, include_input_reuse=False, include_unit_reuse=False):
    """One canonical export for consumer-owned CI, plus a portable drift seal."""
    root = root or Path(__file__).resolve().parents[2]
    repo = repo.resolve()
    path = repo / NATIVE_MANIFEST
    if path.is_symlink() or not path.resolve().is_relative_to(repo):
        raise ValueError('owning-suite source seal escapes consumer tree')
    legacy = None
    if legacy_source_sha is not None and (not emit or not include_reader or path.exists()):
        raise ValueError('legacy source binding requires a first native-reader adoption')
    if emit:
        if path.exists():
            previous = native_bundle(repo)  # Never overwrite locally modified vendor files.
            include_reader |= NATIVE_READER in previous['files']
            include_input_reuse |= all(name in previous['files'] for name in INPUT_REUSE_FILES)
            include_unit_reuse |= all(name in previous['files'] for name in UNIT_REUSE_FILES)
            additions = set(native_names(include_reader=include_reader, include_input_reuse=include_input_reuse,
                                         include_unit_reuse=include_unit_reuse)) - set(previous['files'])
            for name in additions:
                if (repo / name).exists() or (repo / name).is_symlink():
                    git = canonical_git(root, previous['source_sha'])
                    if (repo / name).is_symlink() or (repo / name).read_bytes() != git(
                            'show', previous['source_sha'] + ':' + name):
                        raise ValueError('unsealed native helper differs from the previous canonical source')
        files = canonical_files(root, source_sha, include_reader=include_reader,
                                include_input_reuse=include_input_reuse,
                                include_unit_reuse=include_unit_reuse)
        old_manifest = repo / LEGACY_NATIVE_MANIFEST
        if not path.exists() and include_reader and (old_manifest.exists() or old_manifest.is_symlink()):
            legacy = legacy_native_bundle(repo, root, files, source_sha=legacy_source_sha)
        elif legacy_source_sha is not None:
            legacy_floor_bundle(repo, root, files, legacy_source_sha)
        elif not path.exists() and any((repo / name).exists() for name in files):
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
                or not isinstance(value['files'], dict) or set(value['files']) not in seal_profiles()):
            raise ValueError('owning-suite source seal identity differs')
        if include_reader and NATIVE_READER not in value['files']:
            raise ValueError('owning-suite seal has no native reader')
        if include_input_reuse and not all(name in value['files'] for name in INPUT_REUSE_FILES):
            raise ValueError('owning-suite seal has no input reuse helpers')
        if include_unit_reuse and not all(name in value['files'] for name in UNIT_REUSE_FILES):
            raise ValueError('owning-suite seal has no unit reuse helper')
        files = canonical_files(root, source_sha, include_reader=NATIVE_READER in value['files'],
                                include_input_reuse=all(name in value['files'] for name in INPUT_REUSE_FILES),
                                include_unit_reuse=all(name in value['files'] for name in UNIT_REUSE_FILES)
                                ) if source_sha is not None else None
        if source_sha is not None and source_sha != value['source_sha']:
            raise ValueError('owning-suite source differs from requested canonical revision')
    for name in value['files']:
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
        if legacy is not None:
            legacy.unlink()
    return value


def template_copy(template: Path, *, check: bool) -> int:
    """Copier chrome comes from this owner, byte for byte, never a policy fork."""
    source = Path(__file__).resolve().parents[2]
    if not (template / "copier.yml").is_file() or not (template / "template").is_dir():
        raise ValueError("expected an existing Copier template")
    drift = []
    files = [(name, template / "template" / name) for name in PORTABLE_FILES]
    if template.name in {"python-library-template", "workflow-service-template"}:
        files.extend([
            ("scripts/ci/native_workflow.py", template / ".github/check-rendered-ci.py"),
            ("scripts/ci/native_services.py", template / ".github/native_services.py"),
        ])
    for name, destination in files:
        expected = (source / name).read_bytes()
        if not destination.is_file() or destination.read_bytes() != expected:
            drift.append(name)
            if not check:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(expected)
    print(json.dumps({"template": str(template), "drift": drift, "check": check}))
    return int(check and bool(drift))


def selected_suites(contract: dict, step: str, *, surface: str | None = None,
                    suite_id: str | None = None) -> list[dict]:
    suites = [
        s
        for s in contract["suites"]
        if s["surface"] in ({surface} if surface else {"pr", "local"})
        and s.get("execution_step", "tests") == step
        and (suite_id is None or s["id"] == suite_id)
    ]
    if suite_id is not None and not suites:
        raise ValueError("selected suite is absent from the adopted execution surface")
    return suites


def evaluate(repo: Path, step: str, report: Path, returncode: int,
             *, surface: str | None = None, suite_id: str | None = None) -> dict:
    contract = load_contract(repo)
    if contract is None:
        raise ValueError("suite execution requires an adopted contract")
    suites = selected_suites(contract, step, surface=surface, suite_id=suite_id)
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


# --- Resumable unit facts (ORG-PLAN-505 C2) ---------------------------------
#
# A suite that declares `units: {kind: pytest-file}` gets one outcome per test
# file of its CURRENT tracked inventory, read from the same JUnit report the
# floor is enforced on. This is a normalized execution fact only: it binds no
# input snapshot and grants no reuse. `input_fingerprint.unit_record` and
# `receipts.resume_plan` / `verify_aggregate` add equivalence. An inventory that
# cannot be read, a case the inventory does not own, or a unit claimed by two
# suites makes the bundle incomplete, which carries no aggregate credit.

UNIT_BUNDLE_SCHEMA = "ci-unit-bundle/1"
#: Unit discovery per declared kind. A shell harness is one standalone script;
#: its JUnit cases name it by repository path in `file` (or `classname`).
UNIT_KINDS = {
    "pytest-file": re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$"),
    "shell-harness": re.compile(r"(?:^|/)test[-_][^/]*\.(?:sh|bats)$"),
}


def tracked_files(repo: Path) -> list[str]:
    """The checkout's tracked files; untracked scratch never joins an inventory."""
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"],
                            capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError("tracked file inventory unavailable")
    return sorted(p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p)


def unit_bundle(repo: Path, suites: list[dict], report: Path, *, step: str, status: str,
                files: list[str] | None = None) -> dict:
    """Per-unit outcomes for the unit-declaring `suites` in one execution report."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ci_trigger_paths
    from suite_evidence import unit_outcomes

    declaring = [s for s in suites if s.get("units")]
    inventory = {"complete": bool(declaring), "suites": {}, "units": {}}
    bundle = {"schema": UNIT_BUNDLE_SCHEMA, "step": step, "status": status,
              "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest() if report.is_file() else None,
              "reason": "" if declaring else "no selected suite declares units",
              "suites": {}, "inventory": inventory}
    try:
        if not declaring:
            raise ValueError(bundle["reason"])
        listing = tracked_files(repo) if files is None else sorted(files)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        listing, bundle["reason"] = None, str(exc)
        inventory["complete"] = False
    for suite in declaring:
        row = {"kind": suite["units"]["kind"],
               "hermeticity": suite["units"].get("hermeticity", "unknown"),
               # None = closure undeclared: reuse.py binds every input and the unit never carries.
               "reads": suite["units"].get("reads"),
               "complete": False, "reason": "", "units": {}}
        bundle["suites"][suite["id"]] = row
        inventory["suites"][suite["id"]] = {"minimum_executed": suite["minimum_executed"]}
        try:
            if listing is None:
                raise ValueError("tracked file inventory unavailable")
            kind = suite["units"]["kind"]
            if kind not in UNIT_KINDS:
                raise ValueError(f"unknown unit kind {kind!r}")
            units = [p for p in listing if UNIT_KINDS[kind].search(p)
                     and any(ci_trigger_paths.matches(p, g) for g in suite["paths"])]
            if not units:
                raise ValueError(f"no {kind} unit matches the suite paths")
            shared = sorted(set(units) & set(inventory["units"]))
            if shared:
                raise ValueError(f"unit claimed by two suites: {shared[0]}")
            if not report.is_file():
                raise ValueError("execution report unavailable")
            row["units"] = unit_outcomes(report, units, scope=suite["paths"], kind=kind)
        except (OSError, ValueError, ParseError) as exc:
            row["reason"] = str(exc)
            inventory["complete"] = False
            continue
        row["complete"] = True
        for unit in row["units"]:
            inventory["units"][unit] = {"suite": suite["id"], "applicable": True,
                                        "prerequisites": [], "cases": None}
    return bundle


def emit_units(args, status):
    """Write the unit bundle when a selected suite declares units; never the verdict.

    No declaring suite writes nothing, so a consumer that has not opted in
    uploads no artifact. An emission failure is reported and leaves the floor
    verdict alone: the missing bundle is incomplete evidence, never a pass.
    """
    try:
        suites = selected_suites(load_contract(args.repo), args.step,
                                 surface=args.surface, suite_id=args.suite)
        if not any(s.get("units") for s in suites):
            return
        bundle = unit_bundle(args.repo, suites, args.report, step=args.step, status=status)
        args.emit_units.parent.mkdir(parents=True, exist_ok=True)
        args.emit_units.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, ImportError, ParseError) as exc:
        print(f"unit bundle not emitted: {exc}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    copies = parser.add_mutually_exclusive_group()
    copies.add_argument("--emit-template", type=Path)
    copies.add_argument("--check-template", type=Path)
    copies.add_argument('--emit-native', type=Path)
    copies.add_argument('--check-native', type=Path)
    parser.add_argument('--source-sha', help='published canonical source commit for an owning bundle')
    parser.add_argument('--include-native-reader', action='store_true',
                        help='include the canonical native evidence reader for deploy guards')
    parser.add_argument('--include-input-reuse', action='store_true',
                        help='include canonical portable input fingerprints and the reuse action')
    parser.add_argument('--include-unit-reuse', action='store_true',
                        help='also include the unit bundle binder (scripts/ci/reuse.py); needs input reuse')
    parser.add_argument('--legacy-source-sha',
                        help='published main snapshot proving the legacy reader or four-file floor profile')
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--step", choices=["tests", "contract", "browser", "node"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--returncode", type=int)
    parser.add_argument("--surface", choices=["pr", "local", "scheduled"])
    parser.add_argument("--suite")
    parser.add_argument("--emit-units", type=Path,
                        help="write per-unit outcomes of unit-declaring suites (never changes the verdict)")
    args = parser.parse_args()
    try:
        if args.emit_native or args.check_native:
            value = native_bundle(args.emit_native or args.check_native, emit=bool(args.emit_native),
                                  source_sha=args.source_sha, include_reader=args.include_native_reader,
                                  legacy_source_sha=args.legacy_source_sha,
                                  include_input_reuse=args.include_input_reuse,
                                  include_unit_reuse=args.include_unit_reuse)
            print(json.dumps({'source_sha': value['source_sha'], 'files': len(value['files']), 'status': 'pass'}))
            return 0
        if (args.include_native_reader or args.legacy_source_sha or args.include_input_reuse
                or args.include_unit_reuse):
            parser.error('reader options require a native bundle operation')
        if args.emit_template or args.check_template:
            return template_copy(
                args.emit_template or args.check_template,
                check=bool(args.check_template),
            )
        if args.step is None or args.report is None or args.returncode is None:
            parser.error("execution requires --step, --report and --returncode")
        result = evaluate(args.repo, args.step, args.report, args.returncode,
                          surface=args.surface, suite_id=args.suite)
        if args.emit_units:
            emit_units(args, result["status"])
        print(json.dumps(result, sort_keys=True), file=sys.stderr)
        print(result["status"])
        return int(result["status"] == "fail")
    except (OSError, ValueError, ImportError, ParseError) as exc:
        print(f"required suite evidence unavailable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
