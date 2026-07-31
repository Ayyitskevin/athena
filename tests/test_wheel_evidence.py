"""Wheel evidence must bind one artifact to exact base and MCP closures."""

from __future__ import annotations

import base64
import csv
from hashlib import sha256
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_wheel_evidence.py"
SPEC = importlib.util.spec_from_file_location("athena_check_wheel_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_wheel_evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_wheel_evidence
SPEC.loader.exec_module(check_wheel_evidence)


def _record_hash(data: bytes) -> str:
    encoded = base64.urlsafe_b64encode(sha256(data).digest()).rstrip(b"=")
    return f"sha256={encoded.decode('ascii')}"


def _write_wheel(
    path: Path,
    *,
    name: str = "athena",
    version: str = "0.1.0a1",
    requires_python: str = ">=3.12,<3.13",
    requires: tuple[str, ...] = (),
    extras: tuple[str, ...] = ("mcp",),
    extra_members: tuple[tuple[str, bytes, int | None], ...] = (),
    bad_record_hash: bool = False,
) -> Path:
    dist_info = "athena-0.1.0a1.dist-info"
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        f"Requires-Python: {requires_python}",
    ]
    metadata_lines.extend(f"Provides-Extra: {extra}" for extra in extras)
    metadata_lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    metadata = ("\n".join(metadata_lines) + "\n\n").encode()
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: athena-test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n"
    ).encode()
    members: list[tuple[str, bytes, int | None]] = [
        ("athena/__init__.py", b'__version__ = "0.1.0a1"\n', None),
        (f"{dist_info}/METADATA", metadata, None),
        (f"{dist_info}/WHEEL", wheel_metadata, None),
        *extra_members,
    ]
    record_name = f"{dist_info}/RECORD"
    rows = []
    for member_name, data, _mode in members:
        digest = (
            "sha256=invalid" if bad_record_hash and not rows else _record_hash(data)
        )
        rows.append((member_name, digest, str(len(data))))
    rows.append((record_name, "", ""))
    record_stream = io.StringIO()
    writer = csv.writer(record_stream, lineterminator="\n")
    writer.writerows(rows)
    members.append((record_name, record_stream.getvalue().encode(), None))

    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for member_name, data, mode in members:
            if mode is None:
                archive.writestr(member_name, data)
            else:
                info = ZipInfo(member_name)
                info.external_attr = mode << 16
                archive.writestr(info, data)
    return path


def _write_sdist(
    path: Path,
    wheel_subject,
    *,
    version: str | None = None,
    include_project: bool = True,
    source_override: bytes | None = None,
    backend_version: str = "83.0.0",
    extra_source_members: tuple[tuple[str, bytes], ...] = (),
) -> Path:
    version = version or wheel_subject.version
    root = f"athena-{version}"
    if version == wheel_subject.version:
        metadata = wheel_subject.metadata_bytes
    else:
        metadata_lines = [
            "Metadata-Version: 2.4",
            "Name: athena",
            f"Version: {version}",
            f"Requires-Python: {wheel_subject.requires_python}",
        ]
        metadata_lines.extend(
            f"Provides-Extra: {extra}" for extra in sorted(wheel_subject.extras)
        )
        metadata_lines.extend(
            f"Requires-Dist: {requirement}" for requirement in wheel_subject.requires
        )
        metadata = ("\n".join(metadata_lines) + "\n\n").encode()

    def add_file(archive, name: str, data: bytes) -> None:
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))

    with tarfile.open(path, "w:gz") as archive:
        root_info = tarfile.TarInfo(root)
        root_info.type = tarfile.DIRTYPE
        archive.addfile(root_info)
        add_file(archive, f"{root}/PKG-INFO", metadata)
        if include_project:
            pyproject = (
                "[project]\n"
                'name = "athena"\n'
                f'version = "{version}"\n'
                f'requires-python = "{wheel_subject.requires_python}"\n'
                "\n[build-system]\n"
                f'requires = ["setuptools=={backend_version}"]\n'
                'build-backend = "setuptools.build_meta"\n'
                "\n[tool.setuptools.packages.find]\n"
                'where = ["src"]\n'
            ).encode()
            add_file(archive, f"{root}/pyproject.toml", pyproject)
            with ZipFile(wheel_subject.path) as wheel_archive:
                for info in wheel_archive.infolist():
                    if (
                        not info.is_dir()
                        and ".dist-info/" not in info.filename
                        and info.filename.startswith("athena/")
                    ):
                        data = wheel_archive.read(info)
                        if (
                            source_override is not None
                            and info.filename == "athena/__init__.py"
                        ):
                            data = source_override
                        add_file(
                            archive,
                            f"{root}/src/{info.filename}",
                            data,
                        )
            for name, data in extra_source_members:
                add_file(archive, f"{root}/src/athena/{name}", data)
    return path


