"""Publish source/run-bound execution counts from existing owning JUnit reports.

No tests are run and no network call exists here. The shared suite-contract
owner counts cases and enforces floors; green job conclusions are never input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .execution_contract import (
        CONTRACT_PATH,
        execution_verdict,
        junit_counts,
        load_contract,
    )
else:
    from execution_contract import (
        CONTRACT_PATH,
        execution_verdict,
        junit_counts,
        load_contract,
    )

CI_INPUTS = (".github/test-contract.yml",)
SHA = re.compile(r"[0-9a-f]{40}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def git(root, revision):
    result = subprocess.run(["git", "-C", str(root), "rev-parse", revision],
                            capture_output=True, text=True, check=False, timeout=10)
    identity = result.stdout.strip()
    if result.returncode or not SHA.fullmatch(identity):
        raise ValueError("execution source identity unavailable")
    return identity


def owning_suites(contract, slug, *, job=None):
    suites = [suite for suite in (contract or {}).get("suites", [])
              if suite["surface"] == "pr" and suite.get("execution_step", "tests") in
              {"tests", "contract", "node", "browser"}]
    if not suites or not any(suite["required"] for suite in suites):
        raise ValueError("required owning execution contract unavailable")
    if any(suite["owner"] != slug for suite in suites):
        raise ValueError("suite ownership differs from executing repository")
    if job is not None:
        if __package__:
            from .native_producer import part_suites
        else:
            from native_producer import part_suites
        selected = part_suites(contract, job)
        suites = [suite for suite in suites if suite['id'] in selected]
    return suites


def merged_checkout(value, *, repo, code_sha, tree_sha, run, merged_pr, tested_commit):
    """A tested PR merge tree may qualify the byte-identical landed tree.

    The reader obtains the associated merged PR and tested Git object through
    locally constructed native API paths. Closed runs may have no pull_requests
    entries, so the independent merged PR, parents and exact tree are required.
    """
    if not isinstance(merged_pr, dict) or not isinstance(tested_commit, dict):
        raise TypeError("tested PR lacks independently observed merged source")
    head, base = merged_pr.get("head"), merged_pr.get("base")
    number = merged_pr.get("number")
    if (not isinstance(head, dict) or not isinstance(base, dict)
            or type(number) is not int or number < 1 or merged_pr.get("merged") is not True
            or merged_pr.get("state") != "closed" or merged_pr.get("merge_commit_sha") != code_sha
            or merged_pr.get("html_url") != f"https://github.com/{repo}/pull/{number}"
            or base.get("ref") != "main" or not isinstance(head.get("ref"), str) or not head["ref"]
            or run.get("head_branch") != head["ref"] or run.get("head_sha") != head.get("sha")
            or any(not isinstance(side.get("repo"), dict) or side["repo"].get("full_name") != repo
                   or not SHA.fullmatch(str(side.get("sha"))) for side in (head, base))):
        raise ValueError("tested PR is unmerged, forked or belongs to another source")
    parents = tested_commit.get("parents")
    commit = tested_commit.get("commit")
    tree = commit.get("tree") if isinstance(commit, dict) else None
    if (tested_commit.get("sha") != value["code_sha"] or not isinstance(tree, dict)
            or tree.get("sha") != tree_sha or not isinstance(parents, list) or len(parents) != 2
            or any(not isinstance(parent, dict) for parent in parents)
            or [parent.get("sha") for parent in parents] != [base["sha"], head["sha"]]):
        raise ValueError("tested PR checkout differs from native parents or landed tree")


def validate(value, contract, *, repo, code_sha, tree_sha, checker_sha, run, now=None,
             merged_pr=None, tested_commit=None, job=None):
    """Independent source/run observations qualify counts, never job conclusions."""
    now = now or datetime.now(UTC)
    fields = {"schema_version", "repo", "code_sha", "consumer_tree", "checker_sha", "producer",
              "timestamp", "contract_digest", "suite_ids", "suite_counts", "result"}
    if __package__:
        from .native_producer import part_suites, recipe
    else:
        from native_producer import part_suites, recipe
    declared = recipe(contract)
    multi = declared is not None and declared['format'] == 'owning-workflow-v2'
    if multi:
        part_suites(contract, job)
        fields |= {'producer_job', 'case_ids', 'report_counts'}
    if (not isinstance(value, dict) or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != (2 if multi else 1)
            or value["result"] != "pass" or value["repo"] != repo
            or any(not isinstance(sha, str) or not SHA.fullmatch(sha)
                   for sha in (code_sha, tree_sha, checker_sha))
            or not SHA.fullmatch(str(value["code_sha"]))
            or (value["consumer_tree"], value["checker_sha"]) != (tree_sha, checker_sha)):
        raise ValueError("suite artifact source identity differs")
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] < 1
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
            or run.get("event") not in {"push", "pull_request"} or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or not isinstance(run.get("repository"), dict)
            or run["repository"].get("full_name") != repo
            or run.get("html_url") != f"https://github.com/{repo}/actions/runs/{run['id']}"):
        raise ValueError("suite artifact native main producer differs")
    if run["event"] == "pull_request":
        merged_checkout(value, repo=repo, code_sha=code_sha, tree_sha=tree_sha, run=run,
                        merged_pr=merged_pr, tested_commit=tested_commit)
    elif (run.get("head_sha") != code_sha or value["code_sha"] != code_sha
          or run.get("head_branch") != "main"):
        raise ValueError("suite artifact native main source differs")
    producer = {"run_id": str(run["id"]), "attempt": run["run_attempt"], "url": run["html_url"]}
    if (value["producer"] != producer
            or type(value["producer"].get("attempt")) is not int):
        raise ValueError("suite artifact producer attempt differs")
    if __package__:
        from .native_producer import checker
    else:
        from native_producer import checker

    called = checker(contract, run, repo=repo, code_sha=value["code_sha"], job=job)
    if called != checker_sha:
        raise ValueError("suite artifact differs from native called checker")
    instant = datetime.fromisoformat(value["timestamp"])
    if instant.tzinfo is None or not 0 <= (now - instant).total_seconds() <= 7 * 86400:
        raise ValueError("suite artifact stale or future")
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    if value["contract_digest"] != hashlib.sha256(canonical).hexdigest():
        raise ValueError("suite artifact owning contract differs")
    suites = owning_suites(contract, repo, job=job)
    counts = value["suite_counts"]
    if (value["suite_ids"] != [suite["id"] for suite in suites] or not isinstance(counts, dict)
            or set(counts) != {suite["id"] for suite in suites}
            or any(not isinstance(c, dict) or set(c) != {"collected", "executed", "skipped", "failed"}
                   for c in counts.values())
            or any(execution_verdict(suite, counts[suite["id"]]) not in {"pass", "not-applicable"}
                   for suite in suites)
            or (sum(c["executed"] for c in counts.values()) == 0 and any(s['required'] for s in suites))):
        raise ValueError("suite artifact missing, failed or below owning execution floor")
    if multi:
        identities = value['case_ids']
        reports = value['report_counts']
        bindings = part_suites(contract, job)
        if bindings is None:
            raise ValueError('multi-job report ownership unavailable')
        if (not isinstance(reports, dict) or set(reports) != {row['report'] for row in bindings.values()}
                or any(not isinstance(counts, dict) or set(counts) != {'collected', 'executed', 'skipped', 'failed'}
                    or execution_verdict({'required': False, 'minimum_executed': 0}, counts)
                       not in {'pass', 'not-applicable'} for counts in reports.values())
                or any(counts[suite][metric] > reports[binding['report']][metric]
                       for suite, binding in bindings.items() for metric in ('collected', 'executed', 'skipped', 'failed'))):
            raise ValueError('multi-job report counts or suite population differs')
        if (value['producer_job'] != job or not isinstance(identities, list)
                or len(identities) > 25_000 or len(identities) != len(set(identities))
                or any(not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity)
                       for identity in identities)
                or len(identities) != sum(c['collected'] for c in reports.values())):
            raise ValueError('multi-job testcase identity population differs')
    return value


def record(repo, checker, reports, *, env=None, now=None, job=None):
    env = env if env is not None else os.environ
    now = now or datetime.now(UTC)
    slug, run_id = env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    code_sha, checker_sha = env.get("GITHUB_SHA", ""), env.get("CHECKER_SHA", "")
    if (not REPO.fullmatch(slug) or any(part in {".", ".."} for part in slug.split("/"))
            or not re.fullmatch(r"[1-9][0-9]*", run_id) or not re.fullmatch(r"[1-9][0-9]*", attempt)
            or not SHA.fullmatch(code_sha) or not SHA.fullmatch(checker_sha)
            or git(repo, "HEAD^{commit}") != code_sha or git(checker, "HEAD^{commit}") != checker_sha):
        raise ValueError("execution run or checked-out source identity differs")
    contract_path = repo / CONTRACT_PATH
    declared = subprocess.run(["git", "-C", str(repo), "show", code_sha + ":" + CONTRACT_PATH],
                              capture_output=True, check=False, timeout=10)
    if (declared.returncode or len(declared.stdout) > 128_000
            or contract_path.is_symlink() or contract_path.read_bytes() != declared.stdout):
        raise ValueError("execution contract differs from immutable source")
    contract = load_contract(repo)
    if __package__:
        from .native_producer import declaration, recipe
    else:
        from native_producer import declaration, recipe
    declared = recipe(contract) if contract is not None else None
    multi = declared is not None and declared['format'] == 'owning-workflow-v2'
    shared = multi and job in declared['jobs'] and 'reusable_workflow' in declared['jobs'][job]
    if declared is not None and not shared and checker_sha != code_sha:
        raise ValueError("owning checker is not the executing consumer revision")
    if multi and (not job or env.get('GITHUB_JOB') != ('checks' if shared else job)):
        raise ValueError('owning producer job differs from executing native job')
    if shared:
        workflow = declared['workflow']
        raw = subprocess.run(['git', '-C', str(repo), 'show', code_sha + ':' + workflow],
                             capture_output=True, check=False, timeout=10)
        if raw.returncode or len(raw.stdout) > 128_000:
            raise ValueError('shared owning caller source unavailable')
        declaration({'files': {workflow: raw.stdout.decode()}}, contract)
    suites = owning_suites(contract, slug, job=job)
    bindings = {}
    if multi:
        assert declared is not None
        bindings = declared['jobs'][job]['suites']
    paths = [path.resolve() for path in reports]
    if not paths or len(paths) != len(set(paths)) or len(paths) > (20 if multi else 4):
        raise ValueError("execution report set missing, duplicated or over bounds")
    if multi and (len(paths) != len({binding['report'] for binding in bindings.values()}) or
                  {path.name for path in paths} != {binding['report'] for binding in bindings.values()}):
        raise ValueError('owning job report population incomplete or duplicated')
    counts = {suite["id"]: {"collected": 0, "executed": 0, "skipped": 0, "failed": 0} for suite in suites}
    seen, report_counts = set(), {}
    for path in paths:
        step = {"ci-tests.xml": "tests", "ci-contract.xml": "contract",
                "ci-node.xml": "node", "ci-browser.xml": "browser"}.get(path.name)
        if step is None and not multi:
            raise ValueError("execution report has no canonical runner step")
        owners = [suite for suite in suites if (bindings[suite['id']]['report'] == path.name if multi
                   else suite.get("execution_step", "tests") == step)]
        body = path.read_bytes()
        if len(body) > 4_000_000 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("execution report unavailable or unsafe")
        tree = ET.fromstring(body)
        if tree.tag not in {"testsuite", "testsuites"}:
            raise ValueError("execution report root unavailable")
        raw = {'collected': 0, 'executed': 0, 'skipped': 0, 'failed': 0}
        for case in tree.iter("testcase"):
            context = bindings[owners[0]['id']]['step'] if multi else step
            identity = (context, case.get('classname'), case.get('name'))
            if not all(identity) or identity in seen:
                raise ValueError("execution case identity missing or duplicated")
            seen.add(identity)
            raw['collected'] += 1
            raw['skipped' if case.find('skipped') is not None else 'executed'] += 1
            raw['failed'] += int(case.find('failure') is not None or case.find('error') is not None)
        report_counts[path.name] = raw
        for suite_id, values in junit_counts(path, owners, repo=repo).items():
            for key, count in values.items():
                counts[suite_id][key] += count
    verdicts = {suite["id"]: execution_verdict(suite, counts[suite["id"]]) for suite in suites}
    if (any(state not in {"pass", "not-applicable"} for state in verdicts.values())
            or (sum(values["executed"] for values in counts.values()) == 0 and any(s['required'] for s in suites))):
        raise ValueError("required execution failed, missing or below owning floor")
    canonical = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    value = {"schema_version": 2 if multi else 1, "repo": slug, "code_sha": code_sha,
            "consumer_tree": git(repo, "HEAD^{tree}"), "checker_sha": checker_sha,
            "producer": {"run_id": run_id, "attempt": int(attempt),
                         "url": f"https://github.com/{slug}/actions/runs/{run_id}"},
            "timestamp": now.isoformat(), "contract_digest": hashlib.sha256(canonical).hexdigest(),
            "suite_ids": [suite["id"] for suite in suites], "suite_counts": counts,
            "result": "pass"}
    if multi:
        if any(execution_verdict({'required': False, 'minimum_executed': 0}, counts)
               not in {'pass', 'not-applicable'} for counts in report_counts.values()):
            raise ValueError('owning report contains failed or inconsistent cases')
        value.update(producer_job=job, report_counts=report_counts, case_ids=sorted(hashlib.sha256(
            json.dumps(identity, separators=(',', ':')).encode()).hexdigest() for identity in seen))
        if len(seen) != sum(c['collected'] for c in report_counts.values()) or len(seen) > 25_000:
            raise ValueError('owning reports include duplicate or excessive testcase identities')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--checker", type=Path, required=True)
    parser.add_argument("--junit", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument('--job', help='declared owning-workflow-v2 native job identity')
    args = parser.parse_args()
    value = record(args.repo, args.checker, args.junit, job=args.job)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    print("suite-evidence: actual source/run-bound counts retained")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
