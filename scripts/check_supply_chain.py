"""Build and verify fail-closed Python advisory evidence for Athena."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib.metadata import distributions
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from typing import Any
from uuid import UUID

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version


REPO_ROOT = Path(__file__).resolve().parents[1]
CI_CONSTRAINTS = Path("constraints/ci-py312.txt")
BOOTSTRAP_CONSTRAINTS = Path("constraints/bootstrap-py312.txt")
SECURITY_TOOLS_INPUT = Path("constraints/security-tools.in")
SECURITY_TOOLS_LOCK = Path("constraints/security-tools-py312.txt")
PYPROJECT = Path("pyproject.toml")
MAX_SBOM_BYTES = 10 * 1024 * 1024
_HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")
CYCLONEDX_SCHEMA = "http://cyclonedx.org/schema/bom-1.4.schema.json"


@dataclass(frozen=True)
class Dependency:
    name: str
    version: str
    source: str


def normalize_name(name: str) -> str:
    """Return the package-name comparison form defined by Python packaging."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _logical_lines(path: Path) -> list[str]:
    logical: list[str] = []
    pending = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        content = raw_line.split("#", 1)[0].strip()
        if not content:
            continue
        pending = f"{pending} {content}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        logical.append(pending)
        pending = ""
    if pending:
        raise ValueError(f"{path}: unterminated line continuation")
    return logical


def _parse_pin(value: str, *, source: str) -> Dependency:
    try:
        requirement = Requirement(value)
    except InvalidRequirement as exc:
        raise ValueError(
            f"{source}: expected one exact NAME==VERSION pin, got {value!r}"
        ) from exc

    specifiers = list(requirement.specifier)
    if (
        requirement.extras
        or requirement.marker is not None
        or requirement.url is not None
        or len(specifiers) != 1
        or specifiers[0].operator != "=="
        or "*" in specifiers[0].version
    ):
        raise ValueError(
            f"{source}: expected one exact NAME==VERSION pin, got {value!r}"
        )
    version = specifiers[0].version
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(
            f"{source}: expected one exact NAME==VERSION pin, got {value!r}"
        ) from exc
    return Dependency(name=requirement.name, version=version, source=source)


def read_pins(path: Path, *, require_hashes: bool = False) -> list[Dependency]:
    """Read exact requirement pins, rejecting ambiguous requirement syntax."""
    dependencies: list[Dependency] = []
    seen: set[str] = set()
    for logical_line in _logical_lines(path):
        if logical_line.startswith("--"):
            if logical_line == "--only-binary :all:":
                continue
            raise ValueError(f"{path}: unsupported global option {logical_line!r}")

        tokens = shlex.split(logical_line)
        dependency = _parse_pin(tokens[0], source=str(path))
        hashes = tokens[1:]
        if any(_HASH.fullmatch(token) is None for token in hashes):
            raise ValueError(
                f"{path}: unsupported requirement option in {logical_line!r}"
            )
        if require_hashes and not hashes:
            raise ValueError(f"{path}: {dependency.name} is missing a SHA-256 hash")

        normalized = normalize_name(dependency.name)
        if normalized in seen:
            raise ValueError(f"{path}: duplicate requirement {dependency.name!r}")
        seen.add(normalized)
        dependencies.append(dependency)
    if not dependencies:
        raise ValueError(f"{path}: no dependency pins found")
    return dependencies


def expected_dependencies(root: Path = REPO_ROOT) -> dict[str, Dependency]:
    """Assemble the exact CI, build, and bootstrap dependency subjects."""
    dependencies = [
        *read_pins(root / CI_CONSTRAINTS),
        *read_pins(root / BOOTSTRAP_CONSTRAINTS, require_hashes=True),
    ]

    pyproject_path = root / PYPROJECT
    document = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    build_requires = document.get("build-system", {}).get("requires")
    if not isinstance(build_requires, list) or not build_requires:
        raise ValueError(f"{pyproject_path}: build-system.requires must be nonempty")
    for value in build_requires:
        if not isinstance(value, str):
            raise ValueError(
                f"{pyproject_path}: build requirement must be a string, got {value!r}"
            )
        dependencies.append(
            _parse_pin(value, source=f"{pyproject_path}:build-system.requires")
        )

    expected: dict[str, Dependency] = {}
    for dependency in dependencies:
        normalized = normalize_name(dependency.name)
        prior = expected.get(normalized)
        if prior is not None:
            raise ValueError(
                f"duplicate audit subject {dependency.name!r}: "
                f"{prior.source} and {dependency.source}"
            )
        expected[normalized] = dependency
    return expected