def _write_tool_lock(path: Path, *, version: str = "83.0.0") -> Path:
    path.write_text(
        (
            "--only-binary :all:\n"
            f"setuptools=={version} \\\n"
            f"  --hash=sha256:{'0' * 64}\n"
        ),
        encoding="utf-8",
    )
    return path


def test_valid_wheel_identity_and_record_are_bound(tmp_path):
    wheel = _write_wheel(
        tmp_path / "athena-0.1.0a1-py3-none-any.whl",
        requires=("dep>=1",),
    )

    subject = check_wheel_evidence.inspect_wheel(wheel)

    assert subject.name == "athena"
    assert subject.version == "0.1.0a1"
    assert subject.digest == check_wheel_evidence._sha256(wheel)
    assert subject.requires == ("dep>=1",)


def test_valid_sdist_is_bound_to_wheel_metadata(tmp_path):
    wheel = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel_subject = check_wheel_evidence.inspect_wheel(wheel)
    sdist = _write_sdist(tmp_path / "athena-0.1.0a1.tar.gz", wheel_subject)
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")

    subject = check_wheel_evidence.inspect_sdist(
        sdist,
        wheel=wheel_subject,
        tool_lock=tool_lock,
    )

    assert subject.version == wheel_subject.version
    assert subject.build_backend_version == "83.0.0"
    assert subject.digest == check_wheel_evidence._sha256(sdist)


@pytest.mark.parametrize(
    ("wheel_kwargs", "message"),
    [
        ({"name": "other"}, "does not identify Athena"),
        ({"version": "9"}, "versions differ"),
        ({"requires_python": ">=3.13"}, "excludes"),
        ({"bad_record_hash": True}, "hash mismatch"),
        (
            {"extra_members": (("../escape", b"x", None),)},
            "unsafe member",
        ),
        (
            {"extra_members": (("athena/link", b"target", stat.S_IFLNK | 0o777),)},
            "symlink member",
        ),
    ],
)
def test_wheel_identity_members_and_record_fail_closed(tmp_path, wheel_kwargs, message):
    wheel = _write_wheel(
        tmp_path / "athena-0.1.0a1-py3-none-any.whl",
        **wheel_kwargs,
    )

    with pytest.raises(ValueError, match=message):
        check_wheel_evidence.inspect_wheel(wheel)


def test_duplicate_wheel_member_is_rejected(tmp_path):
    with pytest.warns(UserWarning, match="Duplicate name"):
        wheel = _write_wheel(
            tmp_path / "athena-0.1.0a1-py3-none-any.whl",
            extra_members=(("athena/__init__.py", b"duplicate", None),),
        )

    with pytest.raises(ValueError, match="duplicate members"):
        check_wheel_evidence.inspect_wheel(wheel)


def test_noncanonical_wheel_member_alias_is_rejected(tmp_path):
    wheel = _write_wheel(
        tmp_path / "athena-0.1.0a1-py3-none-any.whl",
        extra_members=(("athena/./alias.py", b"alias", None),),
    )

    with pytest.raises(ValueError, match="unsafe member"):
        check_wheel_evidence.inspect_wheel(wheel)


def test_high_ratio_wheel_member_is_rejected_before_reading(tmp_path):
    wheel = _write_wheel(
        tmp_path / "athena-0.1.0a1-py3-none-any.whl",
        extra_members=(("athena/bomb.bin", b"\0" * (2 * 1024 * 1024), None),),
    )

    with pytest.raises(ValueError, match="compression ratio"):
        check_wheel_evidence.inspect_wheel(wheel)


def test_sdist_rejects_invalid_archive_and_metadata_mismatch(tmp_path):
    invalid = tmp_path / "athena-0.1.0a1.tar.gz"
    invalid.write_text("not a tar archive", encoding="utf-8")
    with pytest.raises(ValueError, match="valid tar archive"):
        check_wheel_evidence.inspect_sdist(invalid)

    wheel_path = _write_wheel(
        tmp_path / "athena-0.1.0a1-py3-none-any.whl",
    )
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    mismatched = _write_sdist(
        tmp_path / "athena-0.2.0.tar.gz",
        wheel,
        version="0.2.0",
    )
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")
    with pytest.raises(ValueError, match="sdist and wheel versions differ"):
        check_wheel_evidence.inspect_sdist(
            mismatched,
            wheel=wheel,
            tool_lock=tool_lock,
        )


