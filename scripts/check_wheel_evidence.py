"""Build and verify wheel-bound runtime advisory evidence for Athena."""

from __future__ import annotations

import argparse
import base64
from collections import Counter, deque
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import BytesParser
import gzip
from hashlib import sha256
import importlib.util
from importlib.metadata import Distribution, distributions
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4
from zipfile import BadZipFile, ZipFile, ZipInfo

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

_SUPPLY_CHAIN_PATH = Path(__file__).with_name("check_supply_chain.py")
_SUPPLY_CHAIN_SPEC = importlib.util.spec_from_file_location(
    "athena_check_supply_chain_for_wheel",
    _SUPPLY_CHAIN_PATH,
)
if _SUPPLY_CHAIN_SPEC is None or _SUPPLY_CHAIN_SPEC.loader is None:
    raise RuntimeError(f"cannot load {_SUPPLY_CHAIN_PATH}")
_SUPPLY_CHAIN = importlib.util.module_from_spec(_SUPPLY_CHAIN_SPEC)
sys.modules[_SUPPLY_CHAIN_SPEC.name] = _SUPPLY_CHAIN
_SUPPLY_CHAIN_SPEC.loader.exec_module(_SUPPLY_CHAIN)
read_pins = _SUPPLY_CHAIN.read_pins


REPO_ROOT = Path(__file__).resolve().parents[1]
CYCLONEDX_SCHEMA = "http://cyclonedx.org/schema/bom-1.4.schema.json"
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_MEMBER_BYTES = 25 * 1024 * 1024
MAX_ARCHIVE_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200
MAX_TAR_CONTROL_MEMBER_BYTES = 64 * 1024
MAX_TAR_CONTROL_TOTAL_BYTES = 1024 * 1024
MAX_TAR_STREAM_BYTES = MAX_ARCHIVE_EXPANDED_BYTES + 16 * 1024 * 1024
MAX_TAR_RAW_HEADERS = MAX_ARCHIVE_MEMBERS * 2 + 16
PROFILES = {"base": frozenset(), "mcp": frozenset({"mcp"})}
BOOTSTRAP_NAMES = frozenset({"pip"})
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class WheelSubject:
    path: Path
    name: str
    version: str
    requires_python: str
    requires: tuple[str, ...]
    extras: frozenset[str]
    metadata_bytes: bytes
    digest: str
    root_ref: str


@dataclass(frozen=True)
class SdistSubject:
    path: Path
    name: str
    version: str
    requires_python: str
    requires: tuple[str, ...]
    extras: frozenset[str]
    build_backend_version: str | None
    digest: str


@dataclass(frozen=True)
class InstalledSubject:
    name: str
    version: str
    requires: tuple[str, ...]
    extras: frozenset[str]
    distribution: Distribution


@dataclass(frozen=True)
class RuntimeClosure:
    profile: str
    subjects: dict[str, InstalledSubject]
    edges: dict[str, frozenset[str]]
    wheel: WheelSubject

    @property
    def third_party(self) -> dict[str, InstalledSubject]:
        return {
            name: subject for name, subject in self.subjects.items() if name != "athena"
        }


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int = MAX_ARTIFACT_BYTES,
) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"{label} is not readable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise ValueError(f"{label} must be a regular, non-symlink file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"{label} is empty: {path}")
    if size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes: {path}")
    return path.resolve()


def _require_external(path: Path, *, label: str, root: Path = REPO_ROOT) -> None:
    resolved = path.resolve(strict=False)
    if resolved.is_relative_to(root.resolve()):
        raise ValueError(f"{label} must stay outside the checkout")


def _prepare_external_output(
    path: Path,
    *,
    label: str,
    protected: tuple[Path, ...] = (),
) -> Path:
    """Prepare a regular output leaf without following an existing symlink."""
    name = path.name
    if name in {"", ".", ".."}:
        raise ValueError(f"{label} has an invalid filename")
    parent = path.absolute().parent.resolve()
    _require_external(parent, label=label)
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / name
    try:
        mode = output.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(mode):
            raise ValueError(f"{label} must be a regular, non-symlink file")
        if any(output == item.resolve(strict=False) for item in protected):
            raise ValueError(f"{label} aliases a protected input")
        output.unlink()
    return output


def _temporary_output(output: Path) -> Path:
    return output.with_name(f".{output.name}.candidate-{uuid4().hex}")


def _new_external_directory(path: Path, *, label: str) -> Path:
    name = path.name
    if name in {"", ".", ".."}:
        raise ValueError(f"{label} has an invalid directory name")
    parent = path.absolute().parent.resolve()
    _require_external(parent, label=label)
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / name
    try:
        output.lstat()
    except FileNotFoundError:
        return output
    raise ValueError(f"{label} already exists or is a symlink")


