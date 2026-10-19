"""Resolve the called checker revision; keep frozen code beside fresh contract data.

The resolver is embedded by value before checkout because its own source is not
available until the called revision is known. render_bootstrap() owns that copy.
No caller SHA, default branch or missing identity substitutes for a checker SHA.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CI_INPUTS = (".github/workflows/ci-python.yml", ".github/workflows/parent-contract.yml",
             ".github/workflows/explain-pin.yml", ".github/workflows/copier-update.yml")
TARGETS = {
    "ci-python.yml": "steps.classify.outputs.docs_only != 'true' && steps.appcheck.outputs.present == 'true'",
    "parent-contract.yml": "steps.appcheck.outputs.present == 'true'",
    "explain-pin.yml": "always()",
    "copier-update.yml": "steps.token.outputs.available == 'true'",
}


def resolve(value, *, repo, run_id, workflow):
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)
            or not re.fullmatch(r"stromy-org/stromy-org/\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", workflow)
            or not isinstance(value, dict) or type(value.get("id")) is not int
            or value["id"] != int(run_id)
            or not isinstance(value.get("repository"), dict)
            or value["repository"].get("full_name") != repo):
        raise ValueError("called workflow run identity unavailable")
    rows = value.get("referenced_workflows")
    if not isinstance(rows, list):
        raise TypeError("called workflow references unavailable")
    matches = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            raise TypeError("malformed called workflow reference")
        path, separator, ref = row["path"].partition("@")
        if path == workflow:
            if (not separator or not re.fullmatch(r"[A-Za-z0-9_./-]+", ref)
                    or ".." in ref or ref.startswith("/")
                    or not re.fullmatch(r"[0-9a-f]{40}", str(row.get("sha")))):
                raise ValueError("called checker revision unavailable")
            matches.append(row["sha"])
    if len(matches) != 1:
        raise ValueError("called checker revision missing or ambiguous")
    return matches[0]


def bootstrap():
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    workflow = os.environ["CHECKER_WORKFLOW"]
    if (not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            or not re.fullmatch(r"[1-9][0-9]*", run_id)):
        raise ValueError("invalid called workflow query")
    response = subprocess.run(["gh", "api", f"repos/{repo}/actions/runs/{run_id}"],
                              capture_output=True, text=True, timeout=15, check=False)
    if response.returncode or len(response.stdout.encode()) > 2_000_000:
        raise ValueError("called workflow lookup refused or over bound; no retry")
    sha = resolve(json.loads(response.stdout), repo=repo, run_id=run_id, workflow=workflow)
    expected = os.environ.get("CHECKER_EXPECTED_REF")
    if expected:
        if not re.fullmatch(r"(?:copier-fleet-v[1-9][0-9]*|ci-v[1-9][0-9]*(?:\.[1-9][0-9]*)?)", expected):
            raise ValueError("engine ref is not a registered release tag")
        rows = json.loads(response.stdout)["referenced_workflows"]
        match = next(row for row in rows if row["path"].partition("@")[0] == workflow)
        if match["path"].partition("@")[2] not in {expected, "refs/tags/" + expected}:
            raise ValueError("engine ref differs from the actual called workflow")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("sha=" + sha + "\n")


def render_bootstrap():
    """Standalone stdlib source runs before fetching any checker files."""
    return ("import json, os, re, subprocess\n\n" + inspect.getsource(resolve) + "\n"
            + inspect.getsource(bootstrap) + "\nbootstrap()\n")


def checker_namespace(value, *, repo, workflow, source_sha):
    """Prove the canonical runtime checker mount belongs to this source.

    Dependency readers may map a local .control-plane action back into the
    immutable called workflow's tree only after proving this binding. The
    canonical bootstrap refuses missing/wrong native called-workflow identity.
    Merely seeing a path or a checkout step does not establish that binding.
    """
    if (repo != "stromy-org/stromy-org" or workflow != ".github/workflows/ci-python.yml"
            or not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha)
            or not isinstance(value, dict) or not isinstance(value.get("jobs"), dict)):
        return False
    expected = ("# GENERATED bootstrap: scripts/ci/workflow_identity.py\n"
                "python3 - <<'PY'\n" + render_bootstrap() + "PY\n")
    found = False
    for job in value["jobs"].values():
        if not isinstance(job, dict) or not isinstance(job.get("steps", []), list):
            return False
        resolved = mounted = False
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                return False
            if step.get("id") == "checker-revision":
                env = step.get("env")
                resolved = (step.get("run") == expected and isinstance(env, dict)
                    and set(env) == {"GH_TOKEN", "CHECKER_WORKFLOW"}
                    and env.get("CHECKER_WORKFLOW") == repo + "/" + workflow
                    and env.get("GH_TOKEN") == "${{ github.token }}"
                    and not step.get("continue-on-error", False))
                mounted = False
            checkout = step.get("with")
            if (isinstance(checkout, dict) and checkout.get("path") == ".control-plane"):
                mounted = (resolved and isinstance(step.get("uses"), str)
                    and re.fullmatch(r"actions/checkout@[0-9a-f]{40}", step["uses"]) is not None
                    and checkout.get("repository") == repo
                    and checkout.get("ref") == "${{ steps.checker-revision.outputs.sha }}"
                    and checkout.get("persist-credentials") is False
                    and not step.get("continue-on-error", False))
            use = step.get("uses")
            if isinstance(use, str) and use.startswith("./.control-plane/"):
                if not mounted:
                    return False
                found = True
    return found


def render_workflows(root=ROOT, *, check=False):
    changed = []
    updates = []
    for name, condition in TARGETS.items():
        path = root / ".github/workflows" / name
        source = path.read_text()
        step = ("      - name: Resolve the concrete shared-check revision\n"
                f"        if: {condition}\n"
                + ("        continue-on-error: true\n" if name == "explain-pin.yml" else "")
                + "        id: checker-revision\n"
                "        env:\n"
                "          GH_TOKEN: ${{ github.token }}\n"
                f"          CHECKER_WORKFLOW: stromy-org/stromy-org/.github/workflows/{name}\n"
                + ("          CHECKER_EXPECTED_REF: ${{ inputs.engine-ref }}\n" if name == "copier-update.yml" else "")
                + "        run: |\n"
                "          # GENERATED bootstrap: scripts/ci/workflow_identity.py\n"
                "          python3 - <<'PY'\n"
                + "".join(("          " + line if line else "") + "\n" for line in render_bootstrap().splitlines())
                + "          PY\n")
        start = source.find("      - name: Resolve the concrete shared-check revision\n")
        if start >= 0:
            end = source.find("      - ", start + 10)
            if end < 0:
                raise ValueError("checker resolver must precede a checkout step")
            updated = source[:start] + step + source[end:]
        elif name in {"parent-contract.yml", "explain-pin.yml", "copier-update.yml"}:
            marker = {"parent-contract.yml": "      - name: Checkout stromy-org (shallow, no submodules)\n",
                      "explain-pin.yml": "      - uses: actions/checkout@",
                      "copier-update.yml": "      - name: Checkout satellite\n"}[name]
            if source.count(marker) != 1:
                raise ValueError("parent checker insertion point unavailable")
            updated = source.replace(marker, step + marker, 1)
        else:
            raise ValueError("checker resolver insertion point unavailable")
        if updated != source:
            changed.append(name)
            updates.append((path, updated))
    if not check:
        for path, updated in updates:
            path.write_text(updated)
    return changed


def bind_parent(checker: Path, data: Path, sha: str):
    """Overlay released helpers into the live tree without replacing its contracts.

    Root-derived checker paths keep seeing the current catalog/ledgers and the
    caller mount. The separate checker checkout remains intact for verification.
    Both identities and every copied helper digest are retained before execution.
    """
    checker, data = checker.resolve(), data.resolve()
    if checker == data or checker.is_relative_to(data) or data.is_relative_to(checker):
        raise ValueError("checker and data checkouts must be independent")
    def head(root):
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10, check=True)
        value = result.stdout.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("checkout identity unavailable")
        return value
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or head(checker) != sha:
        raise ValueError("released checker checkout identity differs")
    data_sha = head(data)
    status = subprocess.run(["git", "-C", str(data), "status", "--porcelain=v1", "--untracked-files=all"],
                            capture_output=True, text=True, timeout=15, check=True)
    if status.stdout.strip():
        raise ValueError("live contract data is not a fresh clean checkout")
    subprocess.run(["git", "-C", str(checker), "diff", "--exit-code", sha, "--", "scripts"],
                   capture_output=True, timeout=15, check=True)
    tree = subprocess.run(["git", "-C", str(checker), "ls-tree", "-rz", sha, "--", "scripts"],
                          capture_output=True, text=True, timeout=15, check=True).stdout
    files = {}
    total = 0
    for entry in tree.split("\0"):
        if not entry:
            continue
        metadata, rel = entry.split("\t", 1)
        mode, kind, _oid = metadata.split()
        if (mode not in {"100644", "100755"} or kind != "blob" or not rel.startswith("scripts/")
                or ".." in Path(rel).parts or rel in files):
            raise ValueError("checker helper entry is not a regular unique source")
        path = checker / rel
        if (not path.is_file() or any(p.is_symlink() for p in (path, *path.parents) if p != checker.parent)
                or not path.resolve().is_relative_to(checker)):
            raise ValueError("checker helper closure unavailable or symlinked")
        total += path.stat().st_size
        if total > 64_000_000 or path.stat().st_size > 8_000_000:
            raise ValueError("checker helper closure exceeds size bound")
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or len(files) > 10_000:
        raise ValueError("checker helper closure empty or over bound")
    # Preserve the prior code outside the evaluation tree. Replacing the whole
    # directory prevents removed released helpers surviving as stale imports.
    destination = data / "scripts"
    prior = data.parent / "prior-parent-contract-scripts"
    if destination.is_symlink():
        raise ValueError("live helper destination is a symlink")
    if prior.exists() or prior.is_symlink():
        raise ValueError("prior helper checkpoint already exists")
    if destination.exists():
        destination.rename(prior)
    for rel in files:
        target = data / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checker / rel, target)
    record = {"schema": 1, "checker_sha": sha, "data_sha": data_sha, "files": files}
    (data / ".checker-identity.json").write_text(json.dumps(record, sort_keys=True) + "\n")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    render = sub.add_parser("render")
    render.add_argument("--check", action="store_true")
    bind = sub.add_parser("bind-parent")
    bind.add_argument("--checker", type=Path, required=True)
    bind.add_argument("--data", type=Path, required=True)
    bind.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    if args.command == "render":
        changed = render_workflows(check=args.check)
        print("checker bootstrap " + ("stale: " + ", ".join(changed) if args.check and changed else "current"))
        return int(args.check and bool(changed))
    bind_parent(args.checker, args.data, args.sha)
    print("released checker bound to fresh contract data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