def test_sdist_rejects_metadata_only_archive_when_bound_to_wheel(tmp_path):
    wheel_path = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    sdist = _write_sdist(
        tmp_path / "athena-0.1.0a1.tar.gz",
        wheel,
        include_project=False,
    )
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")

    with pytest.raises(ValueError, match="required build/source members"):
        check_wheel_evidence.inspect_sdist(
            sdist,
            wheel=wheel,
            tool_lock=tool_lock,
        )


def test_sdist_rejects_payload_or_snapshot_digest_mismatch(tmp_path):
    wheel_path = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    sdist = _write_sdist(
        tmp_path / "athena-0.1.0a1.tar.gz",
        wheel,
        source_override=b"not the wheel payload\n",
    )
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")

    with pytest.raises(ValueError, match="differs from wheel payload"):
        check_wheel_evidence.inspect_sdist(
            sdist,
            wheel=wheel,
            tool_lock=tool_lock,
        )
    with pytest.raises(ValueError, match="pre-extraction snapshot"):
        check_wheel_evidence.inspect_sdist(
            sdist,
            expected_digest="0" * 64,
        )


def test_sdist_rejects_payload_omitted_from_wheel(tmp_path):
    wheel_path = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    sdist = _write_sdist(
        tmp_path / "athena-0.1.0a1.tar.gz",
        wheel,
        extra_source_members=(("omitted.py", b"not shipped\n"),),
    )
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")

    with pytest.raises(ValueError, match="omitted from wheel: athena/omitted.py"):
        check_wheel_evidence.inspect_sdist(
            sdist,
            wheel=wheel,
            tool_lock=tool_lock,
        )


@pytest.mark.parametrize("version", ["83.*", "82.0.0"])
def test_sdist_backend_pin_must_be_exact_and_match_tool_lock(tmp_path, version):
    wheel_path = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    sdist = _write_sdist(
        tmp_path / "athena-0.1.0a1.tar.gz",
        wheel,
        backend_version=version,
    )
    tool_lock = _write_tool_lock(tmp_path / "tools.txt")

    if version.endswith("*"):
        match = "exact version"
    else:
        match = "differs from the evidence tool lock"

    with pytest.raises(ValueError, match=match):
        check_wheel_evidence.inspect_sdist(
            sdist,
            wheel=wheel,
            tool_lock=tool_lock,
        )


@pytest.mark.parametrize(
    ("version", "message"),
    [
        ("83.0", "differs from the evidence tool lock"),
        ("083.0.0", "tool lock setuptools pin is not canonical"),
        ("83.0.0.0", "differs from the evidence tool lock"),
    ],
)
def test_tool_lock_setuptools_pin_must_match_sdist_exactly(tmp_path, version, message):
    wheel_path = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel = check_wheel_evidence.inspect_wheel(wheel_path)
    sdist = _write_sdist(tmp_path / "athena-0.1.0a1.tar.gz", wheel)
    tool_lock = _write_tool_lock(tmp_path / "tools.txt", version=version)

    with pytest.raises(ValueError, match=message):
        check_wheel_evidence.inspect_sdist(
            sdist,
            wheel=wheel,
            tool_lock=tool_lock,
        )


