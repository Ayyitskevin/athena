#!/usr/bin/env python3
"""Run a checksum-pinned Gitleaks scan over the candidate's full ancestry.

The wrapper keeps findings redacted, validates the downloaded release archive,
and turns scanner or evidence failures into a closed gate instead of a silent pass.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Any
from urllib.request import Request, urlopen


GITLEAKS_VERSION = "8.30.1"
GITLEAKS_ARCHIVE_URL = (
    "https://github.com/gitleaks/gitleaks/releases/download/"
    f"v{GITLEAKS_VERSION}/gitleaks_{GITLEAKS_VERSION}_linux_x64.tar.gz"
)
GITLEAKS_ARCHIVE_SHA256 = (
    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
)
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 16
MAX_EXPANDED_BYTES = 48 * 1024 * 1024
MAX_BINARY_BYTES = 32 * 1024 * 1024
MAX_IGNORE_BYTES = 64 * 1024
MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_FINDINGS = 1_000
DOWNLOAD_TIMEOUT_SECONDS = 30
SCAN_TIMEOUT_SECONDS = 120
PROCESS_TIMEOUT_SECONDS = SCAN_TIMEOUT_SECONDS + 30
GIT_LOG_OPTIONS = "--diff-merges=remerge HEAD"
_RULE_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_COMMIT_ID = re.compile(r"[0-9a-f]{40,64}")


class ScanError(RuntimeError):
    """A fail-closed scanner, input, or evidence error with a safe message."""


@dataclass(frozen=True)
class Finding:
    rule_id: str
    path: str
    line: int
    commit: str

    @property
    def fingerprint(self) -> str:
        return f"{self.commit}:{self.path}:{self.rule_id}:{self.line}"


def _bounded_content_length(value: str | None) -> None:
    if value is None:
        return
    try:
        length = int(value)
    except ValueError:
        raise ScanError("scanner archive has an invalid content length") from None
    if length < 0 or length > MAX_ARCHIVE_BYTES:
        raise ScanError("scanner archive exceeds the download limit")


def download_archive(destination: Path) -> None:
    """Download the fixed release asset, enforcing size and digest bounds."""

    request = Request(
        GITLEAKS_ARCHIVE_URL,
        headers={"User-Agent": "athena-secret-history-gate/1"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        try:
            response = urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        except Exception as exc:
            raise ScanError(
                f"scanner archive download failed ({type(exc).__name__})"
            ) from None
        with response:
            _bounded_content_length(response.headers.get("Content-Length"))
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as target:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ScanError("scanner archive exceeds the download limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
        if total == 0:
            raise ScanError("scanner archive is empty")
        if not hmac.compare_digest(digest.hexdigest(), GITLEAKS_ARCHIVE_SHA256):
            raise ScanError(
                "scanner archive checksum does not match the pinned release"
            )
    except ScanError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise ScanError(
            f"scanner archive download failed ({type(exc).__name__})"
        ) from None


def _validate_archive_members(members: list[tarfile.TarInfo]) -> tarfile.TarInfo:
    if not 1 <= len(members) <= MAX_ARCHIVE_MEMBERS:
        raise ScanError("scanner archive has an invalid member count")

    names: set[str] = set()
    expanded_size = 0
    binary: tarfile.TarInfo | None = None
    for member in members:
        path = PurePosixPath(member.name)
        if (
            member.name != path.name
            or path.is_absolute()
            or path.name in {"", ".", ".."}
            or not member.isfile()
        ):
            raise ScanError("scanner archive contains an unsafe member")
        if member.name in names:
            raise ScanError("scanner archive contains a duplicate member")
        names.add(member.name)
        if member.size < 0:
            raise ScanError("scanner archive contains an invalid member size")
        expanded_size += member.size
        if expanded_size > MAX_EXPANDED_BYTES:
            raise ScanError("scanner archive exceeds the expansion limit")
        if member.name == "gitleaks":
            binary = member

    if binary is None or not 0 < binary.size <= MAX_BINARY_BYTES:
        raise ScanError("scanner archive does not contain the expected binary")
    return binary


def extract_binary(archive: Path, destination: Path) -> None:
    """Extract only the validated executable from the pinned release archive."""

    try:
        try:
            bundle = tarfile.open(archive, mode="r:gz")
        except (OSError, tarfile.TarError):
            raise ScanError("scanner archive is not a valid gzip tar archive") from None
        with bundle:
            binary_member = _validate_archive_members(bundle.getmembers())
            source = bundle.extractfile(binary_member)
            if source is None:
                raise ScanError("scanner binary cannot be read from the archive")
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o700,
            )
            copied = 0
            with source, os.fdopen(descriptor, "wb") as target:
                while chunk := source.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_BINARY_BYTES:
                        raise ScanError("scanner binary exceeds the extraction limit")
                    target.write(chunk)
                if copied != binary_member.size:
                    raise ScanError(
                        "scanner binary size does not match its archive entry"
                    )
                target.flush()
                os.fsync(target.fileno())
        os.chmod(destination, 0o700)
    except ScanError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise ScanError(
            f"scanner archive extraction failed ({type(exc).__name__})"
        ) from None


def _subprocess_environment() -> dict[str, str]:
    path = os.environ.get("PATH", os.defpath)
    return {
        "PATH": path,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_output(repository: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", os.fspath(repository), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScanError("repository history could not be inspected") from None
    if result.returncode != 0:
        raise ScanError("repository history could not be inspected")
    try:
        return result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ScanError("repository history returned an invalid response") from None


def validate_repository(repository: Path) -> Path:
    try:
        root = repository.resolve(strict=True)
    except OSError:
        raise ScanError("repository path does not exist") from None
    if not root.is_dir():
        raise ScanError("repository path is not a directory")
    if _git_output(root, "rev-parse", "--is-inside-work-tree") != "true":
        raise ScanError("repository path is not a Git worktree")
    if _git_output(root, "rev-parse", "--is-shallow-repository") != "false":
        raise ScanError("repository history is shallow; full ancestry is unavailable")
    head = _git_output(root, "rev-parse", "--verify", "HEAD")
    if not _COMMIT_ID.fullmatch(head):
        raise ScanError("repository HEAD is not a valid commit")
    try:
        remerge_probe = _git_output(
            root,
            "log",
            "--diff-merges=remerge",
            "--no-patch",
            "-1",
            "--format=%H",
            "HEAD",
        )
    except ScanError:
        raise ScanError("Git 2.36+ with remerge-diff support is required") from None
    if remerge_probe != head:
        raise ScanError("Git remerge-diff did not inspect the candidate HEAD")
    if _git_output(root, "rev-list", "--min-parents=3", "--max-count=1", "HEAD"):
        raise ScanError("octopus merges are unsupported by the full-ancestry scan")

    config = root / ".gitleaks.toml"
    if os.path.lexists(config):
        raise ScanError("repository-local Gitleaks configuration is not permitted")
    ignore = root / ".gitleaksignore"
    try:
        metadata = ignore.lstat()
    except OSError:
        raise ScanError("the reviewed Gitleaks allowlist is missing") from None
    if not stat.S_ISREG(metadata.st_mode):
        raise ScanError("the reviewed Gitleaks allowlist is not a regular file")
    if metadata.st_size > MAX_IGNORE_BYTES:
        raise ScanError("the reviewed Gitleaks allowlist exceeds its size limit")
    return root


def verify_binary(binary: Path) -> None:
    try:
        result = subprocess.run(
            [os.fspath(binary), "version"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScanError("scanner binary could not be executed") from None
    try:
        version = result.stdout.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ScanError("scanner binary returned an invalid version") from None
    if result.returncode != 0 or version != GITLEAKS_VERSION:
        raise ScanError("scanner binary version does not match the pinned release")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ScanError("scanner report contains a duplicate JSON key")
        result[key] = value
    return result


def _safe_path(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ScanError("scanner report contains an invalid file path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ScanError("scanner report contains an invalid file path")
    return value


def _finding_from_json(value: object) -> Finding:
    if not isinstance(value, dict):
        raise ScanError("scanner report contains an invalid finding")
    rule_id = value.get("RuleID")
    line = value.get("StartLine")
    commit = value.get("Commit")
    fingerprint = value.get("Fingerprint")
    if not isinstance(rule_id, str) or _RULE_ID.fullmatch(rule_id) is None:
        raise ScanError("scanner report contains an invalid rule identifier")
    if isinstance(line, bool) or not isinstance(line, int) or not 0 < line < 2**31:
        raise ScanError("scanner report contains an invalid line number")
    if not isinstance(commit, str) or _COMMIT_ID.fullmatch(commit) is None:
        raise ScanError("scanner report contains an invalid commit identifier")
    finding = Finding(
        rule_id=rule_id,
        path=_safe_path(value.get("File")),
        line=line,
        commit=commit,
    )
    if fingerprint != finding.fingerprint:
        raise ScanError("scanner report contains an inconsistent fingerprint")
    return finding


def read_report(report: Path) -> tuple[Finding, ...]:
    try:
        size = report.stat().st_size
    except OSError:
        raise ScanError("scanner did not produce a report") from None
    if not 0 < size <= MAX_REPORT_BYTES:
        raise ScanError("scanner report has an invalid size")
    try:
        document = json.loads(
            report.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except ScanError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ScanError("scanner report is not valid JSON") from None
    if not isinstance(document, list) or len(document) > MAX_FINDINGS:
        raise ScanError("scanner report has an invalid finding count")
    return tuple(_finding_from_json(item) for item in document)


def scan_repository(
    binary: Path, repository: Path, report: Path
) -> tuple[Finding, ...]:
    ignore = repository / ".gitleaksignore"
    command = [
        os.fspath(binary),
        "git",
        f"--log-opts={GIT_LOG_OPTIONS}",
        f"--gitleaks-ignore-path={ignore}",
        "--redact=100",
        "--no-banner",
        "--no-color",
        "--log-level=error",
        "--exit-code=1",
        "--report-format=json",
        f"--report-path={report}",
        f"--timeout={SCAN_TIMEOUT_SECONDS}",
        os.fspath(repository),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PROCESS_TIMEOUT_SECONDS,
            env=_subprocess_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ScanError("secret-history scanner did not complete") from None

    if result.returncode not in {0, 1}:
        raise ScanError("secret-history scanner exited unexpectedly")
    findings = read_report(report)
    if result.returncode == 0 and findings:
        raise ScanError("scanner exit status conflicts with its report")
    if result.returncode == 1 and not findings:
        raise ScanError("scanner exit status conflicts with its report")
    return findings


def run(repository: Path) -> tuple[Finding, ...]:
    root = validate_repository(repository)
    with tempfile.TemporaryDirectory(prefix="athena-gitleaks-") as temporary:
        private_root = Path(temporary)
        archive = private_root / "gitleaks.tar.gz"
        binary = private_root / "gitleaks"
        report = private_root / "report.json"
        download_archive(archive)
        extract_binary(archive, binary)
        verify_binary(binary)
        return scan_repository(binary, root, report)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="scan the full ancestry of an Athena candidate for secrets"
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="complete Git worktree to scan (default: current directory)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        findings = run(arguments.repository)
    except ScanError as exc:
        print(f"Secret history scan could not complete: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print(
            "Secret history scan could not complete: unexpected internal error",
            file=sys.stderr,
        )
        return 2

    if not findings:
        print(
            f"Secret history scan passed (Gitleaks {GITLEAKS_VERSION}): "
            "no unreviewed findings in HEAD ancestry."
        )
        return 0

    print(
        f"Secret history scan failed: {len(findings)} unreviewed finding(s).",
        file=sys.stderr,
    )
    for finding in findings:
        print(
            f"- {finding.rule_id} at {finding.path}:{finding.line} "
            f"({finding.fingerprint})",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
