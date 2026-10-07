"""Portable suite declarations and actual JUnit execution counts.

Vendored verbatim by Copier. No GitHub, private control-plane or metadata
command execution is needed; both public and private runners use these rules.
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

CI_INPUTS = ("scripts/ci/test-contract.schema.json",)
CI_DELEGATES_TO = ("scripts/ci_trigger_paths.py",)
CONTRACT_PATH = ".github/test-contract.yml"
SCHEMA_PATH = Path(__file__).with_name("test-contract.schema.json")


class ContractError(ValueError):
    """An opted-in contract cannot be interpreted safely."""


def load_contract(repo: Path) -> dict[str, Any] | None:
    """Validate the opt-in document; missing dependencies are unavailable evidence."""
    path = repo / CONTRACT_PATH
    if not path.exists():
        return None
    import jsonschema
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(schema).validate(data)
    except (OSError, ValueError, yaml.YAMLError, jsonschema.ValidationError) as exc:
        raise ContractError(str(exc)) from exc
    ids = [s["id"] for s in data["suites"]]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate suite identity")
    if not ids and not str(data.get("not_applicable_reason", "")).strip():
        raise ContractError("an empty contract needs not_applicable_reason")
    if ids and "not_applicable_reason" in data:
        raise ContractError("suite declarations conflict with not_applicable_reason")
    for suite in data["suites"]:
        for field in ("owner", "receiver", "exclusion_reason"):
            if field in suite and not suite[field].strip():
                raise ContractError(f"{suite['id']}: empty {field}")
    return data


def execution_verdict(suite: dict[str, Any], counts: dict[str, int] | None) -> str:
    """pass / fail / unavailable / not-applicable, with skips never execution."""
    if counts is None:
        return "unavailable"
    keys = ("collected", "executed", "skipped", "failed")
    if any(type(counts.get(k)) is not int or counts[k] < 0 for k in keys):
        return "unavailable"
    if (
        counts["executed"] + counts["skipped"] != counts["collected"]
        or counts["failed"] > counts["executed"]
    ):
        return "unavailable"
    if counts["failed"]:
        return "fail"
    if counts["collected"] == 0 and not suite["required"]:
        return "not-applicable"
    return "pass" if counts["executed"] >= suite["minimum_executed"] else "fail"


def junit_counts(
    path: Path, suites: list[dict[str, Any]], *, repo: Path | None = None
) -> dict[str, dict[str, int]]:
    """Count actual test cases by their declared path; errors count as failures."""
    if not path.is_file():
        raise ValueError(f"required execution report unavailable: {path}")
    # Only local trusted test-runner output is accepted; no XML is fetched here.
    root = ET.parse(path).getroot()
    repo = (repo or Path.cwd()).resolve()
    out = {
        s["id"]: {"collected": 0, "executed": 0, "skipped": 0, "failed": 0}
        for s in suites
    }
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ci_trigger_paths

    for case in root.iter("testcase"):
        module = case.get("classname", "").replace(".", "/")
        candidate_paths = [
            case.get("file", ""),
            case.get("classname", "").removeprefix("./"),
            module + ".py",
            module.rsplit("/", 1)[0] + ".py",
        ]
        # Vitest can emit absolute filenames; only this checkout's files count.
        normalized = []
        for candidate in candidate_paths:
            if Path(candidate).is_absolute():
                try:
                    candidate = Path(candidate).resolve().relative_to(repo).as_posix()
                except ValueError:
                    continue
            normalized.append(candidate)
        for suite in suites:
            if any(
                p and any(ci_trigger_paths.matches(p, g) for g in suite["paths"])
                for p in normalized
            ):
                counts = out[suite["id"]]
                counts["collected"] += 1
                skipped = case.find("skipped") is not None
                counts["skipped" if skipped else "executed"] += 1
                counts["failed"] += int(
                    case.find("failure") is not None or case.find("error") is not None
                )
    return out