def test_sdist_rejects_hidden_oversized_pax_metadata(tmp_path):
    metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: athena\n"
        b"Version: 0.1.0a1\n"
        b"Requires-Python: >=3.12,<3.13\n\n"
    )
    sdist = tmp_path / "athena-0.1.0a1.tar.gz"
    with tarfile.open(sdist, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        root = tarfile.TarInfo("athena-0.1.0a1")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        pkg_info = tarfile.TarInfo("athena-0.1.0a1/PKG-INFO")
        pkg_info.size = len(metadata)
        pkg_info.pax_headers = {
            "comment": "A" * (check_wheel_evidence.MAX_TAR_CONTROL_MEMBER_BYTES + 1)
        }
        archive.addfile(pkg_info, io.BytesIO(metadata))

    with pytest.raises(ValueError, match="local PAX header exceeds"):
        check_wheel_evidence.inspect_sdist(sdist)


def _subject(
    name: str,
    *,
    version: str = "1",
    requires: tuple[str, ...] = (),
    extras: frozenset[str] = frozenset(),
) -> Any:
    return check_wheel_evidence.InstalledSubject(
        name=name,
        version=version,
        requires=requires,
        extras=extras,
        distribution=SimpleNamespace(),
    )


def _runtime_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    profile: str,
    mutate: str | None = None,
):
    wheel_path = tmp_path / "athena.whl"
    wheel_path.write_bytes(b"wheel-subject")
    wheel = check_wheel_evidence.WheelSubject(
        path=wheel_path,
        name="athena",
        version="0.1.0a1",
        requires_python=">=3.12,<3.13",
        requires=(
            "dep[feature]>=1",
            "base-only; extra != 'mcp'",
            "mcp-only; extra == 'mcp'",
        ),
        extras=frozenset({"mcp"}),
        metadata_bytes=b"metadata",
        digest=check_wheel_evidence._sha256(wheel_path),
        root_ref="pkg:pypi/athena@0.1.0a1",
    )
    actual = {
        "athena": _subject(
            "athena",
            version="0.1.0a1",
            requires=wheel.requires,
            extras=frozenset({"mcp"}),
        ),
        "pip": _subject("pip", version="26.1.2"),
        "dep": _subject(
            "dep",
            requires=("transitive; extra == 'feature'",),
            extras=frozenset({"feature"}),
        ),
        "transitive": _subject("transitive"),
        "base-only": _subject("base-only"),
    }
    if profile == "mcp":
        # pip resolves both the base candidate (extra="") and ExtrasCandidate, so
        # a negative-extra base dependency remains part of the effective union.
        actual["mcp-only"] = _subject("mcp-only")
    if mutate == "missing-transitive":
        actual.pop("transitive")
    elif mutate == "orphan":
        actual["orphan"] = _subject("orphan")
    elif mutate == "leaked-mcp":
        actual["mcp-only"] = _subject("mcp-only")

    constraints = tmp_path / "constraints.txt"
    constraints.write_text(
        "\n".join(
            [
                "base-only==1",
                "dep==1",
                "mcp-only==1",
                "orphan==1",
                "transitive==1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bootstrap = tmp_path / "bootstrap.txt"
    bootstrap.write_text(
        f"--only-binary :all:\npip==26.1.2 \\\n  --hash=sha256:{'0' * 64}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_wheel_evidence, "inspect_wheel", lambda _path: wheel)
    monkeypatch.setattr(
        check_wheel_evidence, "_installed_subjects", lambda _path: actual
    )
    monkeypatch.setattr(
        check_wheel_evidence, "_verify_direct_url", lambda _subject, _wheel: None
    )
    return wheel_path, constraints, bootstrap


@pytest.mark.parametrize(
    ("profile", "count"),
    [("base", 3), ("mcp", 4)],
)
def test_selected_profile_recursively_propagates_dependency_extras(
    tmp_path, monkeypatch, profile, count
):
    wheel, constraints, bootstrap = _runtime_fixture(
        tmp_path, monkeypatch, profile=profile
    )

    closure = check_wheel_evidence.verify_runtime(
        wheel,
        tmp_path / "site",
        profile,
        constraints,
        bootstrap,
    )

    assert len(closure.third_party) == count
    assert "transitive" in closure.third_party
    assert "base-only" in closure.third_party
    assert ("mcp-only" in closure.third_party) is (profile == "mcp")


@pytest.mark.parametrize(
    ("profile", "mutate", "message"),
    [
        ("base", "missing-transitive", "missing distribution"),
        ("base", "orphan", "unexpected: orphan"),
        ("base", "leaked-mcp", "unexpected: mcp-only"),
    ],
)
def test_runtime_closure_rejects_missing_or_orphan_distributions(
    tmp_path, monkeypatch, profile, mutate, message
):
    wheel, constraints, bootstrap = _runtime_fixture(
        tmp_path,
        monkeypatch,
        profile=profile,
        mutate=mutate,
    )

    with pytest.raises(ValueError, match=message):
        check_wheel_evidence.verify_runtime(
            wheel,
            tmp_path / "site",
            profile,
            constraints,
            bootstrap,
        )


def test_runtime_closure_requires_exact_constraint_pin(tmp_path, monkeypatch):
    wheel, constraints, bootstrap = _runtime_fixture(
        tmp_path, monkeypatch, profile="base"
    )
    text = constraints.read_text(encoding="utf-8").replace(
        "transitive==1", "transitive==2"
    )
    constraints.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="expected constrained 2"):
        check_wheel_evidence.verify_runtime(
            wheel,
            tmp_path / "site",
            "base",
            constraints,
            bootstrap,
        )


def _raw_sbom(closure, **overrides):
    document = {
        "bomFormat": "CycloneDX",
        "components": [
            {
                "type": "library",
                "name": subject.name,
                "version": subject.version,
            }
            for subject in closure.third_party.values()
        ],
    }
    document.update(overrides)
    return document


def _closure(tmp_path, monkeypatch, profile="base"):
    wheel, constraints, bootstrap = _runtime_fixture(
        tmp_path, monkeypatch, profile=profile
    )
    closure = check_wheel_evidence.verify_runtime(
        wheel,
        tmp_path / "site",
        profile,
        constraints,
        bootstrap,
    )
    return closure, constraints, bootstrap


def test_runtime_audit_uses_only_exact_third_party_pins(tmp_path, monkeypatch):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    output = tmp_path / "raw.cdx.json"
    observed: list[str] = []
    audit_input = ""

    def fake_run(command, **kwargs):
        nonlocal audit_input
        observed.extend(command)
        requirement = Path(command[command.index("--requirement") + 1])
        audit_input = requirement.read_text(encoding="utf-8")
        candidate = Path(command[command.index("--output") + 1])
        candidate.write_text(json.dumps(_raw_sbom(closure)), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "No known vulnerabilities\n", "")

    monkeypatch.setattr(check_wheel_evidence.subprocess, "run", fake_run)

    assert check_wheel_evidence.run_runtime_audit(
        Path("/tmp/audit/python"), closure, output
    ) == len(closure.third_party)
    assert "--strict" in observed
    assert "--no-deps" in observed
    assert "--disable-pip" in observed
    assert "athena==" not in audit_input
    assert "pip==" not in audit_input
    assert set(audit_input.splitlines()) == {
        f"{subject.name}=={subject.version}" for subject in closure.third_party.values()
    }
    assert not list(tmp_path.glob(".athena-wheel-audit.*"))


def test_runtime_audit_rejects_nonzero_and_removes_stale_output(tmp_path, monkeypatch):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    output = tmp_path / "raw.cdx.json"
    output.write_text("stale", encoding="utf-8")

    def fake_run(command, **kwargs):
        candidate = Path(command[command.index("--output") + 1])
        candidate.write_text(json.dumps(_raw_sbom(closure)), encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, "", "scanner failed")

    monkeypatch.setattr(check_wheel_evidence.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="exit code 1"):
        check_wheel_evidence.run_runtime_audit(
            Path("/tmp/audit/python"), closure, output
        )
    assert not output.exists()
    assert not list(tmp_path.glob(".*.candidate"))


def test_runtime_audit_rejects_output_symlink_or_wheel_alias(tmp_path, monkeypatch):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    victim = tmp_path / "victim.json"
    victim.write_text("preserve me", encoding="utf-8")
    output = tmp_path / "raw.cdx.json"
    output.symlink_to(victim)
    monkeypatch.setattr(
        check_wheel_evidence.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("scanner must not run"),
    )

    with pytest.raises(ValueError, match="regular, non-symlink"):
        check_wheel_evidence.run_runtime_audit(
            Path("/tmp/audit/python"),
            closure,
            output,
        )
    assert victim.read_text(encoding="utf-8") == "preserve me"
    assert output.is_symlink()

    with pytest.raises(ValueError, match="aliases a protected input"):
        check_wheel_evidence.run_runtime_audit(
            Path("/tmp/audit/python"),
            closure,
            closure.wheel.path,
        )
    assert closure.wheel.path.read_bytes() == b"wheel-subject"


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"vulnerabilities": [{"id": "TEST-1"}]}, "TEST-1"),
        ({"components": []}, "components differ"),
    ],
)
def test_raw_audit_rejects_vulnerabilities_or_component_drift(
    tmp_path, monkeypatch, override, message
):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_raw_sbom(closure, **override)), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        check_wheel_evidence.verify_raw_audit(raw, closure)