def security_tool_dependencies(root: Path = REPO_ROOT) -> dict[str, Dependency]:
    """Validate direct scanner pins against the complete hash-locked tool graph."""
    direct = read_pins(root / SECURITY_TOOLS_INPUT)
    locked = read_pins(root / SECURITY_TOOLS_LOCK, require_hashes=True)
    locked_by_name = {
        normalize_name(dependency.name): dependency for dependency in locked
    }
    for dependency in direct:
        normalized = normalize_name(dependency.name)
        locked_dependency = locked_by_name.get(normalized)
        if locked_dependency is None:
            raise ValueError(
                f"{SECURITY_TOOLS_LOCK}: missing direct tool {dependency.name!r}"
            )
        if locked_dependency.version != dependency.version:
            raise ValueError(
                f"{SECURITY_TOOLS_LOCK}: {dependency.name} is "
                f"{locked_dependency.version}, expected {dependency.version}"
            )
    return locked_by_name


def verify_audit_environment(root: Path = REPO_ROOT) -> int:
    """Require the running interpreter to contain exactly the scanner lock."""
    expected = security_tool_dependencies(root)
    actual: dict[str, str] = {}
    for distribution in distributions():
        name = distribution.metadata.get("Name")
        if not name:
            raise ValueError("installed distribution has no Name metadata")
        normalized = normalize_name(name)
        if normalized in actual:
            raise ValueError(f"audit environment duplicates distribution {name!r}")
        actual[normalized] = distribution.version

    expected_versions = {
        normalized: dependency.version for normalized, dependency in expected.items()
    }
    missing = sorted(set(expected_versions) - set(actual))
    unexpected = sorted(set(actual) - set(expected_versions))
    changed = sorted(
        name
        for name in set(expected_versions) & set(actual)
        if expected_versions[name] != actual[name]
    )
    if missing or unexpected or changed:
        details = ["audit environment differs from the hash-locked tool graph"]
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        if changed:
            details.append(
                "version mismatch: "
                + ", ".join(
                    f"{name} expected {expected_versions[name]}, got {actual[name]}"
                    for name in changed
                )
            )
        raise ValueError("\n".join(details))
    return len(expected)


def _component_versions(document: dict[str, Any]) -> dict[str, str]:
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("CycloneDX document has no component list")

    actual: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("CycloneDX component is not an object")
        name = component.get("name")
        version = component.get("version")
        if component.get("type") != "library":
            raise ValueError("CycloneDX component type is not 'library'")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(version, str)
            or not version
        ):
            raise ValueError("CycloneDX component lacks a string name or version")
        normalized = normalize_name(name)
        if normalized in actual:
            raise ValueError(f"CycloneDX document duplicates component {name!r}")
        actual[normalized] = version
    return actual


