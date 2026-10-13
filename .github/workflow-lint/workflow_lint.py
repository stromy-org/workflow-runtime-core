#!/usr/bin/env python3
"""Run pinned offline workflow analyzers with exact, expiring finding holds."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from workflow_yaml import UniqueLoader

ROOT = Path(__file__).resolve().parents[1]
CI_INPUTS = ('.github/workflows/**', '.github/actions/**', 'scripts/ci/workflow-lint-baseline.json',
             'scripts/workflow_lint.py', 'scripts/workflow_yaml.py', 'scripts/ci/install-workflow-lint.sh')
VERSIONS = {'actionlint': '1.7.12', 'zizmor': '1.30.1', 'shellcheck': '0.11.0'}
BUNDLE_FILES = {'workflow_lint.py': 'scripts/workflow_lint.py',
                'workflow_yaml.py': 'scripts/workflow_yaml.py',
                'install-workflow-lint.sh': 'scripts/ci/install-workflow-lint.sh'}
BUNDLE_MANIFEST = 'workflow-lint-bundle.json'


def bundle_source(source_sha, *, root=ROOT):
    if not isinstance(source_sha, str) or not re.fullmatch(r'[a-f0-9]{40}', source_sha):
        raise ValueError('bundle source must be an immutable commit')
    result = {}
    for name, path in BUNDLE_FILES.items():
        proof = subprocess.run(['git', '-C', str(root), 'show', source_sha + ':' + path],
                               capture_output=True, timeout=10, check=False)
        if proof.returncode or not proof.stdout or len(proof.stdout) > 512_000:
            raise ValueError('canonical bundle source unavailable: ' + path)
        result[name] = proof.stdout
    return result


def check_bundle(path, *, canonical=None):
    manifest_path = path / BUNDLE_MANIFEST
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 16_000:
        raise ValueError('bundle manifest escapes bounds')
    data = json.loads(manifest_path.read_text())
    if (not isinstance(data, dict) or set(data) != {'schema_version', 'source_sha', 'files'}
            or data['schema_version'] != 1 or not isinstance(data['source_sha'], str)
            or not re.fullmatch(r'[a-f0-9]{40}', data['source_sha'])
            or not isinstance(data['files'], dict) or set(data['files']) != set(BUNDLE_FILES)):
        raise ValueError('bundle closure unavailable')
    expected = bundle_source(data['source_sha']) if canonical else None
    for name, digest_value in data['files'].items():
        file = path / name
        if (file.is_symlink() or file.stat().st_size > 512_000
                or not re.fullmatch(r'[a-f0-9]{64}', str(digest_value))):
            raise ValueError('bundle file escapes bounds')
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest_value or expected and raw != expected[name]:
            raise ValueError('bundle source drift: ' + name)
    return data['source_sha']


def emit_bundle(path, source_sha):
    content = bundle_source(source_sha)
    if path.is_symlink():
        raise ValueError('bundle destination is a symlink')
    path.mkdir(parents=True, exist_ok=True)
    if (path / BUNDLE_MANIFEST).is_symlink():
        raise ValueError('bundle manifest destination is a symlink')
    for name, raw in content.items():
        target = path / name
        if target.is_symlink():
            raise ValueError('bundle destination file is a symlink')
        target.write_bytes(raw)
    (path / 'install-workflow-lint.sh').chmod(0o755)
    data = {'schema_version': 1, 'source_sha': source_sha,
            'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in content.items()}}
    (path / BUNDLE_MANIFEST).write_text(json.dumps(data, indent=2) + '\n')
    check_bundle(path, canonical=True)


def digest(value):
    def normalize(item):
        if isinstance(item, dict):
            pairs = [[normalize(key), normalize(child)] for key, child in item.items()]
            return {'mapping': sorted(pairs, key=lambda pair: json.dumps(pair[0], sort_keys=True))}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise TypeError('source scalar type unavailable')

    raw = json.dumps(normalize(value), sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def source(root, name):
    path = root / name
    if (not name.startswith(('.github/workflows/', '.github/actions/'))
            or path.is_symlink() or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError('finding source escapes native callable inputs')
    if path.stat().st_size > 512_000:
        raise ValueError('native callable source exceeds bound')
    return yaml.load(path.read_text(), Loader=UniqueLoader)


def z_finding(root, item):
    if not isinstance(item, dict) or item.get('ignored') is not False:
        raise ValueError('zizmor finding unavailable or ignored')
    locs = item['locations']
    if not isinstance(locs, list) or not locs:
        raise ValueError('zizmor finding location unavailable')
    primary = locs[0]['symbolic']
    name = primary['key']['Local']['verbatim_path']
    route = primary['route']['route']
    value = source(root, name)
    enclosing_step = None
    for part in route:
        if set(part) == {'Key'}:
            key = part['Key']
            # PyYAML's YAML 1.1 loader represents the Actions `on` key as True.
            value = value[key if key in value else True if key == 'on' else key]
        elif set(part) == {'Index'} and type(part['Index']) is int:
            value = value[part['Index']]
            enclosing_step = value
        else:
            raise ValueError('unknown zizmor route component')
    return {'tool': 'zizmor', 'rule': item['ident'], 'path': name,
            'route': route, 'severity': item['determinations']['severity'],
            'fragment_sha256': digest(enclosing_step if enclosing_step is not None else value),
            'diagnostic_sha256': digest([(loc['symbolic']['annotation'], loc['concrete']['feature']) for loc in locs])}


def a_finding(root, item):
    if item['kind'] != 'shellcheck':
        # Native syntax, expression and actionlint semantic errors have no holds.
        raise ValueError('unwaivable actionlint finding: ' + item['message'])
    name = item['filepath']
    path = root / name
    source(root, name)
    document = yaml.compose(path.read_text())
    matches = []

    def visit(node, route=()):
        if isinstance(node, yaml.MappingNode):
            if any(key.value == 'run' and key.start_mark.line == item['line'] - 1 for key, _ in node.value):
                matches.append((route, yaml.serialize(node)))
            for key, value in node.value:
                visit(value, route + (key.value,))
        elif isinstance(node, yaml.SequenceNode):
            for index, value in enumerate(node.value):
                visit(value, route + (index,))

    visit(document)
    if len(matches) != 1:
        raise ValueError('ambiguous actionlint step location')
    route, text = matches[0]
    code = re.search(r'\bSC\d+\b', item['message'])
    if code is None:
        raise ValueError('shellcheck rule unavailable')
    return {'tool': 'actionlint', 'rule': code.group(), 'path': name,
            'route': list(route), 'severity': 'reported',
            'fragment_sha256': digest(text), 'diagnostic_sha256': digest(item['message'])}


def assess(findings, baseline, *, now=None):
    now = now or datetime.now(UTC)
    if (not isinstance(baseline, dict) or set(baseline) != {'schema_version', 'versions', 'holds'}
            or baseline['schema_version'] != 1 or baseline['versions'] != VERSIONS
            or not isinstance(baseline['holds'], list)):
        raise ValueError('lint baseline schema/version unavailable')
    expected = Counter()
    for hold in baseline['holds']:
        if set(hold) != {'finding', 'count', 'owner', 'backlog_ref', 'reason', 'expires'}:
            raise ValueError('invalid lint hold fields')
        finding = hold['finding']
        if (not isinstance(finding, dict) or set(finding) != {'tool', 'rule', 'path', 'route', 'severity', 'fragment_sha256', 'diagnostic_sha256'}
                or finding['tool'] not in {'actionlint', 'zizmor'} or not isinstance(finding['route'], list)
                or not re.fullmatch(r'[0-9a-f]{64}', str(finding['fragment_sha256']))
                or not re.fullmatch(r'[0-9a-f]{64}', str(finding['diagnostic_sha256']))
                or type(hold['count']) is not int or not 1 <= hold['count'] <= 10
                or any(not isinstance(hold[key], str) or not hold[key].strip() for key in ('owner', 'reason', 'backlog_ref'))
                or '*' in str(finding['path']) or '*' in str(finding['rule'])):
            raise ValueError('lint hold is not a scoped finding')
        expires = datetime.fromisoformat(hold['expires'])
        if expires.tzinfo is None or expires <= now or expires > now.replace(microsecond=0) + timedelta(days=31):
            raise ValueError('lint hold expired or exceeds 31 days')
        key = digest(finding)
        if key in expected:
            raise ValueError('duplicate lint hold')
        expected[key] = hold['count']
    observed = Counter(digest(finding) for finding in findings)
    return {'new': sum((observed - expected).values()),
            'stale': sum((expected - observed).values()), 'held': sum((observed & expected).values())}


def run(command, *, root, seconds=60):
    completed = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=seconds, check=False)
    if len(completed.stdout.encode()) > 8 * 1024 * 1024:
        raise ValueError('lint output exceeds bound')
    return completed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--report', type=Path)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--emit-template', type=Path, help='emit the immutable three-file public lint bundle')
    modes.add_argument('--check-template', type=Path, help='compare a bundle with canonical committed source')
    parser.add_argument('--source-sha', help='full canonical commit for --emit-template')
    args = parser.parse_args(argv)
    result = {'status': 'NOT-RUN', 'coverage': 'offline-native-workflows-and-actions', 'reason': '', 'findings': []}
    try:
        if args.emit_template or args.check_template:
            if args.emit_template:
                emit_bundle(args.emit_template, args.source_sha)
            else:
                check_bundle(args.check_template, canonical=True)
            print('Portable workflow lint bundle PASS')
            return 0
        local_bundle = Path(__file__).resolve().parent
        if (local_bundle / BUNDLE_MANIFEST).exists():
            check_bundle(local_bundle)
        workflows = sorted(args.root.glob('.github/workflows/*.y*ml'))
        actions = sorted(args.root.glob('.github/actions/**/action.y*ml'))
        if not workflows:
            raise ValueError('no native workflows; render before linting')
        files = [str(path.relative_to(args.root)) for path in workflows + actions]
        for name in files:
            source(args.root, name)
        for tool, version in VERSIONS.items():
            proof = run([tool, '-version' if tool == 'actionlint' else '--version'], root=args.root, seconds=10)
            if proof.returncode or not re.search(r'(?<![\d.])v?' + re.escape(version) + r'(?![\d.])', proof.stdout):
                raise ValueError(tool + ' pinned version unavailable')
        a = run(['actionlint', '-oneline', '-format', '{{json .}}', *files[:len(workflows)]], root=args.root)
        z = run(['zizmor', '--offline', '--no-ignores', '--strict-collection', '--format=json', *files], root=args.root)
        if a.returncode not in {0, 1} or z.returncode not in {0, 11, 12, 13, 14}:
            raise ValueError('lint tool failed collection/execution')
        a_items, z_items = json.loads(a.stdout), json.loads(z.stdout)
        if not isinstance(a_items, list) or not isinstance(z_items, list):
            raise TypeError('lint output shape unavailable')
        findings = [a_finding(args.root, item) for item in a_items] + [z_finding(args.root, item) for item in z_items]
        if a.returncode == 1 and not a_items or z.returncode != 0 and not z_items:
            raise ValueError('failed lint tool supplied no findings')
        baseline = args.baseline or args.root / 'scripts/ci/workflow-lint-baseline.json'
        counts = assess(findings, json.loads(baseline.read_text()))
        result.update(status='FINDING' if counts['new'] or counts['stale'] else 'PASS', findings=findings, counts=counts, files=len(files))
        if result['status'] == 'FINDING':
            print('Lint findings changed; review the exact holds and native source.')
            for finding in findings:
                print(finding['tool'] + ': ' + finding['path'] + ': ' + finding['rule'])
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.TimeoutExpired, yaml.YAMLError) as exc:
        result['reason'] = str(exc)[:300]
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: value for key, value in result.items() if key != 'findings'}))
    return 0 if result['status'] == 'PASS' else 1 if result['status'] == 'FINDING' else 3


if __name__ == '__main__':
    raise SystemExit(main())