def _write_final_sbom(tmp_path, closure):
    raw = tmp_path / "raw.json"
    raw.write_text(json.dumps(_raw_sbom(closure)), encoding="utf-8")
    output = tmp_path / f"athena-runtime-{closure.profile}-python312.cdx.json"
    check_wheel_evidence.build_runtime_sbom(
        closure,
        raw,
        output,
        source_revision="a" * 40,
    )
    return output


def test_final_sbom_binds_wheel_root_components_and_edges(tmp_path, monkeypatch):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    sbom = _write_final_sbom(tmp_path, closure)

    assert check_wheel_evidence.verify_runtime_sbom(sbom, closure) == len(
        closure.third_party
    )


def test_final_sbom_accepts_another_supported_cpython_patch(tmp_path, monkeypatch):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    sbom = _write_final_sbom(tmp_path, closure)
    document = json.loads(sbom.read_text(encoding="utf-8"))
    for prop in document["metadata"]["component"]["properties"]:
        if prop["name"] == "athena:python":
            prop["value"] = "CPython 3.12.99"
    sbom.write_text(json.dumps(document), encoding="utf-8")

    assert check_wheel_evidence.verify_runtime_sbom(sbom, closure) == len(
        closure.third_party
    )


@pytest.mark.parametrize(
    "identity",
    [
        "PyPy 3.12.13",
        "CPython 3.11.99",
        "CPython 3.12",
        "CPython 03.12.13",
        "CPython 3.12.13rc1",
        "CPython 3.12.13.0",
    ],
)
def test_final_sbom_rejects_invalid_or_unsupported_python_identity(
    tmp_path, monkeypatch, identity
):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    sbom = _write_final_sbom(tmp_path, closure)
    document = json.loads(sbom.read_text(encoding="utf-8"))
    for prop in document["metadata"]["component"]["properties"]:
        if prop["name"] == "athena:python":
            prop["value"] = identity
    sbom.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="Python identity"):
        check_wheel_evidence.verify_runtime_sbom(sbom, closure)