def verify_sbom(sbom_path: Path, *, root: Path = REPO_ROOT) -> int:
    """Require the SBOM to describe every expected subject and no vulnerability."""
    size = sbom_path.stat().st_size
    if size == 0:
        raise ValueError(f"{sbom_path}: SBOM is empty")
    if size > MAX_SBOM_BYTES:
        raise ValueError(f"{sbom_path}: SBOM exceeds {MAX_SBOM_BYTES} bytes")

    document = json.loads(sbom_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("CycloneDX document root is not an object")
    if document.get("$schema") != CYCLONEDX_SCHEMA:
        raise ValueError("SBOM does not declare the CycloneDX 1.4 JSON schema")
    if document.get("bomFormat") != "CycloneDX":
        raise ValueError("SBOM format is not CycloneDX")
    if document.get("specVersion") != "1.4":
        raise ValueError(
            f"unexpected CycloneDX spec version {document.get('specVersion')!r}"
        )
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError(
            f"unexpected CycloneDX document version {document.get('version')!r}"
        )
    serial_number = document.get("serialNumber")
    if not isinstance(serial_number, str) or not serial_number.startswith("urn:uuid:"):
        raise ValueError("CycloneDX document lacks a UUID serial number")
    try:
        parsed_serial = UUID(serial_number.removeprefix("urn:uuid:"))
    except ValueError as exc:
        raise ValueError("CycloneDX serial number is not a valid UUID") from exc
    if serial_number != f"urn:uuid:{parsed_serial}":
        raise ValueError("CycloneDX serial number is not a canonical UUID URN")

    expected = expected_dependencies(root)
    actual = _component_versions(document)
    expected_versions = {
        normalized: dependency.version for normalized, dependency in expected.items()
    }
    missing = sorted(set(expected_versions) - set(actual))
    unexpected = sorted(set(actual) - set(expected_versions))
    changed = sorted(
        name
        for name in set(expected_versions) & set(actual)
        if expected_versions[name] != actual[name]
    )
    if missing or unexpected or changed:
        details = ["CycloneDX components differ from the audited dependency inputs"]
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        if changed:
            details.append(
                "version mismatch: "
                + ", ".join(
                    f"{name} expected {expected_versions[name]}, got {actual[name]}"
                    for name in changed
                )
            )
        raise ValueError("\n".join(details))

    vulnerabilities = document.get("vulnerabilities", [])
    if not isinstance(vulnerabilities, list):
        raise ValueError("CycloneDX vulnerabilities field is not a list")
    if vulnerabilities:
        identifiers = sorted(
            {
                str(vulnerability.get("id", "<missing-id>"))
                if isinstance(vulnerability, dict)
                else "<malformed>"
                for vulnerability in vulnerabilities
            }
        )
        raise ValueError(
            "CycloneDX evidence contains vulnerabilities: " + ", ".join(identifiers)
        )
    return len(expected)


def _write_audit_input(path: Path, dependencies: dict[str, Dependency]) -> None:
    lines = [
        f"{dependencies[name].name}=={dependencies[name].version}"
        for name in sorted(dependencies)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_audit(
    auditor_python: Path,
    output: Path,
    *,
    root: Path = REPO_ROOT,
    timeout_seconds: int = 30,
) -> int:
    """Run pinned pip-audit and semantically verify its CycloneDX result."""
    resolved_root = root.resolve()
    absolute_auditor = auditor_python.absolute()
    if absolute_auditor.is_relative_to(resolved_root) or auditor_python.resolve(
        strict=False
    ).is_relative_to(resolved_root):
        raise ValueError("audit interpreter must stay outside the checkout")
    resolved_output = output.resolve()
    if resolved_output.is_relative_to(resolved_root):
        raise ValueError("supply-chain evidence output must stay outside the checkout")
    if timeout_seconds <= 0:
        raise ValueError("audit timeout must be positive")
    output = resolved_output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    candidate = output.with_name(f".{output.name}.candidate")
    candidate.unlink(missing_ok=True)

    dependencies = expected_dependencies(root)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=".athena-audit.",
            suffix=".txt",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
        _write_audit_input(temporary_path, dependencies)

        command = [
            str(absolute_auditor),
            "-I",
            "-m",
            "pip_audit",
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--progress-spinner",
            "off",
            "--timeout",
            str(timeout_seconds),
            "--vulnerability-service",
            "pypi",
            "--requirement",
            str(temporary_path),
            "--format",
            "cyclonedx-json",
            "--output",
            str(candidate),
        ]
        result = subprocess.run(
            command,
            cwd=output.parent,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

        if not candidate.is_file():
            raise RuntimeError(
                f"pip-audit exited {result.returncode} without producing evidence"
            )
        count = verify_sbom(candidate, root=root)
        if result.returncode != 0:
            raise RuntimeError(f"pip-audit failed with exit code {result.returncode}")
        candidate.replace(output)
        return count
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="audit Athena's pinned Python graph and verify CycloneDX evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the advisory audit")
    run_parser.add_argument("--auditor-python", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    run_parser.add_argument("--timeout", type=int, default=30)

    verify_parser = subparsers.add_parser(
        "verify", help="verify an existing CycloneDX file"
    )
    verify_parser.add_argument("--sbom", required=True, type=Path)

    subparsers.add_parser(
        "verify-environment",
        help="verify the running interpreter against the audit tool lock",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            count = run_audit(
                args.auditor_python,
                args.output,
                timeout_seconds=args.timeout,
            )
            print(
                f"Athena supply-chain audit passed: {count} pinned components, "
                "zero known vulnerabilities"
            )
        elif args.command == "verify":
            count = verify_sbom(args.sbom)
            print(
                f"Athena CycloneDX structure passed: {count} exact components, "
                "no reported vulnerability entries"
            )
        else:
            count = verify_audit_environment()
            print(
                f"Athena audit environment passed: {count} exact "
                "hash-locked distributions"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Athena supply-chain evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
