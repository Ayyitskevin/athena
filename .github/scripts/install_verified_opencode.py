#!/usr/bin/env python3
"""Install an exact release binary without trusting archive paths."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
from pathlib import Path
import re
import stat
import sys
import tarfile
import tempfile
from typing import BinaryIO


CHUNK_BYTES = 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
BINARY_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _expected_sha256(value: str, *, label: str) -> str:
    if SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be exactly 64 lowercase hexadecimal characters")
    return value


def _no_follow_flag() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("this installer requires O_NOFOLLOW support")
    return no_follow


def _copy_archive_snapshot(
    descriptor: int, snapshot: BinaryIO, *, expected_bytes: int
) -> str:
    digest = hashlib.sha256()
    observed_bytes = 0
    while chunk := os.read(descriptor, CHUNK_BYTES):
        observed_bytes += len(chunk)
        if observed_bytes > expected_bytes:
            raise ValueError(
                "archive grew beyond its expected byte count while reading"
            )
        digest.update(chunk)
        snapshot.write(chunk)
    if observed_bytes != expected_bytes:
        raise ValueError(
            f"archive yielded {observed_bytes} bytes, expected {expected_bytes}"
        )
    return digest.hexdigest()


def _archive_metadata_is_stable(before: os.stat_result, after: os.stat_result) -> bool:
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(
        getattr(before, field) == getattr(after, field) for field in stable_fields
    )


def _snapshot_verified_archive(
    archive_path: Path,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> BinaryIO:
    """Return an anonymous, immutable-by-path snapshot of verified archive bytes."""
    if expected_bytes <= 0:
        raise ValueError("expected archive byte count must be positive")
    expected_sha256 = _expected_sha256(
        expected_sha256, label="expected archive SHA-256"
    )
    flags = os.O_RDONLY | _no_follow_flag() | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(archive_path, flags)
    snapshot: BinaryIO | None = None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"archive is not a regular file: {archive_path}")
        if before.st_size != expected_bytes:
            raise ValueError(
                f"archive byte count is {before.st_size}, expected {expected_bytes}"
            )

        snapshot = tempfile.TemporaryFile(mode="w+b")
        observed_sha256 = _copy_archive_snapshot(
            descriptor, snapshot, expected_bytes=expected_bytes
        )

        after = os.fstat(descriptor)
        if not _archive_metadata_is_stable(before, after):
            raise ValueError("archive metadata changed while it was being verified")
        if not hmac.compare_digest(observed_sha256, expected_sha256):
            raise ValueError(
                f"archive SHA-256 is {observed_sha256}, expected {expected_sha256}"
            )
        snapshot.flush()
        snapshot.seek(0)
        return snapshot
    except BaseException:
        if snapshot is not None:
            snapshot.close()
        raise
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("failed to make progress while writing the release binary")
        view = view[written:]


def _verified_release_source(
    release: tarfile.TarFile,
    *,
    expected_binary_bytes: int,
    archive_member: str = "opencode",
    expected_member_count: int = 1,
) -> BinaryIO:
    if expected_member_count <= 0:
        raise ValueError("expected archive member count must be positive")
    if not archive_member:
        raise ValueError("expected archive member name must not be empty")

    selected: tarfile.TarInfo | None = None
    observed_members = 0
    while observed_members < expected_member_count:
        member = release.next()
        if member is None:
            raise ValueError(
                f"release archive contains {observed_members} members, "
                f"expected exactly {expected_member_count}"
            )
        observed_members += 1
        if member.name == archive_member:
            if selected is not None:
                raise ValueError(
                    f"release archive contains duplicate member {archive_member!r}"
                )
            selected = member

    if release.next() is not None:
        if expected_member_count == 1:
            raise ValueError(
                "release archive contains multiple members, expected exactly one"
            )
        raise ValueError(
            "release archive contains more than "
            f"{expected_member_count} members, expected exactly that many"
        )
    if selected is None:
        raise ValueError(
            f"release archive does not contain expected member {archive_member!r}"
        )
    if not selected.isreg():
        raise ValueError(
            f"release archive member {archive_member!r} is not a regular file"
        )
    if selected.size != expected_binary_bytes:
        raise ValueError(
            f"binary byte count is {selected.size}, expected {expected_binary_bytes}"
        )
    source = release.extractfile(selected)
    if source is None:
        raise ValueError(f"release archive member {archive_member!r} has no file data")
    return source


def _validated_binary_name(value: str) -> str:
    if BINARY_NAME.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(
            "binary name must be a single safe filename using letters, digits, '.', "
            "'_', or '-'"
        )
    return value


def _copy_verified_binary(
    source: BinaryIO,
    descriptor: int,
    *,
    expected_bytes: int,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    observed_bytes = 0
    with source:
        while chunk := source.read(CHUNK_BYTES):
            observed_bytes += len(chunk)
            if observed_bytes > expected_bytes:
                raise ValueError(
                    "binary exceeded its expected byte count while extracting"
                )
            digest.update(chunk)
            _write_all(descriptor, chunk)
    if observed_bytes != expected_bytes:
        raise ValueError(
            f"binary yielded {observed_bytes} bytes, expected {expected_bytes}"
        )
    observed_sha256 = digest.hexdigest()
    if not hmac.compare_digest(observed_sha256, expected_sha256):
        raise ValueError(
            f"binary SHA-256 is {observed_sha256}, expected {expected_sha256}"
        )


def _cleanup_failed_install(
    install_dir: Path,
    *,
    binary_name: str,
    directory_descriptor: int | None,
    binary_descriptor: int | None,
    binary_created: bool,
    directory_created: bool,
) -> list[str]:
    """Remove only paths this invocation proved that it created."""
    failures: list[str] = []
    if binary_descriptor is not None:
        try:
            os.close(binary_descriptor)
        except OSError as exc:
            failures.append(f"close partial binary: {exc}")
    if binary_created and directory_descriptor is not None:
        try:
            os.unlink(binary_name, dir_fd=directory_descriptor)
        except OSError as exc:
            failures.append(f"unlink partial binary: {exc}")
    if directory_descriptor is not None:
        try:
            os.close(directory_descriptor)
        except OSError as exc:
            failures.append(f"close install directory: {exc}")
    if directory_created:
        try:
            os.rmdir(install_dir)
        except OSError as exc:
            failures.append(f"remove install directory: {exc}")
    return failures


def install_verified_archive(
    archive_path: Path,
    install_dir: Path,
    *,
    expected_archive_bytes: int,
    expected_archive_sha256: str,
    expected_binary_bytes: int,
    expected_binary_sha256: str,
    archive_member: str = "opencode",
    binary_name: str = "opencode",
    expected_member_count: int = 1,
    label: str = "OpenCode",
) -> Path:
    """Verify one archive member and install it as a private executable."""
    if expected_binary_bytes <= 0:
        raise ValueError("expected binary byte count must be positive")
    binary_name = _validated_binary_name(binary_name)
    if not label.strip():
        raise ValueError("binary label must not be empty")
    expected_binary_sha256 = _expected_sha256(
        expected_binary_sha256, label="expected binary SHA-256"
    )

    with _snapshot_verified_archive(
        archive_path,
        expected_bytes=expected_archive_bytes,
        expected_sha256=expected_archive_sha256,
    ) as snapshot:
        with tarfile.open(fileobj=snapshot, mode="r:gz") as release:
            source = _verified_release_source(
                release,
                expected_binary_bytes=expected_binary_bytes,
                archive_member=archive_member,
                expected_member_count=expected_member_count,
            )

            directory_created = False
            binary_created = False
            directory_descriptor: int | None = None
            binary_descriptor: int | None = None
            try:
                os.mkdir(install_dir, mode=0o700)
                directory_created = True
                no_follow = _no_follow_flag()
                directory_flag = getattr(os, "O_DIRECTORY", None)
                if directory_flag is None:
                    raise OSError("this installer requires O_DIRECTORY support")
                directory_descriptor = os.open(
                    install_dir,
                    os.O_RDONLY
                    | no_follow
                    | directory_flag
                    | getattr(os, "O_CLOEXEC", 0),
                )
                os.fchmod(directory_descriptor, 0o700)
                binary_descriptor = os.open(
                    binary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | no_follow
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_descriptor,
                )
                binary_created = True

                _copy_verified_binary(
                    source,
                    binary_descriptor,
                    expected_bytes=expected_binary_bytes,
                    expected_sha256=expected_binary_sha256,
                )

                os.fchmod(binary_descriptor, 0o700)
                os.fsync(binary_descriptor)
                os.close(binary_descriptor)
                binary_descriptor = None
                os.fsync(directory_descriptor)
                os.close(directory_descriptor)
                directory_descriptor = None
            except BaseException as exc:
                cleanup_failures = _cleanup_failed_install(
                    install_dir,
                    binary_name=binary_name,
                    directory_descriptor=directory_descriptor,
                    binary_descriptor=binary_descriptor,
                    binary_created=binary_created,
                    directory_created=directory_created,
                )
                if cleanup_failures:
                    raise RuntimeError(
                        f"{label} installation failed ({exc}); cleanup also failed: "
                        + "; ".join(cleanup_failures)
                    ) from exc
                raise

    return install_dir / binary_name


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("install_dir", type=Path)
    parser.add_argument("--expected-archive-bytes", required=True, type=int)
    parser.add_argument("--expected-archive-sha256", required=True)
    parser.add_argument("--expected-binary-bytes", required=True, type=int)
    parser.add_argument("--expected-binary-sha256", required=True)
    parser.add_argument("--archive-member", default="opencode")
    parser.add_argument("--binary-name", default="opencode")
    parser.add_argument("--expected-member-count", default=1, type=int)
    parser.add_argument("--label", default="OpenCode")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    installed = install_verified_archive(
        arguments.archive,
        arguments.install_dir,
        expected_archive_bytes=arguments.expected_archive_bytes,
        expected_archive_sha256=arguments.expected_archive_sha256,
        expected_binary_bytes=arguments.expected_binary_bytes,
        expected_binary_sha256=arguments.expected_binary_sha256,
        archive_member=arguments.archive_member,
        binary_name=arguments.binary_name,
        expected_member_count=arguments.expected_member_count,
        label=arguments.label,
    )
    print(f"installed verified {arguments.label} binary at {installed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"verified binary installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