def _candidate_python_result(tmp_path, monkeypatch, identity):
    python = tmp_path / "python"
    python.write_text("candidate", encoding="utf-8")

    def fake_run(command, **kwargs):
        assert command[0] == str(python.absolute())
        assert command[1:3] == ["-I", "-c"]
        assert kwargs == {
            "check": True,
            "capture_output": True,
            "text": True,
            "cwd": "/tmp",
        }
        stdout = identity if isinstance(identity, str) else json.dumps(identity)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(check_wheel_evidence.subprocess, "run", fake_run)
    return python


def test_candidate_interpreter_binds_cpython_version_platform_and_site(
    tmp_path, monkeypatch
):
    site_packages = tmp_path / "site-packages"
    identity = {
        "implementation": "cpython",
        "platform": f"{sys.platform}-{os.uname().machine}",
        "python": sys.version.split()[0],
        "site_packages": str(site_packages),
    }
    python = _candidate_python_result(tmp_path, monkeypatch, identity)

    assert (
        check_wheel_evidence._site_packages_from_python(
            python,
            requires_python=">=3.12,<3.13",
        )
        == site_packages
    )


@pytest.mark.parametrize(
    "defect",
    [
        "json",
        "keys",
        "implementation",
        "version",
        "noncanonical-version",
        "platform",
        "relative-site",
        "non-string-site",
    ],
)
def test_candidate_interpreter_rejects_identity_drift(tmp_path, monkeypatch, defect):
    current_version = sys.version.split()[0]
    version_parts = current_version.split(".")
    other_patch = ".".join([*version_parts[:2], str(int(version_parts[2]) + 1)])
    identity = {
        "implementation": "cpython",
        "platform": f"{sys.platform}-{os.uname().machine}",
        "python": current_version,
        "site_packages": str(tmp_path / "site-packages"),
    }
    if defect == "json":
        identity = "{"
    elif defect == "keys":
        identity.pop("site_packages")
    elif defect == "implementation":
        identity["implementation"] = "pypy"
    elif defect == "version":
        identity["python"] = other_patch
    elif defect == "noncanonical-version":
        identity["python"] = ".".join(version_parts[:2])
    elif defect == "platform":
        identity["platform"] = "windows-arm64"
    elif defect == "relative-site":
        identity["site_packages"] = "relative/site-packages"
    else:
        identity["site_packages"] = None
    python = _candidate_python_result(tmp_path, monkeypatch, identity)

    with pytest.raises(ValueError):
        check_wheel_evidence._site_packages_from_python(
            python,
            requires_python=">=3.12,<3.13",
        )


def test_candidate_interpreter_rejects_incompatible_verifier(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.write_text("candidate", encoding="utf-8")
    monkeypatch.setattr(
        check_wheel_evidence.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("candidate must not run"),
    )

    with pytest.raises(ValueError, match="verifier Python identity is incompatible"):
        check_wheel_evidence._site_packages_from_python(
            python,
            requires_python=">=99",
        )


def test_candidate_interpreter_rejects_non_cpython_verifier(tmp_path, monkeypatch):
    python = tmp_path / "python"
    python.write_text("candidate", encoding="utf-8")
    monkeypatch.setattr(
        check_wheel_evidence.sys,
        "implementation",
        SimpleNamespace(name="pypy"),
    )
    monkeypatch.setattr(
        check_wheel_evidence.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("candidate must not run"),
    )

    with pytest.raises(ValueError, match="verifier is not CPython"):
        check_wheel_evidence._site_packages_from_python(
            python,
            requires_python=">=3.12,<3.13",
        )


@pytest.mark.parametrize("defect", ["root-hash", "component-ref", "edge", "scope"])
def test_final_sbom_rejects_root_component_and_graph_tampering(
    tmp_path, monkeypatch, defect
):
    closure, _constraints, _bootstrap = _closure(tmp_path, monkeypatch)
    sbom = _write_final_sbom(tmp_path, closure)
    document = json.loads(sbom.read_text(encoding="utf-8"))
    if defect == "root-hash":
        document["metadata"]["component"]["hashes"][0]["content"] = "0" * 64
    elif defect == "component-ref":
        document["components"][0]["bom-ref"] = "pkg:pypi/wrong@1"
    elif defect == "edge":
        document["dependencies"][0]["dependsOn"] = []
    else:
        for prop in document["metadata"]["component"]["properties"]:
            if prop["name"] == "athena:advisory-scope":
                prop["value"] = "first-party-code"
    sbom.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError):
        check_wheel_evidence.verify_runtime_sbom(sbom, closure)


