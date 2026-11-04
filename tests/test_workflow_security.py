from __future__ import annotations

from collections import deque
from collections.abc import Iterator
import re
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CODEQL_WORKFLOW = WORKFLOWS / "codeql.yml"
CODEQL_CONFIG = ROOT / ".github" / "codeql" / "codeql-config.yml"
EXTERNAL_ACTION = re.compile(r"^[^@\s]+@([0-9a-f]{40})$")


def _mapping_action_reference(value: object) -> str | None:
    assert isinstance(value, dict), "workflow jobs and action steps must be mappings"
    reference = value.get("uses")
    assert reference is None or isinstance(reference, str), (
        "an action reference must be a string"
    )
    return reference


def _step_action_references(value: object) -> Iterator[str]:
    if value is None:
        return
    assert isinstance(value, list), "workflow and composite-action steps must be lists"
    for step in value:
        reference = _mapping_action_reference(step)
        if reference is not None:
            yield reference


def _action_references(document: object) -> Iterator[str]:
    assert isinstance(document, dict), "workflow and action documents must be mappings"

    jobs = document.get("jobs", {})
    assert isinstance(jobs, dict), "workflow jobs must be a mapping"
    for job in jobs.values():
        reference = _mapping_action_reference(job)
        if reference is not None:
            yield reference
        assert isinstance(job, dict)
        yield from _step_action_references(job.get("steps"))

    runs = document.get("runs")
    if runs is not None:
        assert isinstance(runs, dict), "action runs must be a mapping"
        yield from _step_action_references(runs.get("steps"))


def _read_action_references(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(_action_references(document))


def _assert_codeql_init_analyze_share_full_commit(workflow: str) -> None:
    document = yaml.safe_load(workflow)
    references = list(_action_references(document))
    commits: dict[str, str] = {}

    for step in ("init", "analyze"):
        prefix = f"github/codeql-action/{step}@"
        matches = [
            reference for reference in references if reference.startswith(prefix)
        ]
        assert len(matches) == 1, (
            f"CodeQL {step} step must appear exactly once; found {len(matches)}"
        )
        match = EXTERNAL_ACTION.fullmatch(matches[0])
        assert match is not None, (
            f"CodeQL {step} must use a full 40-hex commit SHA, got {matches[0]}"
        )
        commits[step] = match.group(1)

    assert commits["init"] == commits["analyze"], (
        "CodeQL init and analyze must use the same commit SHA"
    )


def _codeql_workflow_text(
    init_references: tuple[str, ...], analyze_references: tuple[str, ...]
) -> str:
    return yaml.safe_dump(
        {
            "jobs": {
                "analyze": {
                    "steps": [
                        {"uses": reference}
                        for reference in (*init_references, *analyze_references)
                    ]
                }
            }
        }
    )


def _local_action_document(reference: str, *, root: Path = ROOT) -> Path:
    target = (root / reference.removeprefix("./")).resolve()
    assert target.is_relative_to(root.resolve()), (
        f"local action reference escapes the repository: {reference}"
    )
    if target.is_file():
        assert target.suffix in {".yaml", ".yml"}, (
            f"local reusable workflow is not YAML: {reference}"
        )
        return target

    manifests = [
        candidate
        for candidate in (target / "action.yml", target / "action.yaml")
        if candidate.is_file()
    ]
    assert len(manifests) == 1, (
        f"local action must have exactly one action.yml or action.yaml: {reference}"
    )
    return manifests[0]


def _workflow_action_references(
    *, root: Path = ROOT, workflows: Path = WORKFLOWS
) -> list[tuple[Path, str]]:
    references: list[tuple[Path, str]] = []
    pending = deque(sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]))
    visited: set[Path] = set()

    while pending:
        document = pending.popleft().resolve()
        if document in visited:
            continue
        visited.add(document)
        for reference in _read_action_references(document):
            references.append((document, reference))
            if reference.startswith("./"):
                pending.append(_local_action_document(reference, root=root))

    return references


def test_every_external_workflow_action_uses_an_immutable_commit() -> None:
    invalid = [
        f"{path.relative_to(ROOT)}: {reference}"
        for path, reference in _workflow_action_references()
        if not reference.startswith("./") and not EXTERNAL_ACTION.fullmatch(reference)
    ]

    assert invalid == [], (
        "external workflow actions must use full commit SHAs:\n" + "\n".join(invalid)
    )


