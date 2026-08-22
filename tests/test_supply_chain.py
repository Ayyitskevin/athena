"""Supply-chain evidence must cover the exact pinned graph and fail closed."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_supply_chain.py"
SPEC = importlib.util.spec_from_file_location("athena_check_supply_chain", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_supply_chain = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_supply_chain
SPEC.loader.exec_module(check_supply_chain)


def _fake_repo(
    root: Path,
    *,
    ci: str = "fastapi==1.0\nJinja2==2.0\n",
    bootstrap: str = (
        "--only-binary :all:\n"
        "pip==26.1.2 \\\n"
        "  --hash=sha256:"
        "382ff9f685ee3bc25864f820aa50505825f10f5458ffff07e30a6d96e5715cab\n"
    ),
    build: str = 'requires = ["setuptools==83.0.0"]',
) -> Path:
    constraints = root / "constraints"
    constraints.mkdir(parents=True)
    (constraints / "ci-py312.txt").write_text(ci, encoding="utf-8")
    (constraints / "bootstrap-py312.txt").write_text(bootstrap, encoding="utf-8")
    (root / "pyproject.toml").write_text(
        f'[build-system]\n{build}\nbuild-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )
    return root


def _sbom(root: Path, **overrides: Any) -> dict[str, Any]:
    dependencies = check_supply_chain.expected_dependencies(root)
    document: dict[str, Any] = {
        "$schema": check_supply_chain.CYCLONEDX_SCHEMA,
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000000",
        "version": 1,
        "components": [
            {
                "type": "library",
                "name": dependency.name,
                "version": dependency.version,
            }
            for dependency in reversed(dependencies.values())
        ],
    }
    document.update(overrides)
    return document


def _write_sbom(path: Path, document: dict[str, Any]) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_current_repository_has_exact_ci_build_and_bootstrap_subjects():
    assert len(check_supply_chain.read_pins(ROOT / "constraints/ci-py312.txt")) == 63
    dependencies = check_supply_chain.expected_dependencies(ROOT)

    assert len(dependencies) == 65
    assert dependencies["pip"].version == "26.2"
    assert dependencies["setuptools"].version == "83.0.0"
    assert dependencies["fastapi"].source.endswith("constraints/ci-py312.txt")


@pytest.mark.parametrize(
    "bad_requirement",
    [
        "fastapi>=1.0",
        "fastapi",
        "fastapi==1.*",
        "fastapi==1,>=0",
        "fastapi===1",
        "fastapi==https://example.invalid/archive",
        "fastapi @ https://example.invalid/fastapi.whl",
        "fastapi==1.0; python_version >= '3.12'",
        "-e ../fastapi",
    ],
)
def test_unpinned_or_ambiguous_requirements_are_rejected(tmp_path, bad_requirement):
    repo = _fake_repo(tmp_path, ci=f"{bad_requirement}\n")

    with pytest.raises(ValueError, match="exact NAME==VERSION pin"):
        check_supply_chain.expected_dependencies(repo)


def test_bootstrap_pin_requires_a_sha256_hash(tmp_path):
    repo = _fake_repo(tmp_path, bootstrap="pip==26.1.2\n")

    with pytest.raises(ValueError, match="missing a SHA-256 hash"):
        check_supply_chain.expected_dependencies(repo)


def test_duplicate_subjects_across_inputs_are_rejected(tmp_path):
    repo = _fake_repo(tmp_path, ci="pip==26.1.2\n")

    with pytest.raises(ValueError, match="duplicate audit subject"):
        check_supply_chain.expected_dependencies(repo)


def test_build_requirement_must_be_an_exact_pin(tmp_path):
    repo = _fake_repo(tmp_path, build='requires = ["setuptools>=83"]')

    with pytest.raises(ValueError, match="exact NAME==VERSION pin"):
        check_supply_chain.expected_dependencies(repo)


def test_current_evidence_tool_lock_is_exact_and_matches_direct_inputs():
    dependencies = check_supply_chain.evidence_tool_dependencies(ROOT)

    assert len(dependencies) == 32
    assert dependencies["build"].version == "1.5.0"
    assert dependencies["packaging"].version == "26.2"
    assert dependencies["pip"].version == "26.2"
    assert dependencies["pip-audit"].version == "2.10.1"
    assert dependencies["setuptools"].version == "83.0.0"


def test_evidence_tool_direct_pin_must_match_lock(tmp_path):
    constraints = tmp_path / "constraints"
    constraints.mkdir()
    (constraints / "security-tools.in").write_text(
        "pip-audit==2.10.1\n", encoding="utf-8"
    )
    (constraints / "security-tools-py312.txt").write_text(
        (f"--only-binary :all:\npip-audit==2.10.0 \\\n  --hash=sha256:{'0' * 64}\n"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected 2.10.1"):
        check_supply_chain.evidence_tool_dependencies(tmp_path)


def test_audit_environment_must_exactly_match_tool_lock(tmp_path, monkeypatch):
    expected = check_supply_chain.Dependency(
        name="auditor",
        version="1",
        source="test",
    )
    monkeypatch.setattr(
        check_supply_chain,
        "evidence_tool_dependencies",
        lambda _root: {"auditor": expected},
    )
    monkeypatch.setattr(
        check_supply_chain,
        "distributions",
        lambda: [SimpleNamespace(metadata={"Name": "auditor"}, version="1")],
    )

    assert check_supply_chain.verify_audit_environment(tmp_path) == 1

    monkeypatch.setattr(
        check_supply_chain,
        "distributions",
        lambda: [
            SimpleNamespace(metadata={"Name": "auditor"}, version="1"),
            SimpleNamespace(metadata={"Name": "surprise"}, version="2"),
        ],
    )
    with pytest.raises(ValueError, match="unexpected: surprise"):
        check_supply_chain.verify_audit_environment(tmp_path)


@pytest.mark.parametrize(
    ("ci_build", "backend", "message"),
    [
        ("build==1.4.0\n", 'requires = ["setuptools==83.0.0"]', "build is 1.5.0"),
        ("build==1.5.0\n", 'requires = ["setuptools==82.0.0"]', "setuptools is 83.0.0"),
    ],
)
def test_evidence_build_tools_must_match_authoritative_sources(
    tmp_path, ci_build, backend, message
):
    constraints = tmp_path / "constraints"
    constraints.mkdir()
    (constraints / "ci-py312.txt").write_text(ci_build, encoding="utf-8")
    (constraints / "security-tools.in").write_text(
        "build==1.5.0\nsetuptools==83.0.0\n",
        encoding="utf-8",
    )
    (constraints / "security-tools-py312.txt").write_text(
        (
            "--only-binary :all:\n"
            f"build==1.5.0 \\\n  --hash=sha256:{'0' * 64}\n"
            f"setuptools==83.0.0 \\\n  --hash=sha256:{'1' * 64}\n"
        ),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        f'[build-system]\n{backend}\nbuild-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        check_supply_chain.evidence_tool_dependencies(tmp_path)


def test_semantic_sbom_verification_accepts_order_and_name_spelling(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    document = _sbom(repo)
    document["components"][0]["name"] = document["components"][0]["name"].upper()
    sbom = _write_sbom(tmp_path / "sbom.json", document)

    assert check_supply_chain.verify_sbom(sbom, root=repo) == 4


@pytest.mark.parametrize("defect", ["missing", "unexpected", "changed", "duplicate"])
def test_semantic_sbom_verification_rejects_component_drift(tmp_path, defect):
    repo = _fake_repo(tmp_path / "repo")
    document = _sbom(repo)
    components = document["components"]
    if defect == "missing":
        components.pop()
    elif defect == "unexpected":
        components.append({"type": "library", "name": "surprise", "version": "1"})
    elif defect == "changed":
        components[0]["version"] = "999"
    else:
        components.append(dict(components[0]))
    sbom = _write_sbom(tmp_path / "sbom.json", document)

    with pytest.raises(ValueError):
        check_supply_chain.verify_sbom(sbom, root=repo)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("$schema", "https://example.invalid/schema", "schema"),
        ("serialNumber", "urn:uuid:not-a-uuid", "valid UUID"),
        ("version", True, "document version"),
    ],
)
def test_semantic_sbom_verification_rejects_malformed_headers(
    tmp_path, field, value, message
):
    repo = _fake_repo(tmp_path / "repo")
    document = _sbom(repo)
    document[field] = value
    sbom = _write_sbom(tmp_path / "sbom.json", document)

    with pytest.raises(ValueError, match=message):
        check_supply_chain.verify_sbom(sbom, root=repo)


def test_semantic_sbom_verification_requires_library_component_type(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    document = _sbom(repo)
    document["components"][0].pop("type")
    sbom = _write_sbom(tmp_path / "sbom.json", document)

    with pytest.raises(ValueError, match="component type"):
        check_supply_chain.verify_sbom(sbom, root=repo)


def test_semantic_sbom_verification_rejects_vulnerabilities(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    sbom = _write_sbom(
        tmp_path / "sbom.json",
        _sbom(repo, vulnerabilities=[{"id": "PYSEC-TEST-1"}]),
    )

    with pytest.raises(ValueError, match="PYSEC-TEST-1"):
        check_supply_chain.verify_sbom(sbom, root=repo)


def test_semantic_sbom_verification_rejects_malformed_vulnerability_field(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    sbom = _write_sbom(
        tmp_path / "sbom.json",
        _sbom(repo, vulnerabilities={"id": "not-a-list"}),
    )

    with pytest.raises(ValueError, match="not a list"):
        check_supply_chain.verify_sbom(sbom, root=repo)


def test_semantic_sbom_verification_rejects_empty_or_invalid_json(tmp_path):
    repo = _fake_repo(tmp_path / "repo")
    sbom = tmp_path / "sbom.json"

    sbom.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        check_supply_chain.verify_sbom(sbom, root=repo)

    sbom.write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        check_supply_chain.verify_sbom(sbom, root=repo)


def test_run_audit_uses_fail_closed_flags_and_cleans_input(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path / "repo")
    auditor = tmp_path / "audit" / "python"
    auditor.parent.mkdir()
    auditor.symlink_to(sys.executable)
    output = tmp_path / "evidence" / "sbom.json"
    observed: list[str] = []
    observed_input = ""
    observed_cwd: Path | None = None

    def fake_run(command, **kwargs):
        nonlocal observed_cwd, observed_input
        observed.extend(command)
        observed_cwd = kwargs["cwd"]
        requirement_arg = Path(command[command.index("--requirement") + 1])
        observed_input = requirement_arg.read_text(encoding="utf-8")
        output_arg = Path(command[command.index("--output") + 1])
        _write_sbom(output_arg, _sbom(repo))
        return subprocess.CompletedProcess(command, 0, "No known vulnerabilities\n", "")

    monkeypatch.setattr(check_supply_chain.subprocess, "run", fake_run)

    assert check_supply_chain.run_audit(auditor, output, root=repo) == 4
    assert observed[:4] == [str(auditor), "-I", "-m", "pip_audit"]
    for required in (
        "--strict",
        "--no-deps",
        "--disable-pip",
        "--progress-spinner",
        "--timeout",
        "--vulnerability-service",
        "--format",
        "cyclonedx-json",
        "--requirement",
    ):
        assert required in observed
    assert observed_cwd == output.parent.resolve()
    assert observed_input.splitlines() == [
        "fastapi==1.0",
        "Jinja2==2.0",
        "pip==26.1.2",
        "setuptools==83.0.0",
    ]
    assert not list(output.parent.glob(".athena-audit.*.txt"))
    assert not list(output.parent.glob(".*.candidate"))


def test_run_audit_propagates_service_failure_without_stale_output(
    tmp_path, monkeypatch
):
    repo = _fake_repo(tmp_path / "repo")
    output = tmp_path / "evidence" / "sbom.json"
    output.parent.mkdir(parents=True)
    output.write_text("stale", encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "", "service unavailable\n")

    monkeypatch.setattr(check_supply_chain.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="without producing"):
        check_supply_chain.run_audit(Path("/audit/python"), output, root=repo)
    assert not output.exists()
    assert not list(output.parent.glob(".athena-audit.*.txt"))
    assert not list(output.parent.glob(".*.candidate"))


def test_run_audit_rejects_nonzero_exit_with_structurally_clean_sbom(
    tmp_path, monkeypatch
):
    repo = _fake_repo(tmp_path / "repo")
    output = tmp_path / "evidence" / "sbom.json"

    def fake_run(command, **kwargs):
        output_arg = Path(command[command.index("--output") + 1])
        _write_sbom(output_arg, _sbom(repo))
        return subprocess.CompletedProcess(command, 1, "", "scanner failed\n")

    monkeypatch.setattr(check_supply_chain.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="exit code 1"):
        check_supply_chain.run_audit(Path("/audit/python"), output, root=repo)
    assert not output.exists()
    assert not list(output.parent.glob(".*.candidate"))


def test_run_audit_surfaces_advisory_identity(tmp_path, monkeypatch):
    repo = _fake_repo(tmp_path / "repo")
    output = tmp_path / "evidence" / "sbom.json"

    def fake_run(command, **kwargs):
        output_arg = Path(command[command.index("--output") + 1])
        _write_sbom(
            output_arg,
            _sbom(repo, vulnerabilities=[{"id": "PYSEC-TEST-2"}]),
        )
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(check_supply_chain.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="PYSEC-TEST-2"):
        check_supply_chain.run_audit(Path("/audit/python"), output, root=repo)
    assert not output.exists()
    assert not list(output.parent.glob(".athena-audit.*.txt"))
    assert not list(output.parent.glob(".*.candidate"))


def test_run_audit_refuses_checkout_output_or_interpreter(tmp_path):
    repo = _fake_repo(tmp_path / "repo")

    with pytest.raises(ValueError, match="output must stay outside"):
        check_supply_chain.run_audit(
            Path("/audit/python"),
            repo / "sbom.json",
            root=repo,
        )
    with pytest.raises(ValueError, match="interpreter must stay outside"):
        check_supply_chain.run_audit(
            repo / ".audit-venv" / "bin" / "python",
            tmp_path / "sbom.json",
            root=repo,
        )


def test_hash_lock_requires_binary_only_policy(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text(
        f"subject==1 \\\n  --hash=sha256:{'0' * 64}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required --only-binary"):
        check_supply_chain.read_pins(
            lock,
            require_hashes=True,
            require_only_binary=True,
        )


def test_run_audit_rejects_output_symlink_without_touching_target(
    tmp_path, monkeypatch
):
    repo = _fake_repo(tmp_path / "repo")
    target = tmp_path / "victim.txt"
    target.write_text("preserve me", encoding="utf-8")
    output = tmp_path / "evidence.json"
    output.symlink_to(target)
    monkeypatch.setattr(
        check_supply_chain.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("scanner must not run"),
    )

    with pytest.raises(ValueError, match="regular, non-symlink"):
        check_supply_chain.run_audit(
            Path("/audit/python"),
            output,
            root=repo,
        )

    assert target.read_text(encoding="utf-8") == "preserve me"
    assert output.is_symlink()


def test_run_audit_rejects_output_alias_without_deleting_interpreter(
    tmp_path, monkeypatch
):
    repo = _fake_repo(tmp_path / "repo")
    auditor = tmp_path / "auditor-python"
    auditor.write_bytes(b"preserve executable")
    monkeypatch.setattr(
        check_supply_chain.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("scanner must not run"),
    )

    with pytest.raises(ValueError, match="aliases a protected input"):
        check_supply_chain.run_audit(
            auditor,
            auditor,
            root=repo,
        )

    assert auditor.read_bytes() == b"preserve executable"