def _bundle(tmp_path, monkeypatch, *, bundle_verifier=None):
    wheel = _write_wheel(tmp_path / "athena-0.1.0a1-py3-none-any.whl")
    wheel_subject = check_wheel_evidence.inspect_wheel(wheel)
    sdist = _write_sdist(
        tmp_path / "athena-0.1.0a1.tar.gz",
        wheel_subject,
    )
    input_sbom = tmp_path / "athena-ci-python.cdx.json"
    input_sbom.write_text('{"input": true}', encoding="utf-8")
    base_sbom = tmp_path / "athena-runtime-base-python312.cdx.json"
    mcp_sbom = tmp_path / "athena-runtime-mcp-python312.cdx.json"
    for profile, path in (("base", base_sbom), ("mcp", mcp_sbom)):
        dependency_ref = "pkg:pypi/dep@1"
        document = {
            "$schema": check_wheel_evidence.CYCLONEDX_SCHEMA,
            "bomFormat": "CycloneDX",
            "specVersion": "1.4",
            "metadata": {
                "component": {
                    "type": "application",
                    "bom-ref": wheel_subject.root_ref,
                    "name": "athena",
                    "version": wheel_subject.version,
                    "purl": wheel_subject.root_ref,
                    "hashes": [{"alg": "SHA-256", "content": wheel_subject.digest}],
                    "properties": check_wheel_evidence._properties(
                        {
                            "profile": profile,
                            "wheel": wheel.name,
                            "source-revision": "a" * 40,
                            "advisory-scope": (
                                "third-party-python-runtime-distributions"
                            ),
                            "excluded": (
                                "athena-code,bootstrap,build,dev,scanner,native-system"
                            ),
                            "python": f"CPython {sys.version.split()[0]}",
                            "platform": f"{sys.platform}-{os.uname().machine}",
                        }
                    ),
                }
            },
            "components": [
                {
                    "type": "library",
                    "bom-ref": dependency_ref,
                    "name": "dep",
                    "version": "1",
                    "purl": dependency_ref,
                }
            ],
            "dependencies": [
                {"ref": wheel_subject.root_ref, "dependsOn": [dependency_ref]},
                {"ref": dependency_ref, "dependsOn": []},
            ],
            "vulnerabilities": [],
        }
        path.write_text(json.dumps(document), encoding="utf-8")
    constraints = tmp_path / "constraints.txt"
    bootstrap = tmp_path / "bootstrap.txt"
    tool_lock = tmp_path / "tools.txt"
    for path in (constraints, bootstrap):
        path.write_text("subject", encoding="utf-8")
    _write_tool_lock(tool_lock)
    monkeypatch.setattr(
        check_wheel_evidence,
        "_site_packages_from_python",
        lambda _python, **_kwargs: Path("/tmp/site"),
    )
    monkeypatch.setattr(
        check_wheel_evidence,
        "verify_runtime",
        lambda wheel, site, profile, constraints, bootstrap: SimpleNamespace(
            profile=profile
        ),
    )
    monkeypatch.setattr(
        check_wheel_evidence, "verify_runtime_sbom", lambda path, closure: 1
    )
    monkeypatch.setattr(
        check_wheel_evidence._SUPPLY_CHAIN, "verify_sbom", lambda path: 63
    )
    if bundle_verifier is not None:
        monkeypatch.setattr(
            check_wheel_evidence,
            "verify_bundle",
            bundle_verifier,
        )
    output = tmp_path / "bundle"
    check_wheel_evidence.create_bundle(
        output=output,
        sdist=sdist,
        sdist_sha256=check_wheel_evidence._sha256(sdist),
        wheel=wheel,
        input_sbom=input_sbom,
        base_sbom=base_sbom,
        mcp_sbom=mcp_sbom,
        constraints=constraints,
        bootstrap=bootstrap,
        tool_lock=tool_lock,
        base_python=Path("/tmp/base/python"),
        mcp_python=Path("/tmp/mcp/python"),
        repository="Ayyitskevin/athena",
        event="pull_request",
        run_id="1",
        run_attempt="1",
        checkout_commit="a" * 40,
        checkout_tree="b" * 40,
        pr_head="c" * 40,
        pr_base="d" * 40,
    )
    return output, constraints, bootstrap, tool_lock