def _safe_zip_member(info: ZipInfo) -> str:
    name = info.filename
    if not name or "\\" in name:
        raise ValueError(f"wheel contains unsafe member name {name!r}")
    member = PurePosixPath(name)
    canonical = member.as_posix()
    expected = f"{canonical}/" if info.is_dir() else canonical
    if (
        member.is_absolute()
        or ".." in member.parts
        or canonical in {"", "."}
        or name != expected
    ):
        raise ValueError(f"wheel contains unsafe member name {name!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"wheel contains symlink member {name!r}")
    file_type = stat.S_IFMT(mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"wheel contains special-file member {name!r}")
    return canonical


def _record_member_digest(archive: ZipFile, name: str) -> tuple[str, int]:
    digest = sha256()
    size = 0
    with archive.open(name) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    return encoded, size


def _verify_record(
    archive: ZipFile,
    *,
    record_name: str,
    file_names: set[str],
) -> None:
    try:
        rows = list(
            csv.reader(
                archive.read(record_name).decode("utf-8").splitlines(),
                strict=True,
            )
        )
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("wheel RECORD is not valid UTF-8 CSV") from exc

    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or not row[0]:
            raise ValueError("wheel RECORD contains a malformed row")
        if row[0] in records:
            raise ValueError(f"wheel RECORD duplicates {row[0]!r}")
        member = PurePosixPath(row[0])
        if member.is_absolute() or ".." in member.parts or "\\" in row[0]:
            raise ValueError(f"wheel RECORD contains unsafe path {row[0]!r}")
        records[row[0]] = (row[1], row[2])

    missing = sorted(file_names - set(records))
    unexpected = sorted(set(records) - file_names)
    if missing or unexpected:
        raise ValueError(
            "wheel RECORD membership differs from archive"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
        )

    for name in sorted(file_names):
        hash_value, size_value = records[name]
        if name == record_name:
            if hash_value or size_value:
                raise ValueError("wheel RECORD must not hash itself")
            continue
        digest, size = _record_member_digest(archive, name)
        expected_hash = f"sha256={digest}"
        if hash_value != expected_hash:
            raise ValueError(f"wheel RECORD hash mismatch for {name!r}")
        if size_value != str(size):
            raise ValueError(f"wheel RECORD size mismatch for {name!r}")


def inspect_wheel(
    wheel_path: Path,
    *,
    require_external: bool = True,
    root: Path = REPO_ROOT,
) -> WheelSubject:
    """Validate a wheel's identity, compatibility, members, and RECORD."""
    wheel_path = _require_regular_file(wheel_path, label="wheel")
    if require_external:
        _require_external(wheel_path, label="wheel", root=root)
    try:
        filename_name, filename_version, _build, filename_tags = parse_wheel_filename(
            wheel_path.name
        )
    except (InvalidVersion, ValueError) as exc:
        raise ValueError(f"wheel filename is invalid: {wheel_path.name}") from exc
    if canonicalize_name(filename_name) != "athena":
        raise ValueError("wheel filename does not identify Athena")
    if not filename_tags.intersection(sys_tags()):
        raise ValueError("wheel tags are incompatible with the running interpreter")

    try:
        with ZipFile(wheel_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(
                    f"wheel contains more than {MAX_ARCHIVE_MEMBERS} members"
                )
            names = [info.filename for info in infos]
            duplicates = sorted(
                name for name, count in Counter(names).items() if count != 1
            )
            if duplicates:
                raise ValueError(
                    "wheel contains duplicate members: " + ", ".join(duplicates)
                )
            canonical_names: set[str] = set()
            expanded_bytes = 0
            for info in infos:
                canonical = _safe_zip_member(info)
                if canonical in canonical_names:
                    raise ValueError(f"wheel aliases extraction target {canonical!r}")
                canonical_names.add(canonical)
                if info.is_dir():
                    continue
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(
                        f"wheel member {info.filename!r} exceeds "
                        f"{MAX_ARCHIVE_MEMBER_BYTES} bytes"
                    )
                expanded_bytes += info.file_size
                if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                    raise ValueError(
                        f"wheel expands beyond {MAX_ARCHIVE_EXPANDED_BYTES} bytes"
                    )
                if (
                    info.file_size > 1024 * 1024
                    and info.file_size
                    > max(1, info.compress_size) * MAX_ARCHIVE_COMPRESSION_RATIO
                ):
                    raise ValueError(
                        f"wheel member {info.filename!r} exceeds the compression "
                        "ratio limit"
                    )
            file_names = {info.filename for info in infos if not info.is_dir()}

            metadata_names = sorted(
                name for name in file_names if name.endswith(".dist-info/METADATA")
            )
            wheel_names = sorted(
                name for name in file_names if name.endswith(".dist-info/WHEEL")
            )
            record_names = sorted(
                name for name in file_names if name.endswith(".dist-info/RECORD")
            )
            if not (len(metadata_names) == len(wheel_names) == len(record_names) == 1):
                raise ValueError(
                    "wheel must contain exactly one METADATA, WHEEL, and RECORD"
                )
            dist_info = metadata_names[0].removesuffix("/METADATA")
            if wheel_names[0] != f"{dist_info}/WHEEL" or record_names[0] != (
                f"{dist_info}/RECORD"
            ):
                raise ValueError("wheel metadata files do not share one dist-info tree")
            if any(
                ".dist-info/" in name and not name.startswith(f"{dist_info}/")
                for name in file_names
            ):
                raise ValueError("wheel contains more than one dist-info tree")

            metadata_bytes = archive.read(metadata_names[0])
            metadata = BytesParser().parsebytes(metadata_bytes)
            name = metadata.get("Name")
            version = metadata.get("Version")
            requires_python = metadata.get("Requires-Python")
            if not name or canonicalize_name(name) != "athena":
                raise ValueError("wheel METADATA does not identify Athena")
            if not version or Version(version) != filename_version:
                raise ValueError("wheel filename and METADATA versions differ")
            if not requires_python:
                raise ValueError("wheel METADATA lacks Requires-Python")
            running = Version(
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            )
            requirement = Requirement(f"python{requires_python}")
            if running not in requirement.specifier:
                raise ValueError(
                    f"wheel Requires-Python {requires_python!r} excludes {running}"
                )

            wheel_metadata = BytesParser().parsebytes(archive.read(wheel_names[0]))
            declared_tags: set[Any] = set()
            for value in wheel_metadata.get_all("Tag", []):
                declared_tags.update(parse_tag(value))
            if declared_tags != set(filename_tags):
                raise ValueError("wheel filename tags differ from WHEEL metadata")

            requires = tuple(metadata.get_all("Requires-Dist", []))
            extras = frozenset(
                canonicalize_name(extra)
                for extra in metadata.get_all("Provides-Extra", [])
            )
            for raw in requires:
                try:
                    Requirement(raw)
                except InvalidRequirement as exc:
                    raise ValueError(
                        f"wheel METADATA has invalid Requires-Dist {raw!r}"
                    ) from exc
            _verify_record(
                archive,
                record_name=record_names[0],
                file_names=file_names,
            )
    except BadZipFile as exc:
        raise ValueError("wheel is not a valid ZIP archive") from exc

    digest = _sha256(wheel_path)
    version_text = str(filename_version)
    return WheelSubject(
        path=wheel_path,
        name=name,
        version=version_text,
        requires_python=requires_python,
        requires=requires,
        extras=extras,
        metadata_bytes=metadata_bytes,
        digest=digest,
        root_ref=f"pkg:pypi/athena@{version_text}",
    )


def _safe_tar_member(member: tarfile.TarInfo) -> str:
    name = member.name
    if not name or "\\" in name:
        raise ValueError(f"sdist contains unsafe member name {name!r}")
    path = PurePosixPath(name)
    canonical = path.as_posix()
    if (
        path.is_absolute()
        or ".." in path.parts
        or canonical in {"", "."}
        or name != canonical
    ):
        raise ValueError(f"sdist contains unsafe member name {name!r}")
    if not (member.isdir() or member.isreg()):
        raise ValueError(f"sdist contains link or special-file member {name!r}")
    if member.size < 0 or member.size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError(
            f"sdist member {name!r} exceeds {MAX_ARCHIVE_MEMBER_BYTES} bytes"
        )
    return canonical


def _tar_header_size(header: bytes) -> int:
    raw = header[124:136]
    if raw[:1] and raw[0] & 0x80:
        raise ValueError("sdist uses an unsupported binary tar size")
    value = raw.rstrip(b"\0 ").lstrip(b" ")
    if not value or any(byte not in b"01234567" for byte in value):
        raise ValueError("sdist contains an invalid tar size")
    return int(value, 8)


def _read_exact(stream: Any, size: int, *, label: str, capture: bool = False) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise ValueError(f"sdist ended inside {label}")
        if capture:
            chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_local_pax(payload: bytes) -> None:
    """Allow only the bounded local mtime record emitted by Python build."""
    position = 0
    keys: set[str] = set()
    while position < len(payload):
        separator = payload.find(b" ", position)
        if separator < 0:
            raise ValueError("sdist local PAX header is malformed")
        raw_length = payload[position:separator]
        if (
            not raw_length
            or not raw_length.isdigit()
            or (len(raw_length) > 1 and raw_length.startswith(b"0"))
        ):
            raise ValueError("sdist local PAX header has an invalid record length")
        length = int(raw_length)
        end = position + length
        record = payload[position:end]
        body_start = separator - position + 1
        if end > len(payload) or len(record) != length or not record.endswith(b"\n"):
            raise ValueError("sdist local PAX header has a truncated record")
        body = record[body_start:-1]
        if b"=" not in body:
            raise ValueError("sdist local PAX header has a malformed record")
        raw_key, raw_value = body.split(b"=", 1)
        try:
            key = raw_key.decode("ascii")
            value = raw_value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("sdist local PAX header is not ASCII") from exc
        if key != "mtime":
            raise ValueError(f"sdist local PAX header uses unsupported key {key!r}")
        if key in keys or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
            raise ValueError("sdist local PAX mtime record is invalid")
        keys.add(key)
        position = end
    if keys != {"mtime"}:
        raise ValueError("sdist local PAX header lacks one mtime record")


def _preflight_sdist_stream(path: Path) -> None:
    """Bound raw gzip/tar work before tarfile consumes hidden control records."""
    header_count = 0
    control_bytes = 0
    stream_bytes = 0
    pending_local_pax = False
    compressed_bytes = path.stat().st_size

    try:
        with gzip.open(path, "rb") as stream:
            while True:
                header = stream.read(512)
                stream_bytes += len(header)
                if stream_bytes > MAX_TAR_STREAM_BYTES:
                    raise ValueError(
                        f"sdist tar stream exceeds {MAX_TAR_STREAM_BYTES} bytes"
                    )
                if len(header) != 512:
                    raise ValueError("sdist lacks a complete tar terminator")
                if header == bytes(512):
                    second = stream.read(512)
                    stream_bytes += len(second)
                    if len(second) != 512 or second != bytes(512):
                        raise ValueError(
                            "sdist lacks two canonical tar terminator blocks"
                        )
                    while chunk := stream.read(1024 * 1024):
                        stream_bytes += len(chunk)
                        if stream_bytes > MAX_TAR_STREAM_BYTES:
                            raise ValueError(
                                f"sdist tar stream exceeds {MAX_TAR_STREAM_BYTES} bytes"
                            )
                        if any(chunk):
                            raise ValueError(
                                "sdist contains nonzero data after its terminator"
                            )
                    break

                header_count += 1
                if header_count > MAX_TAR_RAW_HEADERS:
                    raise ValueError(
                        f"sdist contains more than {MAX_TAR_RAW_HEADERS} raw headers"
                    )
                size = _tar_header_size(header)
                typeflag = header[156:157]
                if typeflag == b"x":
                    if pending_local_pax:
                        raise ValueError("sdist contains consecutive local PAX headers")
                    if size > MAX_TAR_CONTROL_MEMBER_BYTES:
                        raise ValueError(
                            "sdist local PAX header exceeds "
                            f"{MAX_TAR_CONTROL_MEMBER_BYTES} bytes"
                        )
                    control_bytes += size
                    if control_bytes > MAX_TAR_CONTROL_TOTAL_BYTES:
                        raise ValueError(
                            "sdist control metadata exceeds "
                            f"{MAX_TAR_CONTROL_TOTAL_BYTES} bytes"
                        )
                    pending_local_pax = True
                    capture = True
                elif typeflag in {b"g", b"L", b"K", b"S"}:
                    raise ValueError("sdist contains an unsupported tar control header")
                else:
                    if pending_local_pax:
                        pending_local_pax = False
                    if size > MAX_ARCHIVE_MEMBER_BYTES:
                        raise ValueError(
                            f"sdist raw member exceeds {MAX_ARCHIVE_MEMBER_BYTES} bytes"
                        )
                    capture = False

                padded_size = ((size + 511) // 512) * 512
                stream_bytes += padded_size
                if stream_bytes > MAX_TAR_STREAM_BYTES:
                    raise ValueError(
                        f"sdist tar stream exceeds {MAX_TAR_STREAM_BYTES} bytes"
                    )
                payload = _read_exact(
                    stream,
                    padded_size,
                    label="tar member",
                    capture=capture,
                )
                if capture:
                    _validate_local_pax(payload[:size])
    except (EOFError, gzip.BadGzipFile, OSError) as exc:
        raise ValueError(
            "sdist is not a valid tar archive or bounded gzip stream"
        ) from exc

    if pending_local_pax:
        raise ValueError("sdist ends after a local PAX header")
    if (
        stream_bytes > 1024 * 1024
        and stream_bytes > compressed_bytes * MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        raise ValueError("sdist tar stream exceeds the compression ratio limit")


def _digest_stream(stream: Any) -> tuple[str, int]:
    digest = sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _tool_setuptools_version(tool_lock: Path) -> str:
    dependencies = {
        canonicalize_name(dependency.name): dependency
        for dependency in read_pins(
            _require_regular_file(tool_lock, label="evidence tool lock"),
            require_hashes=True,
            require_only_binary=True,
        )
    }
    setuptools = dependencies.get("setuptools")
    if setuptools is None:
        raise ValueError("evidence tool lock has no setuptools pin")
    canonical_version = str(Version(setuptools.version))
    if canonical_version != setuptools.version:
        raise ValueError("evidence tool lock setuptools pin is not canonical")
    return canonical_version


def _validate_sdist_project(
    sdist_path: Path,
    file_names: set[str],
    *,
    expected_root: str,
    wheel: WheelSubject,
    metadata_bytes: bytes,
) -> str:
    if metadata_bytes != wheel.metadata_bytes:
        raise ValueError("sdist PKG-INFO differs from wheel METADATA")
    required = {
        f"{expected_root}/pyproject.toml",
        f"{expected_root}/src/athena/__init__.py",
    }
    missing = sorted(required - file_names)
    if missing:
        raise ValueError(
            "sdist lacks required build/source members: " + ", ".join(missing)
        )
    if f"{expected_root}/setup.py" in file_names:
        raise ValueError("sdist contains unsupported executable setup.py")

    with tarfile.open(sdist_path, mode="r:*") as archive:
        pyproject_stream = archive.extractfile(f"{expected_root}/pyproject.toml")
        if pyproject_stream is None:
            raise ValueError("sdist pyproject.toml is unreadable")
        try:
            configuration = tomllib.loads(pyproject_stream.read().decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("sdist pyproject.toml is invalid") from exc
    project = configuration.get("project")
    build_system = configuration.get("build-system")
    tool = configuration.get("tool")
    setuptools_configuration = (
        tool.get("setuptools") if isinstance(tool, dict) else None
    )
    packages = (
        setuptools_configuration.get("packages")
        if isinstance(setuptools_configuration, dict)
        else None
    )
    package_find = packages.get("find") if isinstance(packages, dict) else None
    try:
        project_python = Requirement(
            f"python{str(project.get('requires-python', ''))}"
            if isinstance(project, dict)
            else "python"
        ).specifier
        wheel_python = Requirement(f"python{wheel.requires_python}").specifier
    except InvalidRequirement as exc:
        raise ValueError("sdist pyproject Requires-Python is invalid") from exc
    if (
        not isinstance(project, dict)
        or canonicalize_name(str(project.get("name", ""))) != "athena"
        or str(project.get("version", "")) != wheel.version
        or project_python != wheel_python
    ):
        raise ValueError("sdist pyproject identity differs from the wheel")
    if (
        not isinstance(build_system, dict)
        or build_system.get("build-backend") != "setuptools.build_meta"
        or build_system.get("backend-path") is not None
    ):
        raise ValueError("sdist build backend is not the supported setuptools backend")
    backend_requirements = build_system.get("requires")
    if not isinstance(backend_requirements, list) or len(backend_requirements) != 1:
        raise ValueError("sdist build backend must have one exact setuptools pin")
    try:
        backend_requirement = Requirement(str(backend_requirements[0]))
    except InvalidRequirement as exc:
        raise ValueError("sdist build backend requirement is invalid") from exc
    backend_specifiers = list(backend_requirement.specifier)
    if (
        canonicalize_name(backend_requirement.name) != "setuptools"
        or backend_requirement.url is not None
        or backend_requirement.extras
        or backend_requirement.marker is not None
        or len(backend_specifiers) != 1
        or backend_specifiers[0].operator != "=="
    ):
        raise ValueError("sdist build backend must have one exact setuptools pin")
    try:
        backend_version = Version(backend_specifiers[0].version)
    except InvalidVersion as exc:
        raise ValueError("sdist build backend pin is not an exact version") from exc
    if str(backend_version) != backend_specifiers[0].version:
        raise ValueError("sdist build backend pin is not canonical")
    if not isinstance(package_find, dict) or package_find.get("where") != ["src"]:
        raise ValueError("sdist package discovery is not rooted at src")

    with (
        tarfile.open(sdist_path, mode="r:*") as archive,
        ZipFile(wheel.path) as wheel_archive,
    ):
        payload = [
            info
            for info in wheel_archive.infolist()
            if not info.is_dir() and ".dist-info/" not in info.filename
        ]
        if not payload:
            raise ValueError("wheel has no installable Athena payload")
        wheel_payload_names = {info.filename for info in payload}
        source_prefix = f"{expected_root}/src/"
        sdist_payload_names = {
            name.removeprefix(source_prefix)
            for name in file_names
            if name.startswith(f"{source_prefix}athena/")
        }
        missing_from_sdist = sorted(wheel_payload_names - sdist_payload_names)
        omitted_from_wheel = sorted(sdist_payload_names - wheel_payload_names)
        if missing_from_sdist or omitted_from_wheel:
            details = ["sdist and wheel payload membership differ"]
            if missing_from_sdist:
                details.append("missing from sdist: " + ", ".join(missing_from_sdist))
            if omitted_from_wheel:
                details.append("omitted from wheel: " + ", ".join(omitted_from_wheel))
            raise ValueError("; ".join(details))
        for info in payload:
            if not info.filename.startswith("athena/"):
                raise ValueError(
                    f"wheel contains unsupported payload path {info.filename!r}"
                )
            source_name = f"{expected_root}/src/{info.filename}"
            if source_name not in file_names:
                raise ValueError(
                    f"sdist lacks wheel payload source member {source_name!r}"
                )
            source_stream = archive.extractfile(source_name)
            if source_stream is None:
                raise ValueError(f"sdist source member {source_name!r} is unreadable")
            with source_stream, wheel_archive.open(info) as wheel_stream:
                source_digest, source_size = _digest_stream(source_stream)
                wheel_digest, wheel_size = _digest_stream(wheel_stream)
            if (source_digest, source_size) != (wheel_digest, wheel_size):
                raise ValueError(
                    f"sdist source member differs from wheel payload {info.filename!r}"
                )
    return str(backend_version)


def inspect_sdist(
    sdist_path: Path,
    *,
    wheel: WheelSubject | None = None,
    tool_lock: Path | None = None,
    expected_digest: str | None = None,
    require_external: bool = True,
    root: Path = REPO_ROOT,
) -> SdistSubject:
    """Validate a bounded Athena sdist and optionally bind it to a wheel."""
    sdist_path = _require_regular_file(sdist_path, label="sdist")
    if require_external:
        _require_external(sdist_path, label="sdist", root=root)
    initial_digest = _sha256(sdist_path)
    if expected_digest is not None:
        if not _HEX_SHA256.fullmatch(expected_digest):
            raise ValueError("expected sdist digest is not a SHA-256 value")
        if initial_digest != expected_digest:
            raise ValueError("sdist digest differs from the pre-extraction snapshot")
    _preflight_sdist_stream(sdist_path)
    prefix, suffix = "athena-", ".tar.gz"
    if not sdist_path.name.startswith(prefix) or not sdist_path.name.endswith(suffix):
        raise ValueError("sdist filename does not identify Athena")
    filename_version = sdist_path.name[len(prefix) : -len(suffix)]
    try:
        parsed_filename_version = Version(filename_version)
    except InvalidVersion as exc:
        raise ValueError("sdist filename has an invalid version") from exc
    if str(parsed_filename_version) != filename_version:
        raise ValueError("sdist filename version is not canonical")
    expected_root = f"athena-{filename_version}"

    seen: set[str] = set()
    roots: set[str] = set()
    expanded_bytes = 0
    root_directory = False
    pkg_info: tarfile.TarInfo | None = None
    files: dict[str, tarfile.TarInfo] = {}
    try:
        with tarfile.open(sdist_path, mode="r:*") as archive:
            for index, member in enumerate(archive, start=1):
                if index > MAX_ARCHIVE_MEMBERS:
                    raise ValueError(
                        f"sdist contains more than {MAX_ARCHIVE_MEMBERS} members"
                    )
                canonical = _safe_tar_member(member)
                if canonical in seen:
                    raise ValueError(f"sdist duplicates member {canonical!r}")
                seen.add(canonical)
                roots.add(PurePosixPath(canonical).parts[0])
                if canonical == expected_root and member.isdir():
                    root_directory = True
                if member.isreg():
                    files[canonical] = member
                    expanded_bytes += member.size
                    if expanded_bytes > MAX_ARCHIVE_EXPANDED_BYTES:
                        raise ValueError(
                            f"sdist expands beyond {MAX_ARCHIVE_EXPANDED_BYTES} bytes"
                        )
                if canonical == f"{expected_root}/PKG-INFO":
                    pkg_info = member
            if roots != {expected_root} or not root_directory:
                raise ValueError("sdist does not contain one canonical Athena root")
            if pkg_info is None or not pkg_info.isreg():
                raise ValueError("sdist lacks one canonical PKG-INFO file")
            stream = archive.extractfile(pkg_info)
            if stream is None:
                raise ValueError("sdist PKG-INFO is unreadable")
            metadata_bytes = stream.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    except tarfile.TarError as exc:
        raise ValueError("sdist is not a valid tar archive") from exc

    if len(metadata_bytes) > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError("sdist PKG-INFO exceeds the member-size limit")
    compressed_bytes = sdist_path.stat().st_size
    if (
        expanded_bytes > 1024 * 1024
        and expanded_bytes > compressed_bytes * MAX_ARCHIVE_COMPRESSION_RATIO
    ):
        raise ValueError("sdist exceeds the compression ratio limit")

    metadata = BytesParser().parsebytes(metadata_bytes)
    name = metadata.get("Name")
    version = metadata.get("Version")
    requires_python = metadata.get("Requires-Python")
    if not name or canonicalize_name(name) != "athena":
        raise ValueError("sdist PKG-INFO does not identify Athena")
    if not version or Version(version) != parsed_filename_version:
        raise ValueError("sdist filename and PKG-INFO versions differ")
    if not requires_python:
        raise ValueError("sdist PKG-INFO lacks Requires-Python")
    requires = tuple(metadata.get_all("Requires-Dist", []))
    extras = frozenset(
        canonicalize_name(extra) for extra in metadata.get_all("Provides-Extra", [])
    )
    for raw in requires:
        try:
            Requirement(raw)
        except InvalidRequirement as exc:
            raise ValueError(
                f"sdist PKG-INFO has invalid Requires-Dist {raw!r}"
            ) from exc

    build_backend_version: str | None = None
    if wheel is not None:
        if tool_lock is None:
            raise ValueError("evidence tool lock is required to bind sdist and wheel")
        expected_backend_version = _tool_setuptools_version(tool_lock)
        if Version(version) != Version(wheel.version):
            raise ValueError("sdist and wheel versions differ")
        if requires_python != wheel.requires_python:
            raise ValueError("sdist and wheel Requires-Python differ")
        if requires != wheel.requires or extras != wheel.extras:
            raise ValueError("sdist and wheel dependency metadata differ")
        build_backend_version = _validate_sdist_project(
            sdist_path,
            set(files),
            expected_root=expected_root,
            wheel=wheel,
            metadata_bytes=metadata_bytes,
        )
        if build_backend_version != expected_backend_version:
            raise ValueError("sdist setuptools pin differs from the evidence tool lock")

    final_digest = _sha256(sdist_path)
    if final_digest != initial_digest:
        raise ValueError("sdist changed while it was inspected")

    return SdistSubject(
        path=sdist_path,
        name=name,
        version=str(parsed_filename_version),
        requires_python=requires_python,
        requires=requires,
        extras=extras,
        build_backend_version=build_backend_version,
        digest=final_digest,
    )


def _installed_subjects(site_packages: Path) -> dict[str, InstalledSubject]:
    try:
        mode = site_packages.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"site-packages is not readable: {site_packages}") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError("site-packages must be a real directory")
    _require_external(site_packages, label="site-packages")

    actual: dict[str, InstalledSubject] = {}
    for distribution in distributions(path=[str(site_packages)]):
        name = distribution.metadata.get("Name")
        if not name:
            raise ValueError("installed distribution has no Name metadata")
        normalized = canonicalize_name(name)
        if normalized in actual:
            raise ValueError(f"candidate duplicates installed distribution {name!r}")
        requires = tuple(distribution.requires or ())
        extras = frozenset(
            canonicalize_name(extra)
            for extra in distribution.metadata.get_all("Provides-Extra", [])
        )
        actual[normalized] = InstalledSubject(
            name=name,
            version=distribution.version,
            requires=requires,
            extras=extras,
            distribution=distribution,
        )
    if not actual:
        raise ValueError("candidate environment has no installed distributions")
    return actual


def _marker_applies(requirement: Requirement, extras: frozenset[str]) -> bool:
    if requirement.marker is None:
        return True
    environment = default_environment()
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in ("", *sorted(extras))
    )


def _ref(subject: InstalledSubject) -> str:
    return f"pkg:pypi/{canonicalize_name(subject.name)}@{Version(subject.version)}"


def _verify_direct_url(subject: InstalledSubject, wheel: WheelSubject) -> None:
    dist_info = Path(subject.distribution._path)  # type: ignore[attr-defined]
    metadata_path = dist_info / "METADATA"
    direct_url_path = dist_info / "direct_url.json"
    if metadata_path.read_bytes() != wheel.metadata_bytes:
        raise ValueError("installed Athena METADATA differs from the wheel")
    try:
        direct_url = json.loads(direct_url_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("installed Athena lacks valid direct_url.json") from exc
    archive_info = direct_url.get("archive_info")
    if not isinstance(archive_info, dict):
        raise ValueError("installed Athena direct_url lacks archive_info")
    if archive_info.get("hash") != f"sha256={wheel.digest}":
        raise ValueError("installed Athena direct_url hash differs from the wheel")
    parsed = urlparse(str(direct_url.get("url", "")))
    if parsed.scheme != "file":
        raise ValueError("installed Athena direct_url is not a local wheel URL")
    installed_from = Path(unquote(parsed.path)).resolve()
    if installed_from != wheel.path:
        raise ValueError("installed Athena direct_url points at another wheel")


def verify_runtime(
    wheel_path: Path,
    site_packages: Path,
    profile: str,
    constraints_path: Path,
    bootstrap_path: Path,
) -> RuntimeClosure:
    """Require a candidate environment to equal one selected metadata closure."""
    if profile not in PROFILES:
        raise ValueError(f"unsupported runtime profile {profile!r}")
    wheel = inspect_wheel(wheel_path)
    if not PROFILES[profile].issubset(wheel.extras):
        missing = sorted(PROFILES[profile] - wheel.extras)
        raise ValueError(
            "wheel does not provide selected extras: " + ", ".join(missing)
        )

    actual = _installed_subjects(site_packages)
    athena = actual.get("athena")
    if athena is None:
        raise ValueError("candidate environment is missing Athena")
    if Version(athena.version) != Version(wheel.version):
        raise ValueError("installed Athena version differs from the wheel")
    _verify_direct_url(athena, wheel)

    bootstrap = {
        canonicalize_name(dependency.name): dependency.version
        for dependency in read_pins(
            bootstrap_path,
            require_hashes=True,
            require_only_binary=True,
        )
    }
    for name in BOOTSTRAP_NAMES:
        subject = actual.get(name)
        expected_version = bootstrap.get(name)
        if subject is None or expected_version is None:
            raise ValueError(f"candidate environment is missing bootstrap {name}")
        if Version(subject.version) != Version(expected_version):
            raise ValueError(
                f"bootstrap {name} is {subject.version}, expected {expected_version}"
            )

    constraints = {
        canonicalize_name(dependency.name): dependency.version
        for dependency in read_pins(constraints_path)
    }
    requested_extras: dict[str, frozenset[str]] = {
        "athena": PROFILES[profile],
    }
    processed: dict[str, frozenset[str]] = {}
    queue: deque[str] = deque(["athena"])
    reachable = {"athena"}
    edge_names: dict[str, set[str]] = {}

    while queue:
        parent_name = queue.popleft()
        parent = actual.get(parent_name)
        if parent is None:
            raise ValueError(f"candidate environment is missing {parent_name}")
        extras = requested_extras.get(parent_name, frozenset())
        if processed.get(parent_name) == extras:
            continue
        unknown_extras = extras - parent.extras
        if unknown_extras:
            raise ValueError(
                f"{parent.name} does not provide requested extras: "
                + ", ".join(sorted(unknown_extras))
            )
        processed[parent_name] = extras
        active_children: set[str] = set()
        for raw in parent.requires:
            try:
                requirement = Requirement(raw)
            except InvalidRequirement as exc:
                raise ValueError(
                    f"installed {parent.name} has invalid requirement {raw!r}"
                ) from exc
            if not _marker_applies(requirement, extras):
                continue
            child_name = canonicalize_name(requirement.name)
            child = actual.get(child_name)
            if child is None:
                raise ValueError(
                    f"{parent.name} requires missing distribution {requirement.name}"
                )
            if (
                requirement.specifier
                and Version(child.version) not in requirement.specifier
            ):
                raise ValueError(
                    f"{parent.name} requires {requirement}, got {child.version}"
                )
            active_children.add(child_name)
            reachable.add(child_name)
            next_extras = requested_extras.get(child_name, frozenset()).union(
                canonicalize_name(extra) for extra in requirement.extras
            )
            if requested_extras.get(child_name) != next_extras:
                requested_extras[child_name] = frozenset(next_extras)
                queue.append(child_name)
            elif child_name not in processed:
                queue.append(child_name)
        edge_names[parent_name] = active_children

    allowed = reachable | set(BOOTSTRAP_NAMES)
    missing = sorted(reachable - set(actual))
    unexpected = sorted(set(actual) - allowed)
    if missing or unexpected:
        raise ValueError(
            "candidate distributions differ from the selected runtime closure"
            + (f"; missing: {', '.join(missing)}" if missing else "")
            + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
        )

    third_party_names = reachable - {"athena"}
    for name in sorted(third_party_names):
        subject = actual[name]
        expected_version = constraints.get(name)
        if expected_version is None:
            raise ValueError(f"runtime distribution {name} has no CI constraint pin")
        if Version(subject.version) != Version(expected_version):
            raise ValueError(
                f"runtime distribution {name} is {subject.version}, "
                f"expected constrained {expected_version}"
            )

    subjects = {name: actual[name] for name in reachable}
    refs = {
        name: wheel.root_ref if name == "athena" else _ref(subjects[name])
        for name in subjects
    }
    edges = {
        refs[name]: frozenset(refs[child] for child in edge_names.get(name, set()))
        for name in subjects
    }
    if _sha256(wheel.path) != wheel.digest:
        raise ValueError("wheel changed while the candidate environment was verified")
    return RuntimeClosure(
        profile=profile,
        subjects=subjects,
        edges=edges,
        wheel=wheel,
    )


def _component_versions(document: dict[str, Any]) -> dict[str, str]:
    components = document.get("components")
    if not isinstance(components, list):
        raise ValueError("CycloneDX document has no component list")
    actual: dict[str, str] = {}
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "library":
            raise ValueError("CycloneDX component is not a library object")
        name = component.get("name")
        version = component.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("CycloneDX component lacks a string name or version")
        normalized = canonicalize_name(name)
        if normalized in actual:
            raise ValueError(f"CycloneDX duplicates component {name!r}")
        actual[normalized] = version
    return actual


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    path = _require_regular_file(path, label=label, max_bytes=MAX_JSON_BYTES)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} root is not an object")
    return document


def verify_raw_audit(path: Path, closure: RuntimeClosure) -> int:
    """Require pip-audit output to equal the exact third-party closure."""
    document = _load_json(path, label="pip-audit output")
    if document.get("bomFormat") != "CycloneDX":
        raise ValueError("pip-audit output is not CycloneDX")
    expected = {name: subject.version for name, subject in closure.third_party.items()}
    actual = _component_versions(document)
    if expected != actual:
        raise ValueError("pip-audit components differ from the runtime closure")
    vulnerabilities = document.get("vulnerabilities", [])
    if not isinstance(vulnerabilities, list):
        raise ValueError("pip-audit vulnerabilities field is not a list")
    if vulnerabilities:
        identifiers = sorted(
            str(item.get("id", "<missing-id>"))
            if isinstance(item, dict)
            else "<malformed>"
            for item in vulnerabilities
        )
        raise ValueError(
            "pip-audit reported vulnerabilities: " + ", ".join(identifiers)
        )
    return len(expected)


def run_runtime_audit(
    auditor_python: Path,
    closure: RuntimeClosure,
    output: Path,
    *,
    timeout_seconds: int = 30,
) -> int:
    """Run strict pip-audit against exact third-party pins, then promote output."""
    _require_external(auditor_python, label="audit interpreter")
    _require_external(output, label="audit output")
    if timeout_seconds < 1 or timeout_seconds > 300:
        raise ValueError("audit timeout must be between 1 and 300 seconds")
    output = _prepare_external_output(
        output,
        label="audit output",
        protected=(closure.wheel.path, auditor_python),
    )
    candidate = _temporary_output(output)
    requirement_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".athena-wheel-audit.",
            suffix=".txt",
            dir=output.parent,
            delete=False,
        ) as stream:
            for name in sorted(closure.third_party):
                subject = closure.third_party[name]
                stream.write(f"{subject.name}=={subject.version}\n")
            requirement_path = Path(stream.name)
        command = [
            str(auditor_python.absolute()),
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
            str(requirement_path),
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
        count = verify_raw_audit(candidate, closure)
        if result.returncode != 0:
            raise RuntimeError(f"pip-audit failed with exit code {result.returncode}")
        candidate.replace(output)
        if _sha256(closure.wheel.path) != closure.wheel.digest:
            output.unlink(missing_ok=True)
            raise ValueError("wheel changed during the advisory scan")
        return count
    finally:
        if requirement_path is not None:
            requirement_path.unlink(missing_ok=True)
        candidate.unlink(missing_ok=True)


def _properties(values: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"name": f"athena:{name}", "value": value}
        for name, value in sorted(values.items())
    ]


def build_runtime_sbom(
    closure: RuntimeClosure,
    raw_audit: Path,
    output: Path,
    *,
    source_revision: str,
) -> int:
    """Create a wheel-rooted CycloneDX document from verified audit evidence."""
    verify_raw_audit(raw_audit, closure)
    if not _GIT_OID.fullmatch(source_revision):
        raise ValueError("source revision is not a full hexadecimal Git object id")
    output = _prepare_external_output(
        output,
        label="runtime SBOM output",
        protected=(closure.wheel.path, raw_audit),
    )

    components = []
    for name in sorted(closure.third_party):
        subject = closure.third_party[name]
        reference = _ref(subject)
        components.append(
            {
                "type": "library",
                "bom-ref": reference,
                "name": subject.name,
                "version": subject.version,
                "purl": reference,
            }
        )
    root = {
        "type": "application",
        "bom-ref": closure.wheel.root_ref,
        "name": closure.wheel.name,
        "version": closure.wheel.version,
        "purl": closure.wheel.root_ref,
        "hashes": [{"alg": "SHA-256", "content": closure.wheel.digest}],
        "properties": _properties(
            {
                "profile": closure.profile,
                "python": (
                    f"CPython {sys.version_info.major}.{sys.version_info.minor}."
                    f"{sys.version_info.micro}"
                ),
                "platform": f"{sys.platform}-{os.uname().machine}",
                "source-revision": source_revision,
                "wheel": closure.wheel.path.name,
                "advisory-scope": "third-party-python-runtime-distributions",
                "excluded": "athena-code,bootstrap,build,dev,scanner,native-system",
            }
        ),
    }
    dependencies = [
        {"ref": reference, "dependsOn": sorted(children)}
        for reference, children in sorted(closure.edges.items())
    ]
    document = {
        "$schema": CYCLONEDX_SCHEMA,
        "bomFormat": "CycloneDX",
        "specVersion": "1.4",
        "serialNumber": f"urn:uuid:{uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "component": root,
        },
        "components": components,
        "dependencies": dependencies,
        "vulnerabilities": [],
    }
    candidate = _temporary_output(output)
    candidate.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        count = verify_runtime_sbom(candidate, closure)
        candidate.replace(output)
        return count
    finally:
        candidate.unlink(missing_ok=True)


def _property_map(component: dict[str, Any]) -> dict[str, str]:
    properties = component.get("properties")
    if not isinstance(properties, list):
        raise ValueError("Athena SBOM root lacks properties")
    result: dict[str, str] = {}
    for item in properties:
        if not isinstance(item, dict):
            raise ValueError("Athena SBOM root has malformed property")
        name, value = item.get("name"), item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("Athena SBOM root has malformed property")
        if name in result:
            raise ValueError(f"Athena SBOM root duplicates property {name!r}")
        result[name] = value
    return result


def _validate_cpython_version(
    value: Any,
    *,
    requires_python: str,
    label: str,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        version = Version(value)
    except InvalidVersion as exc:
        raise ValueError(f"{label} is invalid") from exc
    if (
        str(version) != value
        or len(version.release) != 3
        or version.epoch != 0
        or version.pre is not None
        or version.post is not None
        or version.dev is not None
        or version.local is not None
    ):
        raise ValueError(f"{label} is invalid")
    supported_python = Requirement(f"python{requires_python}").specifier
    if version not in supported_python:
        raise ValueError(f"{label} is incompatible with wheel")
    return value


def verify_runtime_sbom(path: Path, closure: RuntimeClosure) -> int:
    """Semantically verify a final wheel-rooted CycloneDX document."""
    document = _load_json(path, label="runtime SBOM")
    if document.get("$schema") != CYCLONEDX_SCHEMA:
        raise ValueError("runtime SBOM has the wrong CycloneDX schema")
    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.4":
        raise ValueError("runtime SBOM is not CycloneDX 1.4")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("runtime SBOM has an invalid document version")
    serial = document.get("serialNumber")
    if not isinstance(serial, str) or not serial.startswith("urn:uuid:"):
        raise ValueError("runtime SBOM lacks a UUID serial number")
    try:
        parsed_uuid = UUID(serial.removeprefix("urn:uuid:"))
    except ValueError as exc:
        raise ValueError("runtime SBOM serial number is invalid") from exc
    if serial != f"urn:uuid:{parsed_uuid}":
        raise ValueError("runtime SBOM serial number is not canonical")

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("runtime SBOM lacks metadata")
    root = metadata.get("component")
    if not isinstance(root, dict) or root.get("type") != "application":
        raise ValueError("runtime SBOM lacks an Athena application root")
    if (
        canonicalize_name(str(root.get("name", ""))) != "athena"
        or str(root.get("version", "")) != closure.wheel.version
        or root.get("bom-ref") != closure.wheel.root_ref
        or root.get("purl") != closure.wheel.root_ref
    ):
        raise ValueError("runtime SBOM root identity differs from the wheel")
    if root.get("hashes") != [{"alg": "SHA-256", "content": closure.wheel.digest}]:
        raise ValueError("runtime SBOM root hash differs from the wheel")
    properties = _property_map(root)
    if properties.get("athena:profile") != closure.profile:
        raise ValueError("runtime SBOM root profile differs from the closure")
    if properties.get("athena:wheel") != closure.wheel.path.name:
        raise ValueError("runtime SBOM root filename differs from the wheel")
    if not _GIT_OID.fullmatch(properties.get("athena:source-revision", "")):
        raise ValueError("runtime SBOM root source revision is invalid")
    if (
        properties.get("athena:advisory-scope")
        != "third-party-python-runtime-distributions"
    ):
        raise ValueError("runtime SBOM root advisory scope is invalid")
    if (
        properties.get("athena:excluded")
        != "athena-code,bootstrap,build,dev,scanner,native-system"
    ):
        raise ValueError("runtime SBOM root exclusions are invalid")
    python_identity = properties.get("athena:python", "")
    if not python_identity.startswith("CPython "):
        raise ValueError("runtime SBOM root Python identity is invalid")
    _validate_cpython_version(
        python_identity.removeprefix("CPython "),
        requires_python=closure.wheel.requires_python,
        label="runtime SBOM root Python identity",
    )
    if properties.get("athena:platform") != f"{sys.platform}-{os.uname().machine}":
        raise ValueError("runtime SBOM root platform identity is invalid")

    expected = {name: subject.version for name, subject in closure.third_party.items()}
    if _component_versions(document) != expected:
        raise ValueError("runtime SBOM components differ from the closure")
    declared_refs = {closure.wheel.root_ref}
    for component in document["components"]:
        name = canonicalize_name(component["name"])
        expected_ref = _ref(closure.third_party[name])
        if (
            component.get("bom-ref") != expected_ref
            or component.get("purl") != expected_ref
        ):
            raise ValueError(
                f"runtime SBOM component reference differs for {component['name']!r}"
            )
        declared_refs.add(expected_ref)
    dependency_items = document.get("dependencies")
    if not isinstance(dependency_items, list):
        raise ValueError("runtime SBOM lacks dependency edges")
    actual_edges: dict[str, frozenset[str]] = {}
    for item in dependency_items:
        if not isinstance(item, dict):
            raise ValueError("runtime SBOM dependency entry is malformed")
        reference = item.get("ref")
        children = item.get("dependsOn")
        if (
            not isinstance(reference, str)
            or not isinstance(children, list)
            or not all(isinstance(child, str) for child in children)
        ):
            raise ValueError("runtime SBOM dependency entry is malformed")
        if reference in actual_edges:
            raise ValueError(f"runtime SBOM duplicates dependency ref {reference!r}")
        actual_edges[reference] = frozenset(children)
    if actual_edges != closure.edges:
        raise ValueError("runtime SBOM dependency graph differs from the closure")
    if set(actual_edges) != declared_refs:
        raise ValueError("runtime SBOM dependency subjects differ from components")
    dangling = sorted(
        child
        for children in actual_edges.values()
        for child in children - declared_refs
    )
    if dangling:
        raise ValueError("runtime SBOM contains dangling dependency refs")
    vulnerabilities = document.get("vulnerabilities")
    if vulnerabilities != []:
        raise ValueError("runtime SBOM must contain no vulnerability entries")
    return len(expected)


def audit_profile(
    *,
    auditor_python: Path,
    wheel: Path,
    site_packages: Path,
    profile: str,
    constraints: Path,
    bootstrap: Path,
    raw_output: Path,
    sbom_output: Path,
    source_revision: str,
    timeout_seconds: int,
) -> int:
    """Validate one installed profile, audit it, and emit its final SBOM."""
    closure = verify_runtime(
        wheel,
        site_packages,
        profile,
        constraints,
        bootstrap,
    )
    run_runtime_audit(
        auditor_python,
        closure,
        raw_output,
        timeout_seconds=timeout_seconds,
    )
    return build_runtime_sbom(
        closure,
        raw_output,
        sbom_output,
        source_revision=source_revision,
    )


def _manifest_artifact(path: Path) -> dict[str, Any]:
    path = _require_regular_file(path, label="candidate artifact")
    return {
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _validate_identity(value: str, *, label: str, optional: bool = False) -> str:
    if optional and not value:
        return ""
    if not _GIT_OID.fullmatch(value):
        raise ValueError(f"{label} must be a full hexadecimal Git object id")
    return value


def _validate_manifest_environment(
    value: Any,
    *,
    wheel: WheelSubject,
) -> tuple[str, str]:
    expected_keys = {"python", "implementation", "platform"}
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise ValueError("candidate manifest environment is invalid")
    if value.get("implementation") != "cpython":
        raise ValueError("candidate manifest implementation is not CPython")
    python_version = _validate_cpython_version(
        value.get("python"),
        requires_python=wheel.requires_python,
        label="candidate manifest Python identity",
    )
    platform_identity = value.get("platform")
    if platform_identity != f"{sys.platform}-{os.uname().machine}":
        raise ValueError("candidate manifest platform identity is invalid")
    return f"CPython {python_version}", str(platform_identity)


def _verify_bundle_sbom_identity(
    path: Path,
    *,
    profile: str,
    wheel: WheelSubject,
    source_revision: str,
    python_identity: str,
    platform_identity: str,
) -> None:
    """Verify self-contained SBOM identity without reconstructing an environment."""
    document = _load_json(path, label=f"{profile} runtime SBOM")
    if (
        document.get("$schema") != CYCLONEDX_SCHEMA
        or document.get("bomFormat") != "CycloneDX"
        or document.get("specVersion") != "1.4"
    ):
        raise ValueError(f"{profile} runtime SBOM identity is invalid")
    metadata = document.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    if (
        not isinstance(root, dict)
        or root.get("type") != "application"
        or canonicalize_name(str(root.get("name", ""))) != "athena"
        or str(root.get("version", "")) != wheel.version
        or root.get("bom-ref") != wheel.root_ref
        or root.get("purl") != wheel.root_ref
        or root.get("hashes") != [{"alg": "SHA-256", "content": wheel.digest}]
    ):
        raise ValueError(f"{profile} runtime SBOM root differs from the wheel")
    properties = _property_map(root)
    if properties.get("athena:profile") != profile:
        raise ValueError(f"{profile} runtime SBOM has the wrong profile")
    if properties.get("athena:wheel") != wheel.path.name:
        raise ValueError(f"{profile} runtime SBOM has the wrong wheel filename")
    if properties.get("athena:source-revision") != source_revision:
        raise ValueError(
            f"{profile} runtime SBOM source revision differs from the manifest"
        )
    if (
        properties.get("athena:python") != python_identity
        or properties.get("athena:platform") != platform_identity
    ):
        raise ValueError(
            f"{profile} runtime SBOM producer differs from the manifest environment"
        )
    if (
        properties.get("athena:advisory-scope")
        != "third-party-python-runtime-distributions"
        or properties.get("athena:excluded")
        != "athena-code,bootstrap,build,dev,scanner,native-system"
    ):
        raise ValueError(f"{profile} runtime SBOM scope is invalid")

    components = document.get("components")
    if not isinstance(components, list) or not components:
        raise ValueError(f"{profile} runtime SBOM has no components")
    declared_refs = {wheel.root_ref}
    seen_names: set[str] = set()
    for component in components:
        if not isinstance(component, dict) or component.get("type") != "library":
            raise ValueError(f"{profile} runtime SBOM component is malformed")
        name = component.get("name")
        version = component.get("version")
        reference = component.get("bom-ref")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError(f"{profile} runtime SBOM component is malformed")
        normalized = canonicalize_name(name)
        if normalized in seen_names:
            raise ValueError(f"{profile} runtime SBOM duplicates {name!r}")
        seen_names.add(normalized)
        expected_ref = f"pkg:pypi/{normalized}@{Version(version)}"
        if reference != expected_ref or component.get("purl") != expected_ref:
            raise ValueError(f"{profile} runtime SBOM component reference is invalid")
        if reference in declared_refs:
            raise ValueError(f"{profile} runtime SBOM duplicates a component ref")
        declared_refs.add(reference)

    dependencies = document.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError(f"{profile} runtime SBOM has no dependency graph")
    actual_refs: set[str] = set()
    all_children: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError(f"{profile} runtime SBOM dependency is malformed")
        reference = dependency.get("ref")
        children = dependency.get("dependsOn")
        if (
            not isinstance(reference, str)
            or not isinstance(children, list)
            or not all(isinstance(child, str) for child in children)
        ):
            raise ValueError(f"{profile} runtime SBOM dependency is malformed")
        if reference in actual_refs:
            raise ValueError(f"{profile} runtime SBOM duplicates a dependency ref")
        actual_refs.add(reference)
        all_children.update(children)
    if actual_refs != declared_refs or not all_children.issubset(declared_refs):
        raise ValueError(f"{profile} runtime SBOM graph is not component-complete")
    if document.get("vulnerabilities") != []:
        raise ValueError(f"{profile} runtime SBOM has vulnerability entries")


def create_bundle(
    *,
    output: Path,
    sdist: Path,
    sdist_sha256: str,
    wheel: Path,
    input_sbom: Path,
    base_sbom: Path,
    mcp_sbom: Path,
    constraints: Path,
    bootstrap: Path,
    tool_lock: Path,
    base_python: Path,
    mcp_python: Path,
    repository: str,
    event: str,
    run_id: str,
    run_attempt: str,
    checkout_commit: str,
    checkout_tree: str,
    pr_head: str,
    pr_base: str,
) -> int:
    """Promote an exact, allowlisted candidate bundle after semantic checks."""
    output = _new_external_directory(output, label="candidate bundle")
    if not repository or "/" not in repository:
        raise ValueError("repository identity is invalid")
    if not event or not run_id.isdigit() or not run_attempt.isdigit():
        raise ValueError("workflow run identity is invalid")
    identities = {
        "checkout_commit": _validate_identity(checkout_commit, label="checkout commit"),
        "checkout_tree": _validate_identity(checkout_tree, label="checkout tree"),
        "pr_head": _validate_identity(pr_head, label="PR head", optional=True),
        "pr_base": _validate_identity(pr_base, label="PR base", optional=True),
    }
    if bool(pr_head) != bool(pr_base):
        raise ValueError("PR head and base identities must be supplied together")

    inputs = {
        "sdist": _require_regular_file(sdist, label="sdist"),
        "wheel": _require_regular_file(wheel, label="wheel"),
        "input_sbom": _require_regular_file(
            input_sbom,
            label="input SBOM",
            max_bytes=MAX_JSON_BYTES,
        ),
        "base_sbom": _require_regular_file(
            base_sbom, label="base runtime SBOM", max_bytes=MAX_JSON_BYTES
        ),
        "mcp_sbom": _require_regular_file(
            mcp_sbom, label="MCP runtime SBOM", max_bytes=MAX_JSON_BYTES
        ),
    }
    for path in inputs.values():
        _require_external(path, label="candidate input")
    constraints = _require_regular_file(constraints, label="CI constraints")
    bootstrap = _require_regular_file(bootstrap, label="bootstrap lock")
    tool_lock = _require_regular_file(tool_lock, label="evidence tool lock")
    wheel_subject = inspect_wheel(inputs["wheel"])
    inspect_sdist(
        inputs["sdist"],
        wheel=wheel_subject,
        tool_lock=tool_lock,
        expected_digest=sdist_sha256,
    )
    if inputs["input_sbom"].name != "athena-ci-python.cdx.json":
        raise ValueError("input SBOM filename is not canonical")
    _SUPPLY_CHAIN.verify_sbom(inputs["input_sbom"])
    if inputs["base_sbom"].name != "athena-runtime-base-python312.cdx.json":
        raise ValueError("base SBOM filename is not canonical")
    if inputs["mcp_sbom"].name != "athena-runtime-mcp-python312.cdx.json":
        raise ValueError("MCP SBOM filename is not canonical")
    closures: dict[str, RuntimeClosure] = {}
    for profile, path, candidate_python in (
        ("base", inputs["base_sbom"], base_python),
        ("mcp", inputs["mcp_sbom"], mcp_python),
    ):
        closure = verify_runtime(
            inputs["wheel"],
            _site_packages_from_python(
                candidate_python,
                requires_python=wheel_subject.requires_python,
            ),
            profile,
            constraints,
            bootstrap,
        )
        verify_runtime_sbom(path, closure)
        closures[profile] = closure

    candidate = output.with_name(f".{output.name}.candidate-{uuid4().hex}")
    candidate.mkdir(parents=True, mode=0o700)
    try:
        copied: dict[str, Path] = {}
        for key, source in inputs.items():
            destination = candidate / source.name
            shutil.copyfile(source, destination)
            copied[key] = destination
        copied_wheel = inspect_wheel(copied["wheel"])
        if copied_wheel.digest != wheel_subject.digest:
            raise ValueError("wheel changed while the candidate bundle was promoted")
        inspect_sdist(
            copied["sdist"],
            wheel=copied_wheel,
            tool_lock=tool_lock,
            expected_digest=sdist_sha256,
        )
        for profile, key in (("base", "base_sbom"), ("mcp", "mcp_sbom")):
            verify_runtime_sbom(copied[key], closures[profile])
        manifest = {
            "schema": "athena.candidate.v1",
            "candidate": True,
            "published_release": False,
            "signed": False,
            "attested": False,
            "repository": repository,
            "workflow": {
                "event": event,
                "run_id": run_id,
                "run_attempt": run_attempt,
                **identities,
            },
            "environment": {
                "python": sys.version.split()[0],
                "implementation": sys.implementation.name,
                "platform": f"{sys.platform}-{os.uname().machine}",
            },
            "inputs": {
                "bootstrap": _manifest_artifact(bootstrap),
                "constraints": _manifest_artifact(constraints),
                "evidence_tool_lock": _manifest_artifact(tool_lock),
            },
            "artifacts": {
                key: _manifest_artifact(path) for key, path in copied.items()
            },
            "subject": {
                "name": wheel_subject.name,
                "version": wheel_subject.version,
                "sdist_sha256": sdist_sha256,
                "wheel_sha256": wheel_subject.digest,
            },
        }
        manifest_path = candidate / "athena-candidate.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        checksum_paths = [*copied.values(), manifest_path]
        sums_path = candidate / "SHA256SUMS"
        sums_path.write_text(
            "".join(
                f"{_sha256(path)}  {path.name}\n"
                for path in sorted(checksum_paths, key=lambda item: item.name)
            ),
            encoding="utf-8",
        )
        verify_bundle(
            candidate,
            constraints=constraints,
            bootstrap=bootstrap,
            tool_lock=tool_lock,
            base_python=base_python,
            mcp_python=mcp_python,
            installed_wheel=inputs["wheel"],
        )
        candidate.replace(output)
        try:
            return verify_bundle(
                output,
                constraints=constraints,
                bootstrap=bootstrap,
                tool_lock=tool_lock,
                base_python=base_python,
                mcp_python=mcp_python,
                installed_wheel=inputs["wheel"],
            )
        except Exception:
            shutil.rmtree(output)
            raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def verify_bundle(
    output: Path,
    *,
    constraints: Path,
    bootstrap: Path,
    tool_lock: Path,
    base_python: Path,
    mcp_python: Path,
    installed_wheel: Path | None = None,
) -> int:
    """Verify the final candidate directory's allowlist, sums, and manifest."""
    constraints = _require_regular_file(constraints, label="CI constraints")
    bootstrap = _require_regular_file(bootstrap, label="bootstrap lock")
    tool_lock = _require_regular_file(tool_lock, label="evidence tool lock")
    try:
        mode = output.lstat().st_mode
    except OSError as exc:
        raise ValueError("candidate bundle is not readable") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError("candidate bundle must be a real directory")
    _require_external(output, label="candidate bundle")
    entries = list(output.iterdir())
    if any(not stat.S_ISREG(path.lstat().st_mode) for path in entries):
        raise ValueError("candidate bundle contains a non-regular entry")
    sdists = [
        path
        for path in entries
        if path.name.startswith("athena-") and path.name.endswith(".tar.gz")
    ]
    wheels = [
        path
        for path in entries
        if path.name.startswith("athena-") and path.name.endswith(".whl")
    ]
    expected_names = {
        "athena-ci-python.cdx.json",
        "athena-runtime-base-python312.cdx.json",
        "athena-runtime-mcp-python312.cdx.json",
        "athena-candidate.json",
        "SHA256SUMS",
    }
    if len(sdists) != 1 or len(wheels) != 1:
        raise ValueError("candidate bundle must contain exactly one sdist and wheel")
    expected_names.update({sdists[0].name, wheels[0].name})
    actual_names = {path.name for path in entries}
    if actual_names != expected_names:
        raise ValueError("candidate bundle differs from the exact artifact allowlist")
    for entry in entries:
        _require_regular_file(
            entry,
            label="candidate bundle entry",
            max_bytes=MAX_ARTIFACT_BYTES,
        )

    sums: dict[str, str] = {}
    for line in (output / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or not _HEX_SHA256.fullmatch(parts[0]):
            raise ValueError("SHA256SUMS contains a malformed row")
        digest, name = parts
        if name in sums:
            raise ValueError(f"SHA256SUMS duplicates {name!r}")
        sums[name] = digest
    expected_sum_names = actual_names - {"SHA256SUMS"}
    if set(sums) != expected_sum_names:
        raise ValueError("SHA256SUMS membership differs from the candidate bundle")
    for name, digest in sums.items():
        if _sha256(output / name) != digest:
            raise ValueError(f"SHA256SUMS mismatch for {name!r}")

    manifest = _load_json(output / "athena-candidate.json", label="candidate manifest")
    if manifest.get("schema") != "athena.candidate.v1":
        raise ValueError("candidate manifest has the wrong schema")
    if (
        manifest.get("candidate") is not True
        or manifest.get("published_release") is not False
        or manifest.get("signed") is not False
        or manifest.get("attested") is not False
    ):
        raise ValueError("candidate manifest overstates release status")
    repository = manifest.get("repository")
    if not isinstance(repository, str) or "/" not in repository:
        raise ValueError("candidate manifest repository identity is invalid")
    workflow = manifest.get("workflow")
    if not isinstance(workflow, dict):
        raise ValueError("candidate manifest lacks workflow identity")
    if (
        not isinstance(workflow.get("event"), str)
        or not workflow["event"]
        or not isinstance(workflow.get("run_id"), str)
        or not workflow["run_id"].isdigit()
        or not isinstance(workflow.get("run_attempt"), str)
        or not workflow["run_attempt"].isdigit()
    ):
        raise ValueError("candidate manifest workflow run identity is invalid")
    manifest_checkout_commit = _validate_identity(
        str(workflow.get("checkout_commit", "")), label="manifest checkout commit"
    )
    _validate_identity(
        str(workflow.get("checkout_tree", "")), label="manifest checkout tree"
    )
    manifest_pr_head = str(workflow.get("pr_head", ""))
    manifest_pr_base = str(workflow.get("pr_base", ""))
    _validate_identity(manifest_pr_head, label="manifest PR head", optional=True)
    _validate_identity(manifest_pr_base, label="manifest PR base", optional=True)
    if bool(manifest_pr_head) != bool(manifest_pr_base):
        raise ValueError("candidate manifest PR identities are incomplete")
    artifacts = manifest.get("artifacts")
    expected_artifact_names = {
        "sdist": sdists[0].name,
        "wheel": wheels[0].name,
        "input_sbom": "athena-ci-python.cdx.json",
        "base_sbom": "athena-runtime-base-python312.cdx.json",
        "mcp_sbom": "athena-runtime-mcp-python312.cdx.json",
    }
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        expected_artifact_names
    ):
        raise ValueError("candidate manifest has the wrong artifact subjects")
    for key, expected_name in expected_artifact_names.items():
        item = artifacts[key]
        if not isinstance(item, dict) or item.get("filename") != expected_name:
            raise ValueError(f"candidate manifest {key} filename is invalid")
        path = output / expected_name
        if item.get("size") != path.stat().st_size or item.get("sha256") != _sha256(
            path
        ):
            raise ValueError(
                f"candidate manifest hash/size mismatch for {expected_name!r}"
            )
    inputs = manifest.get("inputs")
    expected_input_paths = {
        "bootstrap": bootstrap,
        "constraints": constraints,
        "evidence_tool_lock": tool_lock,
    }
    if not isinstance(inputs, dict) or set(inputs) != set(expected_input_paths):
        raise ValueError("candidate manifest has the wrong input identities")
    for key, path in expected_input_paths.items():
        item = inputs[key]
        if not isinstance(item, dict):
            raise ValueError(f"candidate manifest lacks {key}")
        if (
            item.get("filename") != path.name
            or item.get("sha256") != _sha256(path)
            or item.get("size") != path.stat().st_size
        ):
            raise ValueError(f"candidate manifest {key} identity mismatch")
    wheel_subject = inspect_wheel(wheels[0], require_external=True)
    python_identity, platform_identity = _validate_manifest_environment(
        manifest.get("environment"),
        wheel=wheel_subject,
    )
    manifest_sdist_digest = artifacts["sdist"]["sha256"]
    if not isinstance(manifest_sdist_digest, str):
        raise ValueError("candidate manifest sdist digest is invalid")
    inspect_sdist(
        sdists[0],
        wheel=wheel_subject,
        tool_lock=tool_lock,
        expected_digest=manifest_sdist_digest,
        require_external=True,
    )
    _SUPPLY_CHAIN.verify_sbom(output / "athena-ci-python.cdx.json")
    runtime_wheel = installed_wheel or wheels[0]
    runtime_wheel_subject = inspect_wheel(runtime_wheel, require_external=True)
    if runtime_wheel_subject.digest != wheel_subject.digest:
        raise ValueError("installed runtime wheel differs from the bundled wheel")
    for profile, filename, candidate_python in (
        ("base", "athena-runtime-base-python312.cdx.json", base_python),
        ("mcp", "athena-runtime-mcp-python312.cdx.json", mcp_python),
    ):
        closure = verify_runtime(
            runtime_wheel,
            _site_packages_from_python(
                candidate_python,
                requires_python=wheel_subject.requires_python,
            ),
            profile,
            constraints,
            bootstrap,
        )
        verify_runtime_sbom(output / filename, closure)
    _verify_bundle_sbom_identity(
        output / "athena-runtime-base-python312.cdx.json",
        profile="base",
        wheel=wheel_subject,
        source_revision=manifest_checkout_commit,
        python_identity=python_identity,
        platform_identity=platform_identity,
    )
    _verify_bundle_sbom_identity(
        output / "athena-runtime-mcp-python312.cdx.json",
        profile="mcp",
        wheel=wheel_subject,
        source_revision=manifest_checkout_commit,
        python_identity=python_identity,
        platform_identity=platform_identity,
    )
    subject = manifest.get("subject")
    if (
        not isinstance(subject, dict)
        or canonicalize_name(str(subject.get("name", ""))) != "athena"
        or str(subject.get("version", "")) != wheel_subject.version
        or subject.get("sdist_sha256") != manifest_sdist_digest
        or subject.get("wheel_sha256") != wheel_subject.digest
    ):
        raise ValueError("candidate manifest subject differs from the wheel")
    return len(entries)


def _site_packages_from_python(
    python: Path,
    *,
    requires_python: str,
) -> Path:
    _require_external(python, label="candidate interpreter")
    if sys.implementation.name != "cpython":
        raise ValueError("wheel evidence verifier is not CPython")
    verifier_version = _validate_cpython_version(
        sys.version.split()[0],
        requires_python=requires_python,
        label="wheel evidence verifier Python identity",
    )
    result = subprocess.run(
        [
            str(python.absolute()),
            "-I",
            "-c",
            (
                "import json, os, sys, sysconfig; "
                "print(json.dumps({"
                "'implementation': sys.implementation.name, "
                "'platform': f'{sys.platform}-{os.uname().machine}', "
                "'python': sys.version.split()[0], "
                "'site_packages': sysconfig.get_path('purelib')"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd="/tmp",
    )
    try:
        identity = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("candidate interpreter returned invalid identity") from exc
    expected_keys = {"implementation", "platform", "python", "site_packages"}
    if not isinstance(identity, dict) or set(identity) != expected_keys:
        raise ValueError("candidate interpreter returned invalid identity")
    if identity.get("implementation") != "cpython":
        raise ValueError("candidate interpreter is not CPython")
    candidate_version = _validate_cpython_version(
        identity.get("python"),
        requires_python=requires_python,
        label="candidate interpreter Python identity",
    )
    if candidate_version != verifier_version:
        raise ValueError("candidate interpreter version differs from verifier")
    if identity.get("platform") != f"{sys.platform}-{os.uname().machine}":
        raise ValueError("candidate interpreter platform differs from verifier")
    site_packages = identity.get("site_packages")
    if not isinstance(site_packages, str):
        raise ValueError("candidate interpreter returned invalid identity")
    path = Path(site_packages)
    if not path.is_absolute():
        raise ValueError("candidate interpreter returned a relative site-packages path")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="verify an Athena wheel and its exact runtime advisory evidence"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect-wheel")
    inspect_parser.add_argument("--wheel", required=True, type=Path)

    inspect_sdist_parser = subparsers.add_parser("inspect-sdist")
    inspect_sdist_parser.add_argument("--sdist", required=True, type=Path)
    inspect_sdist_parser.add_argument("--wheel", type=Path)
    inspect_sdist_parser.add_argument("--tool-lock", type=Path)
    inspect_sdist_parser.add_argument("--expected-sha256")

    audit_parser = subparsers.add_parser("audit-profile")
    audit_parser.add_argument("--auditor-python", required=True, type=Path)
    audit_parser.add_argument("--candidate-python", required=True, type=Path)
    audit_parser.add_argument("--wheel", required=True, type=Path)
    audit_parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    audit_parser.add_argument("--constraints", required=True, type=Path)
    audit_parser.add_argument("--bootstrap", required=True, type=Path)
    audit_parser.add_argument("--raw-output", required=True, type=Path)
    audit_parser.add_argument("--sbom-output", required=True, type=Path)
    audit_parser.add_argument("--source-revision", required=True)
    audit_parser.add_argument("--timeout", type=int, default=30)

    verify_parser = subparsers.add_parser("verify-profile")
    verify_parser.add_argument("--candidate-python", required=True, type=Path)
    verify_parser.add_argument("--wheel", required=True, type=Path)
    verify_parser.add_argument("--profile", required=True, choices=sorted(PROFILES))
    verify_parser.add_argument("--constraints", required=True, type=Path)
    verify_parser.add_argument("--bootstrap", required=True, type=Path)
    verify_parser.add_argument("--sbom", required=True, type=Path)

    bundle_parser = subparsers.add_parser("create-bundle")
    bundle_parser.add_argument("--output", required=True, type=Path)
    bundle_parser.add_argument("--sdist", required=True, type=Path)
    bundle_parser.add_argument("--sdist-sha256", required=True)
    bundle_parser.add_argument("--wheel", required=True, type=Path)
    bundle_parser.add_argument("--input-sbom", required=True, type=Path)
    bundle_parser.add_argument("--base-sbom", required=True, type=Path)
    bundle_parser.add_argument("--mcp-sbom", required=True, type=Path)
    bundle_parser.add_argument("--constraints", required=True, type=Path)
    bundle_parser.add_argument("--bootstrap", required=True, type=Path)
    bundle_parser.add_argument("--tool-lock", required=True, type=Path)
    bundle_parser.add_argument("--base-python", required=True, type=Path)
    bundle_parser.add_argument("--mcp-python", required=True, type=Path)
    bundle_parser.add_argument("--repository", required=True)
    bundle_parser.add_argument("--event", required=True)
    bundle_parser.add_argument("--run-id", required=True)
    bundle_parser.add_argument("--run-attempt", required=True)
    bundle_parser.add_argument("--checkout-commit", required=True)
    bundle_parser.add_argument("--checkout-tree", required=True)
    bundle_parser.add_argument("--pr-head", default="")
    bundle_parser.add_argument("--pr-base", default="")

    verify_bundle_parser = subparsers.add_parser("verify-bundle")
    verify_bundle_parser.add_argument("--bundle", required=True, type=Path)
    verify_bundle_parser.add_argument("--constraints", required=True, type=Path)
    verify_bundle_parser.add_argument("--bootstrap", required=True, type=Path)
    verify_bundle_parser.add_argument("--tool-lock", required=True, type=Path)
    verify_bundle_parser.add_argument("--base-python", required=True, type=Path)
    verify_bundle_parser.add_argument("--mcp-python", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect-wheel":
            subject = inspect_wheel(args.wheel)
            print(
                f"Athena wheel evidence passed: {subject.path.name}, "
                f"sha256:{subject.digest}"
            )
        elif args.command == "inspect-sdist":
            subject = inspect_sdist(
                args.sdist,
                wheel=inspect_wheel(args.wheel) if args.wheel else None,
                tool_lock=args.tool_lock,
                expected_digest=args.expected_sha256,
            )
            print(
                f"Athena sdist evidence passed: {subject.path.name}, "
                f"sha256:{subject.digest}"
            )
        elif args.command == "audit-profile":
            count = audit_profile(
                auditor_python=args.auditor_python,
                wheel=args.wheel,
                site_packages=_site_packages_from_python(
                    args.candidate_python,
                    requires_python=inspect_wheel(args.wheel).requires_python,
                ),
                profile=args.profile,
                constraints=args.constraints,
                bootstrap=args.bootstrap,
                raw_output=args.raw_output,
                sbom_output=args.sbom_output,
                source_revision=args.source_revision,
                timeout_seconds=args.timeout,
            )
            print(
                f"Athena {args.profile} wheel audit passed: {count} exact "
                "third-party distributions, zero known vulnerabilities"
            )
        elif args.command == "verify-profile":
            closure = verify_runtime(
                args.wheel,
                _site_packages_from_python(
                    args.candidate_python,
                    requires_python=inspect_wheel(args.wheel).requires_python,
                ),
                args.profile,
                args.constraints,
                args.bootstrap,
            )
            count = verify_runtime_sbom(args.sbom, closure)
            print(
                f"Athena {args.profile} runtime SBOM passed: {count} exact "
                "third-party distributions"
            )
        elif args.command == "create-bundle":
            count = create_bundle(
                output=args.output,
                sdist=args.sdist,
                sdist_sha256=args.sdist_sha256,
                wheel=args.wheel,
                input_sbom=args.input_sbom,
                base_sbom=args.base_sbom,
                mcp_sbom=args.mcp_sbom,
                constraints=args.constraints,
                bootstrap=args.bootstrap,
                tool_lock=args.tool_lock,
                base_python=args.base_python,
                mcp_python=args.mcp_python,
                repository=args.repository,
                event=args.event,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                checkout_commit=args.checkout_commit,
                checkout_tree=args.checkout_tree,
                pr_head=args.pr_head,
                pr_base=args.pr_base,
            )
            print(f"Athena candidate bundle passed: {count} exact files")
        else:
            count = verify_bundle(
                args.bundle,
                constraints=args.constraints,
                bootstrap=args.bootstrap,
                tool_lock=args.tool_lock,
                base_python=args.base_python,
                mcp_python=args.mcp_python,
            )
            print(f"Athena candidate bundle passed: {count} exact files")
    except (
        BadZipFile,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        subprocess.CalledProcessError,
        ValueError,
    ) as exc:
        print(f"Athena wheel evidence failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
