"""Qualify owning workflows with independent direct and shared Python checkers.

Recipes contain identities only. They never supply executable commands. The
owning workflow and contract are read together at the independently observed
consumer revision; executed native steps and receipt counts remain mandatory.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
from itertools import product
from pathlib import Path
from typing import Any

if __package__:
    from .workflow_identity import resolve
else:
    from workflow_identity import resolve

CI_INPUTS = ("scripts/ci/test-contract.schema.json", "maintenance/ci-channel.json", "maintenance/ci-release.json")
RECORD_STEP = "Record executed-suite evidence for future reuse"
UPLOAD_STEP = "Retain owning suite counts for source canaries"
ARTIFACT = "ci-suite-evidence-checks"
REPORT = "ci-suite-evidence.json"
IDENTITY = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,99}\Z')
WORKFLOW = re.compile(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml\Z')
PYTHON_WORKFLOW = 'stromy-org/stromy-org/.github/workflows/ci-python.yml'
CHANNEL_REF = 'ci-stable'
CHANNEL_RECORDS = ('maintenance/ci-channel.json', 'maintenance/ci-release.json')
RELEASE_TAG = re.compile(r'ci-v[1-9][0-9]*(?:\.[1-9][0-9]*)?\Z')
# A full SHA, an immutable release tag, or the ruleset-protected channel branch.
PYTHON_CALLER = re.compile(re.escape(PYTHON_WORKFLOW) + r'@(?:[a-f0-9]{40}|ci-v[1-9][0-9]*|ci-stable)\Z')
PYTHON_REPORTS ={'tests': ('Tests', 'ci-tests.xml'), 'contract': ('Contract tests', 'ci-contract.xml')}
MAX_PARTS = 16


def multi_recipe(contract, value):
    if (set(value) != {'format', 'workflow', 'jobs'} or not WORKFLOW.fullmatch(str(value['workflow']))
            or not isinstance(value['jobs'], dict) or not 1 <= len(value['jobs']) <= MAX_PARTS):
        raise ValueError('owning multi-job recipe unavailable')
    suites = {s['id'] for s in contract['suites'] if s['surface'] == 'pr'}
    if not suites or not any(s['required'] and s['surface'] == 'pr' for s in contract['suites']):
        raise ValueError('owning multi-job required suite population unavailable')
    seen = set()
    for job, row in value['jobs'].items():
        if (not IDENTITY.fullmatch(str(job)) or not isinstance(row, dict)
                or 'suites' not in row or set(row) - {'suites', 'reusable_workflow', 'native_name', 'matrix'}
                or ('reusable_workflow' in row and row['reusable_workflow'] != PYTHON_WORKFLOW)
                or not isinstance(row['suites'], dict) or not 1 <= len(row['suites']) <= 20):
            raise ValueError('owning multi-job identity unavailable')
        if 'matrix' in row:
            matrix = row['matrix']
            values = matrix.get('values') if isinstance(matrix, dict) else None
            if (not isinstance(matrix, dict) or set(matrix) != {'job', 'values'}
                    or not IDENTITY.fullmatch(str(matrix['job'])) or 'native_name' not in row
                    or 'reusable_workflow' in row or not isinstance(values, dict) or not 1 <= len(values) <= 12
                    or any(not IDENTITY.fullmatch(str(key)) or type(value) not in {str, int, bool}
                           or isinstance(value, str) and ('${{' in value or len(value) > 160)
                           for key, value in values.items())):
                raise ValueError('owning finite matrix identity unavailable')
        name = row.get('native_name', job)
        if (not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 160
                or any(token in name for token in ('${{', '\n', '\r'))):
            raise ValueError('owning native job name is not static')
        reports = {}
        for suite, binding in row['suites'].items():
            if suite not in suites or suite in seen or not isinstance(binding, dict) or set(binding) != {'step', 'report'}:
                raise ValueError('owning multi-job suite ownership unavailable or duplicated')
            name, report = binding['step'], binding['report']
            if (not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 160
                    or any(token in name for token in ('${{', '\n', '\r')) or name in {RECORD_STEP, UPLOAD_STEP}
                    or not isinstance(report, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.xml', report)
                    or (report in reports and reports[report] != name)):
                raise ValueError('owning multi-job step/report identity unavailable')
            seen.add(suite)
            reports[report] = name
            if 'reusable_workflow' in row:
                step = next(s for s in contract['suites'] if s['id'] == suite).get('execution_step', 'tests')
                if PYTHON_REPORTS.get(step) != (name, report):
                    raise ValueError('shared Python suite has a different runner/report binding')
    if sum('reusable_workflow' in row for row in value['jobs'].values()) > 1:
        raise ValueError('shared Python checker invocation is ambiguous')
    if seen != suites:
        raise ValueError('owning multi-job suite population incomplete')
    return value


def matrix_text(value, values):
    """Expand literal include-row identity fields only; never evaluate code."""
    if not isinstance(value, str):
        raise TypeError('matrix native identity is not text')
    def replace(match):
        key = match.group(1)
        if key not in values:
            raise ValueError('matrix native identity references an absent field')
        item = values[key]
        return str(item).lower() if type(item) is bool else str(item)
    result = re.sub(r'\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}', replace, value)
    if '${{' in result:
        raise ValueError('matrix native identity has an unsupported expression')
    return result


def event_rows(rows):
    """Recognize only the finite push-subset shape shipped by Astro's owner.

    This is a declaration of the full population, never permission to accept
    a partial push run. Native execution must still prove every declared part.
    No expression evaluator or caller-provided command is involved.
    """
    if not isinstance(rows, str):
        return rows
    match = re.fullmatch(
        r"\$\{\{\s*fromJSON\(github\.event_name == 'push' && '([^']*)' \|\| '([^']*)'\)\s*\}\}",
        rows)
    if not match:
        raise ValueError('owning matrix event population is not finite literals')
    push, full = (json.loads(value) for value in match.groups())
    if (not isinstance(push, list) or not isinstance(full, list) or not push
            or len(push) > MAX_PARTS or len(full) > MAX_PARTS
            or any(not isinstance(row, dict) for row in [*push, *full])
            or len({json.dumps(row, sort_keys=True) for row in push}) != len(push)
            or any(row not in full for row in push)):
        raise ValueError('owning matrix push population is not a literal subset')
    return full


def matrix_job(jobs, parts, part):
    """Every literal include row has exactly one declared native part."""
    identity, values = part['matrix']['job'], part['matrix']['values']
    job = jobs.get(identity) if isinstance(jobs, dict) else None
    strategy = job.get('strategy') if isinstance(job, dict) else None
    matrix = strategy.get('matrix') if isinstance(strategy, dict) else None
    rows = event_rows(matrix.get('include')) if isinstance(matrix, dict) else None
    if isinstance(matrix, dict) and matrix and not {'include', 'exclude'} & matrix.keys():
        # A literal Cartesian product is also finite. Never expand a dynamic
        # expression, mixed include/exclude semantics or an oversized population.
        if (any(not isinstance(axis, list) or not 1 <= len(axis) <= MAX_PARTS for axis in matrix.values())
                or any(type(item) not in {str, int, bool} for axis in matrix.values() for item in axis)):
            raise ValueError('owning matrix axes are not finite literals')
        size = 1
        for axis in matrix.values():
            size *= len(axis)
        if size > MAX_PARTS:
            raise ValueError('owning matrix exceeds the native part bound')
        # product yields exactly one value per axis. Indexed construction keeps
        # that total mapping without requiring Python 3.10's zip(strict=): the
        # fingerprint owner is also imported by host-Python (3.9) push hooks.
        rows = [{axis: items[index] for index, axis in enumerate(matrix)}
                for items in product(*matrix.values())]
    declared = [row['matrix']['values'] for row in parts.values()
                if row.get('matrix', {}).get('job') == identity]
    canonical = lambda row: json.dumps(row, sort_keys=True, separators=(',', ':'))
    if (not isinstance(job, dict) or not isinstance(matrix, dict)
            or 'include' in matrix and set(matrix) != {'include'} or 'exclude' in matrix
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_PARTS
            or any(not isinstance(row, dict) for row in rows)
            or len({canonical(row) for row in rows}) != len(rows)
            or len(declared) != len(rows) or len({canonical(row) for row in declared}) != len(rows)
            or {canonical(row) for row in rows} != {canonical(row) for row in declared}
            or set(strategy) - {'matrix', 'fail-fast', 'max-parallel'}):
        raise ValueError('owning matrix include population is incomplete or dynamic')
    # Build only the fields consumed by the existing direct-job identity gate.
    steps = job.get('steps')
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise ValueError('owning matrix has no direct steps')
    expanded = []
    for step in steps:
        item = dict(step)
        if 'name' in item:
            item['name'] = matrix_text(item['name'], values)
        if item.get('name') == UPLOAD_STEP and isinstance(item.get('with'), dict):
            item['with'] = {**item['with'], 'name': matrix_text(item['with'].get('name'), values)}
        expanded.append(item)
    return {identity: {**{key: value for key, value in job.items() if key != 'strategy'},
                       'name': matrix_text(job.get('name', identity), values), 'steps': expanded}}


def recipe(contract: dict[str, Any]) -> dict[str, Any] | None:
    value = contract.get("execution_producer")
    if value is None:
        return None
    if isinstance(value, dict) and value.get('format') == 'owning-workflow-v2':
        return multi_recipe(contract, value)
    if (not isinstance(value, dict) or set(value) != {"format", "workflow", "job", "steps"}
            or value["format"] != "owning-workflow-v1"
            or value["workflow"] not in {".github/workflows/ci.yml", ".github/workflows/ci.yaml"}
            or not isinstance(value["job"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,99}", value["job"])
            or not isinstance(value["steps"], dict) or not value["steps"]
            or set(value["steps"]) - {"tests", "contract", "node", "browser"}):
        raise ValueError("owning producer recipe unavailable")
    names = list(value["steps"].values())
    if (any(not isinstance(name, str) or not name.strip() or name != name.strip()
            or len(name) > 160 or "${{" in name or "\n" in name or "\r" in name for name in names)
            or len(names) != len(set(names)) or {RECORD_STEP, UPLOAD_STEP} & set(names)):
        raise ValueError("owning producer step identity ambiguous")
    required = {s.get("execution_step", "tests") for s in contract["suites"]
                if s["required"] and s["surface"] == "pr"}
    if not required or not required <= set(value["steps"]):
        raise ValueError("owning producer does not cover required runner steps")
    return value


def declaration(snapshot: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any] | None:
    value = recipe(contract)
    if value is None:
        return None
    # The public emission closure keeps this dependency beside scripts/ci.
    # A standalone recorder starts with only scripts/ci on its import path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from workflow_yaml import UniqueLoader, yaml

    workflow = yaml.load(snapshot["files"].get(value["workflow"], ""), Loader=UniqueLoader)
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if value['format'] == 'owning-workflow-v2':
        for identity, part in value['jobs'].items():
            names = [binding['step'] for binding in part['suites'].values()]
            native_name = part.get('native_name', identity)
            if 'reusable_workflow' in part:
                job = jobs.get(identity) if isinstance(jobs, dict) else None
                settings = job.get('with') if isinstance(job, dict) else None
                if (not isinstance(job, dict) or 'strategy' in job or 'steps' in job
                        or job.get('name', identity) != native_name
                        or not PYTHON_CALLER.fullmatch(str(job.get('uses')))
                        or not isinstance(settings, dict) or settings.get('suite_evidence_job') != identity
                        or ('Contract tests' in names and settings.get('run_contract_tests', True) is not True)):
                    raise ValueError('shared Python owning caller identity/input differs')
            else:
                selected, native_job = jobs, identity
                if 'matrix' in part:
                    selected = matrix_job(jobs, value['jobs'], part)
                    native_job = part['matrix']['job']
                declared_job(selected, native_job, list(dict.fromkeys(names)), 'ci-suite-evidence-' + identity,
                             native_name=native_name)
    else:
        declared_job(jobs, value['job'], list(value['steps'].values()), ARTIFACT)
    return value


def channel_parts(snapshot: dict[str, Any], value: dict[str, Any] | None) -> set[str]:
    """Shared parts of a qualified declaration whose caller names the channel.

    declaration() admits `@ci-stable` by syntax only, exactly as it admits a
    `ci-vN` tag. The commit behind it is proved where execution evidence exists:
    the recorder by channel_release(), the run reader by checker().
    """
    if value is None or value['format'] != 'owning-workflow-v2':
        return set()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from workflow_yaml import UniqueLoader, yaml

    workflow = yaml.load(snapshot["files"].get(value["workflow"], ""), Loader=UniqueLoader)
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else {}
    return {identity for identity, part in value['jobs'].items()
            if 'reusable_workflow' in part and isinstance(jobs, dict) and isinstance(jobs.get(identity), dict)
            and jobs[identity].get('uses') == PYTHON_WORKFLOW + '@' + CHANNEL_REF}


def channel_release(policy: Path | None, sha: str) -> dict[str, str]:
    """Resolve the protected channel to one published release, or raise.

    Reads the same two records the R8 channel rule admits through
    (scripts/action_pins.py:channel_admission): the operator activation record
    and the mover's `channel: {tag, sha}` entry naming the current or previous
    published release. The executing checker commit must be that release.
    """
    unavailable = ValueError('shared Python channel caller does not resolve to a published release')
    if policy is None:
        raise unavailable
    rows = []
    for name in CHANNEL_RECORDS:
        path = Path(policy) / name
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000:
                raise unavailable
            rows.append(json.loads(path.read_text()))
        except (OSError, ValueError):
            raise unavailable from None
    config, release = rows
    if (not isinstance(config, dict)
            or set(config) != {'schema', 'ref', 'ruleset_id', 'app_id', 'ruleset_updated_at'}
            or config['schema'] != 1 or config['ref'] != CHANNEL_REF
            or any(type(config[key]) is not int or config[key] < 1 for key in ('ruleset_id', 'app_id'))
            or not isinstance(config['ruleset_updated_at'], str)
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+(?:Z|[+-]\d\d:\d\d)',
                                config['ruleset_updated_at'])):
        raise unavailable
    channel = release.get('channel') if isinstance(release, dict) else None
    supported = [row for row in (release.get('current'), release.get('previous'))
                 if isinstance(row, dict)] if isinstance(release, dict) else []
    if (not isinstance(channel, dict) or set(channel) != {'tag', 'sha'}
            or not isinstance(channel['tag'], str) or not RELEASE_TAG.fullmatch(channel['tag'])
            or not isinstance(channel['sha'], str) or not re.fullmatch(r'[0-9a-f]{40}', channel['sha'])
            or not any(row.get('tag') == channel['tag'] and row.get('sha') == channel['sha']
                       for row in supported)
            or not isinstance(sha, str) or sha != channel['sha']):
        raise unavailable
    return {'tag': channel['tag'], 'sha': channel['sha']}


def declared_job(jobs, identity, names, artifact, *, native_name=None):
    job = jobs.get(identity) if isinstance(jobs, dict) else None
    if (not isinstance(job, dict) or "strategy" in job or "uses" in job
            or job.get("name", identity) != (native_name or identity)
            or not isinstance(job.get("steps"), list)):
        raise ValueError("owning producer must be one static direct job")
    for name in [*names, RECORD_STEP, UPLOAD_STEP]:
        matches = [step for step in job["steps"] if isinstance(step, dict) and step.get("name") == name]
        if len(matches) != 1:
            raise ValueError("owning workflow lacks a unique declared execution step")
        step = matches[0]
        if name == UPLOAD_STEP:
            settings = step.get("with")
            if (not re.fullmatch(r"actions/upload-artifact@[0-9a-f]{40}", str(step.get("uses")))
                    or not isinstance(settings, dict) or settings.get("name") != artifact
                    or settings.get("path") != REPORT
                    or type(settings.get("retention-days")) is not int
                    or not 35 <= settings["retention-days"] <= 90
                    or settings.get("if-no-files-found") != "error"):
                raise ValueError("owning suite artifact retention or upload identity differs")
        elif not isinstance(step.get("run"), str) or not step["run"].strip() or "uses" in step:
            raise ValueError("owning execution step lacks its native runner")
        elif name in names and '--install-only' in step['run']:
            tokens = shlex.split(step['run'].replace('\\\n', ' '), comments=True)
            if '--install-only' in tokens:
                raise ValueError('setup-only invocation cannot prove native suite execution')


def part_suites(contract, job=None):
    value = recipe(contract)
    if value is None or value['format'] != 'owning-workflow-v2':
        if job is not None:
            raise ValueError('single-job execution has no multi-job part')
        return None
    if job not in value['jobs']:
        raise ValueError('owning execution job is not declared')
    return value['jobs'][job]['suites']


def checker(contract: dict[str, Any], run: dict[str, Any], *, repo: str, code_sha: str,
            job: str | None = None) -> str:
    producer = recipe(contract)
    if producer is not None:
        if (not re.fullmatch(r"[0-9a-f]{40}", code_sha)
                or run.get("path") != contract["execution_producer"]["workflow"]):
            raise ValueError("owning checker revision unavailable")
        if producer['format'] == 'owning-workflow-v2' and job is not None:
            part_suites(contract, job)
            if 'reusable_workflow' in producer['jobs'][job]:
                called = resolve(run, repo=repo, run_id=str(run['id']), workflow=PYTHON_WORKFLOW)
                # A tag spelled like the channel escapes its branch ruleset.
                for row in run['referenced_workflows']:
                    path, _, ref = row['path'].partition('@')
                    if (path == PYTHON_WORKFLOW and ref.rpartition('/')[2] == CHANNEL_REF
                            and ref not in {CHANNEL_REF, 'refs/heads/' + CHANNEL_REF}):
                        raise ValueError('shared Python channel caller is not the protected channel branch')
                return called
        # This format executes the consumer's own committed checker. A tested
        # PR merge is qualified separately by its parents and identical tree.
        return code_sha
    return resolve(run, repo=repo, run_id=str(run["id"]),
                   workflow="stromy-org/stromy-org/.github/workflows/ci-python.yml")