def test_candidate_bundle_removes_promoted_output_after_final_failure(
    tmp_path, monkeypatch
):
    calls = 0

    def fail_after_promotion(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 7
        raise ValueError("post-promotion failure")

    with pytest.raises(ValueError, match="post-promotion failure"):
        _bundle(
            tmp_path,
            monkeypatch,
            bundle_verifier=fail_after_promotion,
        )

    assert calls == 2
    assert not (tmp_path / "bundle").exists()


def test_candidate_bundle_has_exact_allowlist_and_verified_manifest(
    tmp_path, monkeypatch
):
    output, constraints, bootstrap, tool_lock = _bundle(tmp_path, monkeypatch)

    assert {path.name for path in output.iterdir()} == {
        "SHA256SUMS",
        "athena-0.1.0a1-py3-none-any.whl",
        "athena-0.1.0a1.tar.gz",
        "athena-ci-python.cdx.json",
        "athena-candidate.json",
        "athena-runtime-base-python312.cdx.json",
        "athena-runtime-mcp-python312.cdx.json",
    }
    assert (
        check_wheel_evidence.verify_bundle(
            output,
            constraints=constraints,
            bootstrap=bootstrap,
            tool_lock=tool_lock,
            base_python=Path("/tmp/base/python"),
            mcp_python=Path("/tmp/mcp/python"),
        )
        == 7
    )


@pytest.mark.parametrize(
    "defect",
    [
        "extra",
        "symlink",
        "hash",
        "commit",
        "sbom-ref",
        "source-revision",
        "environment",
        "producer",
        "artifact-swap",
        "input-filename",
        "sdist",
    ],
)
def test_candidate_bundle_rejects_extra_symlink_hash_and_identity_tampering(
    tmp_path, monkeypatch, defect
):
    output, constraints, bootstrap, tool_lock = _bundle(tmp_path, monkeypatch)
    if defect == "extra":
        (output / "extra.txt").write_text("extra", encoding="utf-8")
    elif defect == "symlink":
        (output / "link").symlink_to(output / "athena-candidate.json")
    elif defect == "hash":
        (output / "athena-runtime-base-python312.cdx.json").write_text(
            "tampered", encoding="utf-8"
        )
    elif defect == "commit":
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["workflow"]["checkout_commit"] = "not-a-git-oid"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "environment":
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["environment"] = {
            "implementation": "pypy",
            "platform": "windows-arm64",
            "python": "not-a-version",
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "producer":
        sbom_path = output / "athena-runtime-base-python312.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        for prop in sbom["metadata"]["component"]["properties"]:
            if prop["name"] == "athena:python":
                prop["value"] = "CPython 3.12.99"
        sbom_path.write_text(json.dumps(sbom), encoding="utf-8")
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["base_sbom"] = check_wheel_evidence._manifest_artifact(
            sbom_path
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "artifact-swap":
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["sdist"], manifest["artifacts"]["wheel"] = (
            manifest["artifacts"]["wheel"],
            manifest["artifacts"]["sdist"],
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "input-filename":
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inputs"]["constraints"]["filename"] = "another-lock.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "sdist":
        sdist_path = output / "athena-0.1.0a1.tar.gz"
        sdist_path.write_text("not a tar archive", encoding="utf-8")
        manifest_path = output / "athena-candidate.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["sdist"] = check_wheel_evidence._manifest_artifact(
            sdist_path
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif defect == "sbom-ref":
        sbom_path = output / "athena-runtime-base-python312.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        sbom["components"][0]["bom-ref"] = "pkg:pypi/wrong@1"
        sbom_path.write_text(json.dumps(sbom), encoding="utf-8")
    else:
        sbom_path = output / "athena-runtime-base-python312.cdx.json"
        sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
        for prop in sbom["metadata"]["component"]["properties"]:
            if prop["name"] == "athena:source-revision":
                prop["value"] = "e" * 40
        sbom_path.write_text(json.dumps(sbom), encoding="utf-8")

    if defect in {
        "commit",
        "sbom-ref",
        "source-revision",
        "environment",
        "producer",
        "artifact-swap",
        "input-filename",
        "sdist",
    }:
        checksum_paths = [
            path for path in output.iterdir() if path.name != "SHA256SUMS"
        ]
        (output / "SHA256SUMS").write_text(
            "".join(
                f"{check_wheel_evidence._sha256(path)}  {path.name}\n"
                for path in sorted(checksum_paths, key=lambda item: item.name)
            ),
            encoding="utf-8",
        )

    with pytest.raises(ValueError):
        check_wheel_evidence.verify_bundle(
            output,
            constraints=constraints,
            bootstrap=bootstrap,
            tool_lock=tool_lock,
            base_python=Path("/tmp/base/python"),
            mcp_python=Path("/tmp/mcp/python"),
        )
