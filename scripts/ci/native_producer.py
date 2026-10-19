"""Qualify owning workflows with independent direct and shared Python checkers.

Recipes contain identities only. They never supply executable commands. The
owning workflow and contract are read together at the independently observed
consumer revision; executed native steps and receipt counts remain mandatory.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .workflow_identity import resolve
else:
    from workflow_identity import resolve

CI_INPUTS = ("scripts/ci/test-contract.schema.json",)
RECORD_STEP = "Record executed-suite evidence for future reuse"
UPLOAD_STEP = "Retain owning suite counts for source canaries"
ARTIFACT = "ci-suite-evidence-checks"
REPORT = "ci-suite-evidence.json"
IDENTITY = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,99}\Z')
WORKFLOW = re.compile(r'\.github/workflows/[A-Za-z0-9_-]+\.ya?ml\Z')
PYTHON_WORKFLOW = 'stromy-org/stromy-org/.github/workflows/ci-python.yml'
PYTHON_REPORTS = {'tests': ('Tests', 'ci-tests.xml'), 'contract': ('Contract tests', 'ci-contract.xml')}


def multi_recipe(contract, value):
    if (set(value) != {'format', 'workflow', 'jobs'} or not WORKFLOW.fullmatch(str(value['workflow']))
            or not isinstance(value['jobs'], dict) or not 1 <= len(value['jobs']) <= 8):
        raise ValueError('owning multi-job recipe unavailable')
    suites = {s['id'] for s in contract['suites'] if s['surface'] == 'pr'}
    if not suites or not any(s['required'] and s['surface'] == 'pr' for s in contract['suites']):
        raise ValueError('owning multi-job required suite population unavailable')
    seen = set()
    for job, row in value['jobs'].items():
        if (not IDENTITY.fullmatch(str(job)) or not isinstance(row, dict)
                or 'suites' not in row or set(row) - {'suites', 'reusable_workflow', 'native_name'}
                or ('reusable_workflow' in row and row['reusable_workflow'] != PYTHON_WORKFLOW)
                or not isinstance(row['suites'], dict) or not 1 <= len(row['suites']) <= 20):
            raise ValueError('owning multi-job identity unavailable')
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
                        or not re.fullmatch(re.escape(PYTHON_WORKFLOW) + r'@(?:[a-f0-9]{40}|ci-v[1-9][0-9]*)', str(job.get('uses')))
                        or not isinstance(settings, dict) or settings.get('suite_evidence_job') != identity
                        or ('Contract tests' in names and settings.get('run_contract_tests', True) is not True)):
                    raise ValueError('shared Python owning caller identity/input differs')
            else:
                declared_job(jobs, identity, list(dict.fromkeys(names)), 'ci-suite-evidence-' + identity,
                             native_name=native_name)
    else:
        declared_job(jobs, value['job'], list(value['steps'].values()), ARTIFACT)
    return value


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
                return resolve(run, repo=repo, run_id=str(run['id']), workflow=PYTHON_WORKFLOW)
        # This format executes the consumer's own committed checker. A tested
        # PR merge is qualified separately by its parents and identical tree.
        return code_sha
    return resolve(run, repo=repo, run_id=str(run["id"]),
                   workflow="stromy-org/stromy-org/.github/workflows/ci-python.yml")