def test_action_reference_reader_uses_yaml_structure() -> None:
    immutable = "owner/good@0123456789abcdef0123456789abcdef01234567"
    suffixed = [
        f"{immutable}{suffix}" for suffix in ("#mutable", ",mutable", "}mutable")
    ]
    document = yaml.safe_load(
        f"""
jobs:
  policy:
    steps:
      - {{uses: {immutable}}}
      - {{"uses": owner/evil@main}}
      - uses: {suffixed[0]}
      - uses: {suffixed[1]}
      - uses: {suffixed[2]}
      - run: |
          uses: owner/script-text@main
"""
    )

    references = list(_action_references(document))
    assert references == [immutable, "owner/evil@main", *suffixed]
    assert EXTERNAL_ACTION.fullmatch(immutable)
    assert all(
        EXTERNAL_ACTION.fullmatch(reference) is None for reference in references[1:]
    )


def test_action_reference_reader_follows_local_composite_actions(
    tmp_path: Path,
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    action = tmp_path / ".github" / "actions" / "policy"
    workflows.mkdir(parents=True)
    action.mkdir(parents=True)
    (workflows / "ci.yaml").write_text(
        """
jobs:
  policy:
    steps:
      - uses: ./.github/actions/policy
""",
        encoding="utf-8",
    )
    (action / "action.yml").write_text(
        """
name: Policy
runs:
  using: composite
  steps:
    - uses: owner/evil@main
""",
        encoding="utf-8",
    )

    references = _workflow_action_references(root=tmp_path, workflows=workflows)

    assert references == [
        (workflows / "ci.yaml", "./.github/actions/policy"),
        (action / "action.yml", "owner/evil@main"),
    ]


def test_codeql_workflow_has_a_bounded_write_authority() -> None:
    workflow = CODEQL_WORKFLOW.read_text(encoding="utf-8")

    assert "pull_request_target" not in workflow
    assert "\npermissions:\n  contents: read\n" in workflow
    assert (
        "\n    permissions:\n"
        "      contents: read\n"
        "      security-events: write\n" in workflow
    )
    assert "persist-credentials: false" in workflow
    assert "id-token: write" not in workflow
    assert "contents: write" not in workflow
    assert "pull-requests: write" not in workflow
    assert "secrets." not in workflow


def test_codeql_workflow_scans_first_party_languages_on_every_gate() -> None:
    workflow = CODEQL_WORKFLOW.read_text(encoding="utf-8")

    assert "  push:\n    branches: [main]\n" in workflow
    assert "  pull_request:\n    branches: [main]\n" in workflow
    assert "  schedule:\n" in workflow
    assert "  workflow_dispatch:\n" in workflow
    assert "language: [actions, javascript-typescript, python]" in workflow
    assert "autobuild" not in workflow
    assert "upload: always" in workflow
    assert "wait-for-processing: true" in workflow
    _assert_codeql_init_analyze_share_full_commit(workflow)


@pytest.mark.parametrize(
    ("init_references", "analyze_references", "message"),
    [
        (
            ("github/codeql-action/init@v4",),
            ("github/codeql-action/analyze@" + "a" * 40,),
            "full 40-hex",
        ),
        (
            ("github/codeql-action/init@a1b2c3d4",),
            ("github/codeql-action/analyze@" + "a" * 40,),
            "full 40-hex",
        ),
        (
            ("github/codeql-action/init@" + "a" * 40,),
            ("github/codeql-action/analyze@" + "b" * 40,),
            "same commit SHA",
        ),
        (
            (
                "github/codeql-action/init@" + "a" * 40,
                "github/codeql-action/init@" + "a" * 40,
            ),
            ("github/codeql-action/analyze@" + "a" * 40,),
            "appear exactly once",
        ),
        (
            (),
            ("github/codeql-action/analyze@" + "a" * 40,),
            "appear exactly once",
        ),
    ],
    ids=("movable-tag", "short-sha", "split-commits", "duplicate-init", "missing-init"),
)
def test_codeql_init_analyze_pair_rejects_unpinned_or_incomplete_workflows(
    init_references: tuple[str, ...],
    analyze_references: tuple[str, ...],
    message: str,
) -> None:
    workflow = _codeql_workflow_text(init_references, analyze_references)

    with pytest.raises(AssertionError, match=message):
        _assert_codeql_init_analyze_share_full_commit(workflow)


def test_codeql_config_covers_declared_sources() -> None:
    config = CODEQL_CONFIG.read_text(encoding="utf-8")

    assert "  - uses: security-extended\n" in config
    for path in (".github", "examples", "scripts", "src"):
        assert f"  - {path}\n" in config
    assert "paths-ignore:\n  - tests\n" in config
    assert "  - src/athena/static/htmx.min.js\n" in config
