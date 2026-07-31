from __future__ import annotations

import hashlib
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import textwrap
from types import SimpleNamespace

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "check_secret_history.py"
WORKFLOW = ROOT / ".github" / "workflows" / "secret-scan.yml"
IGNORE = ROOT / ".gitleaksignore"
SPEC = importlib.util.spec_from_file_location("athena_check_secret_history", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
check_secret_history = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_secret_history
SPEC.loader.exec_module(check_secret_history)


def _release_archive(path: Path, *, binary: bytes = b"trusted binary") -> Path:
    with tarfile.open(path, mode="w:gz") as bundle:
        for name, content, mode in (
            ("LICENSE", b"license", 0o644),
            ("README.md", b"readme", 0o644),
            ("gitleaks", binary, 0o755),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            bundle.addfile(member, BytesIO(content))
    return path


def _finding(**overrides: object) -> dict[str, object]:
    finding: dict[str, object] = {
        "RuleID": "generic-api-key",
        "File": "tests/example.py",
        "StartLine": 7,
        "Commit": "a" * 40,
        "Fingerprint": f"{'a' * 40}:tests/example.py:generic-api-key:7",
        "Sec" + "ret": "must-never-be-printed",
        "Match": "also-private",
    }
    finding.update(overrides)
    return finding


def _write_report(path: Path, findings: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(findings), encoding="utf-8")
    return path


def _git(
    repository: Path, *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_release_asset_is_versioned_and_checksum_pinned() -> None:
    assert check_secret_history.GITLEAKS_VERSION == "8.30.1"
    assert check_secret_history.GITLEAKS_ARCHIVE_URL == (
        "https://github.com/gitleaks/gitleaks/releases/download/"
        "v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
    )
    assert check_secret_history.GITLEAKS_ARCHIVE_SHA256 == (
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
    )


def test_workflow_runs_with_complete_history_and_read_only_authority() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)

    assert document["permissions"] == {"contents": "read"}
    assert document["jobs"]["scan"]["timeout-minutes"] == 10
    assert "fetch-depth: 0" in workflow
    assert "persist-credentials: false" in workflow
    assert "python -I .github/scripts/check_secret_history.py" in workflow
    assert "pull_request_target" not in workflow
    assert "secrets." not in workflow
    assert "contents: write" not in workflow
    assert "id-token: write" not in workflow
    assert "upload-artifact" not in workflow
    assert (
        workflow.count("actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803") == 1
    )
    assert (
        workflow.count("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1")
        == 1
    )


def test_workflow_covers_push_pr_schedule_and_manual_runs() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "  push:\n    branches: [main]\n" in workflow
    assert "  pull_request:\n" in workflow
    assert "  schedule:\n" in workflow
    assert "  workflow_dispatch:\n" in workflow
    assert 'GIT_LOG_OPTIONS = "--diff-merges=remerge HEAD"' in SCRIPT.read_text(
        encoding="utf-8"
    )


def test_allowlist_contains_only_the_reviewed_historical_false_positive() -> None:
    entries = [
        line
        for line in IGNORE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    fingerprint = (
        "a3fb55127f2a73c3f45f4758576b4685e61024b7:"
        "tests/test_mcp_client.py:generic-api-key:634"
    )
    assert entries == [fingerprint]


class _Response:
    def __init__(self, content: bytes, *, content_length: str | None = None):
        self._content = BytesIO(content)
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def read(self, size: int) -> bytes:
        return self._content.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_download_requires_the_exact_digest(tmp_path: Path, monkeypatch) -> None:
    content = b"release archive"
    monkeypatch.setattr(
        check_secret_history,
        "GITLEAKS_ARCHIVE_SHA256",
        hashlib.sha256(content).hexdigest(),
    )
    monkeypatch.setattr(
        check_secret_history,
        "urlopen",
        lambda *_args, **_kwargs: _Response(content, content_length=str(len(content))),
    )
    archive = tmp_path / "asset.tar.gz"

    check_secret_history.download_archive(archive)

    assert archive.read_bytes() == content
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def test_download_removes_a_checksum_mismatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        check_secret_history,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"altered"),
    )
    archive = tmp_path / "asset.tar.gz"

    with pytest.raises(check_secret_history.ScanError, match="checksum"):
        check_secret_history.download_archive(archive)

    assert not archive.exists()


def test_download_rejects_a_declared_or_streamed_oversize(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(check_secret_history, "MAX_ARCHIVE_BYTES", 4)
    archive = tmp_path / "asset.tar.gz"
    monkeypatch.setattr(
        check_secret_history,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"small", content_length="5"),
    )
    with pytest.raises(check_secret_history.ScanError, match="download limit"):
        check_secret_history.download_archive(archive)
    assert not archive.exists()

    monkeypatch.setattr(
        check_secret_history,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"12345"),
    )
    with pytest.raises(check_secret_history.ScanError, match="download limit"):
        check_secret_history.download_archive(archive)
    assert not archive.exists()


def test_extracts_only_the_expected_binary_with_private_mode(tmp_path: Path) -> None:
    archive = _release_archive(tmp_path / "release.tar.gz")
    binary = tmp_path / "gitleaks"

    check_secret_history.extract_binary(archive, binary)

    assert binary.read_bytes() == b"trusted binary"
    assert stat.S_IMODE(binary.stat().st_mode) == 0o700


@pytest.mark.parametrize("unsafe_name", ["../gitleaks", "/gitleaks", "dir/gitleaks"])
def test_archive_rejects_traversal_and_nested_members(
    tmp_path: Path, unsafe_name: str
) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, mode="w:gz") as bundle:
        member = tarfile.TarInfo(unsafe_name)
        member.size = 1
        bundle.addfile(member, BytesIO(b"x"))

    with pytest.raises(check_secret_history.ScanError, match="unsafe member"):
        check_secret_history.extract_binary(archive, tmp_path / "gitleaks")


def test_archive_rejects_links_and_duplicate_members(tmp_path: Path) -> None:
    link_archive = tmp_path / "link.tar.gz"
    with tarfile.open(link_archive, mode="w:gz") as bundle:
        member = tarfile.TarInfo("gitleaks")
        member.type = tarfile.SYMTYPE
        member.linkname = "elsewhere"
        bundle.addfile(member)
    with pytest.raises(check_secret_history.ScanError, match="unsafe member"):
        check_secret_history.extract_binary(link_archive, tmp_path / "linked")

    duplicate_archive = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate_archive, mode="w:gz") as bundle:
        for content in (b"first", b"second"):
            member = tarfile.TarInfo("gitleaks")
            member.size = len(content)
            bundle.addfile(member, BytesIO(content))
    with pytest.raises(check_secret_history.ScanError, match="duplicate member"):
        check_secret_history.extract_binary(duplicate_archive, tmp_path / "duplicate")


def test_report_exposes_only_validated_metadata(tmp_path: Path) -> None:
    report = _write_report(tmp_path / "report.json", [_finding()])

    findings = check_secret_history.read_report(report)

    assert findings == (
        check_secret_history.Finding(
            rule_id="generic-api-key",
            path="tests/example.py",
            line=7,
            commit="a" * 40,
        ),
    )
    assert not hasattr(findings[0], "secret")
    assert not hasattr(findings[0], "match")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"File": "../secret"}, "file path"),
        ({"StartLine": True}, "line number"),
        ({"Commit": "not-a-commit"}, "commit identifier"),
        ({"Fingerprint": "inconsistent"}, "fingerprint"),
    ],
)
def test_report_rejects_unsafe_or_inconsistent_metadata(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    report = _write_report(tmp_path / "report.json", [_finding(**overrides)])

    with pytest.raises(check_secret_history.ScanError, match=message):
        check_secret_history.read_report(report)


def test_report_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text('[{"RuleID":"one","RuleID":"two"}]', encoding="utf-8")

    with pytest.raises(check_secret_history.ScanError, match="duplicate JSON key"):
        check_secret_history.read_report(report)


def test_scan_uses_redaction_full_head_ancestry_and_no_output_streams(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    ignore = repository / ".gitleaksignore"
    ignore.write_text("# none\n", encoding="utf-8")
    report = tmp_path / "report.json"
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object):
        captured["command"] = command
        captured["kwargs"] = kwargs
        _write_report(report, [])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check_secret_history.subprocess, "run", fake_run)

    assert (
        check_secret_history.scan_repository(tmp_path / "binary", repository, report)
        == ()
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert "--log-opts=--diff-merges=remerge HEAD" in command
    assert "--redact=100" in command
    assert "--exit-code=1" in command
    assert "--report-format=json" in command
    assert f"--gitleaks-ignore-path={ignore}" in command
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert not any(key.startswith("GITLEAKS_") for key in environment)


def test_remerge_traversal_detects_a_merge_resolution_only_finding(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.name", "Athena Test")
    _git(repository, "config", "user.email", "athena@example.invalid")
    (repository / ".gitleaksignore").write_text("# none\n", encoding="utf-8")
    conflict = repository / "conflict.txt"
    conflict.write_text("base value\n", encoding="utf-8")
    _git(repository, "add", ".gitleaksignore", "conflict.txt")
    _git(repository, "commit", "-m", "base")

    _git(repository, "switch", "-c", "topic")
    conflict.write_text("topic value\n", encoding="utf-8")
    _git(repository, "commit", "-am", "topic side")
    _git(repository, "switch", "main")
    conflict.write_text("main value\n", encoding="utf-8")
    _git(repository, "commit", "-am", "main side")
    merge = _git(repository, "merge", "--no-ff", "--no-edit", "topic", check=False)
    assert merge.returncode == 1

    credential = "".join(
        (
            "m9Q4vT2zK7pN1cX8",
            "rF5wY3hJ6sL0dBqE",
        )
    )
    credential_line = f'{"api_" + "key"} = "{credential}"\n'
    conflict.write_text(credential_line, encoding="utf-8")
    _git(repository, "add", "conflict.txt")
    _git(repository, "commit", "--no-edit")
    head = _git(repository, "rev-parse", "HEAD").stdout.strip()

    ordinary_patch = _git(repository, "log", "-p", "HEAD").stdout
    remerge_patch = _git(
        repository, "log", "-p", "--diff-merges=remerge", "HEAD"
    ).stdout
    assert credential_line.strip() not in ordinary_patch
    assert credential_line.strip() in remerge_patch

    fake_scanner = tmp_path / "gitleaks"
    fake_scanner.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            from pathlib import Path
            import shlex
            import subprocess
            import sys

            log_options = next(
                item.split("=", 1)[1]
                for item in sys.argv
                if item.startswith("--log-opts=")
            )
            report_path = Path(next(
                item.split("=", 1)[1]
                for item in sys.argv
                if item.startswith("--report-path=")
            ))
            repository = Path(sys.argv[-1])
            patch = subprocess.run(
                ["git", "-C", str(repository), "log", "-p", *shlex.split(log_options)],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout
            commit = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "HEAD"],
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            ).stdout.strip()
            rule = "generic-api-key"
            path = "conflict.txt"
            line = 1
            found = ("api_" + "key") in patch
            findings = [{
                "RuleID": rule,
                "File": path,
                "StartLine": line,
                "Commit": commit,
                "Fingerprint": f"{commit}:{path}:{rule}:{line}",
            }] if found else []
            report_path.write_text(json.dumps(findings), encoding="utf-8")
            raise SystemExit(1 if found else 0)
            """
        ),
        encoding="utf-8",
    )
    fake_scanner.chmod(0o700)
    assert check_secret_history.validate_repository(repository) == repository.resolve()

    findings = check_secret_history.scan_repository(
        fake_scanner, repository.resolve(), tmp_path / "report.json"
    )

    assert len(findings) == 1
    assert findings[0].commit == head
    assert findings[0].path == "conflict.txt"


def test_scan_fails_closed_on_tool_or_report_disagreement(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / ".gitleaksignore").write_text("# none\n", encoding="utf-8")
    report = tmp_path / "report.json"

    def fake_run(_command: list[str], **_kwargs: object):
        _write_report(report, [])
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(check_secret_history.subprocess, "run", fake_run)
    with pytest.raises(check_secret_history.ScanError, match="exited unexpectedly"):
        check_secret_history.scan_repository(tmp_path / "binary", repository, report)

    def conflicting_run(_command: list[str], **_kwargs: object):
        _write_report(report, [_finding()])
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(check_secret_history.subprocess, "run", conflicting_run)
    with pytest.raises(check_secret_history.ScanError, match="conflicts"):
        check_secret_history.scan_repository(tmp_path / "binary", repository, report)


def test_main_never_prints_sensitive_report_fields(capsys, monkeypatch) -> None:
    finding = check_secret_history.Finding(
        rule_id="generic-api-key",
        path="tests/example.py",
        line=7,
        commit="a" * 40,
    )
    monkeypatch.setattr(check_secret_history, "run", lambda _repository: (finding,))

    assert check_secret_history.main([]) == 1

    output = capsys.readouterr()
    assert "generic-api-key" in output.err
    assert finding.fingerprint in output.err
    assert "must-never-be-printed" not in output.err
    assert "also-private" not in output.err


def test_repository_validation_requires_complete_history_and_fixed_policy(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".gitleaksignore").write_text("# none\n", encoding="utf-8")
    responses = iter(("true", "true"))
    monkeypatch.setattr(
        check_secret_history, "_git_output", lambda *_args: next(responses)
    )
    with pytest.raises(check_secret_history.ScanError, match="shallow"):
        check_secret_history.validate_repository(tmp_path)

    (tmp_path / ".gitleaks.toml").write_text("[allowlist]\n", encoding="utf-8")
    responses = iter(("true", "false", "a" * 40, "a" * 40, ""))
    monkeypatch.setattr(
        check_secret_history, "_git_output", lambda *_args: next(responses)
    )
    with pytest.raises(check_secret_history.ScanError, match="not permitted"):
        check_secret_history.validate_repository(tmp_path)


def test_repository_validation_requires_remerge_support_and_rejects_octopus_history(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / ".gitleaksignore").write_text("# none\n", encoding="utf-8")
    head = "a" * 40

    def unsupported(_root: Path, *arguments: str) -> str:
        if arguments[0] == "log":
            raise check_secret_history.ScanError("unsupported")
        return {
            ("rev-parse", "--is-inside-work-tree"): "true",
            ("rev-parse", "--is-shallow-repository"): "false",
            ("rev-parse", "--verify", "HEAD"): head,
        }[arguments]

    monkeypatch.setattr(check_secret_history, "_git_output", unsupported)
    with pytest.raises(check_secret_history.ScanError, match="Git 2.36"):
        check_secret_history.validate_repository(tmp_path)

    responses = iter(("true", "false", head, head, "b" * 40))
    monkeypatch.setattr(
        check_secret_history, "_git_output", lambda *_args: next(responses)
    )
    with pytest.raises(check_secret_history.ScanError, match="octopus"):
        check_secret_history.validate_repository(tmp_path)


def test_binary_version_must_match_the_pinned_release(monkeypatch) -> None:
    monkeypatch.setattr(
        check_secret_history.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"8.30.0\n"),
    )
    with pytest.raises(check_secret_history.ScanError, match="version"):
        check_secret_history.verify_binary(Path("gitleaks"))


def test_download_failure_message_does_not_echo_exception_details(
    tmp_path: Path, monkeypatch
) -> None:
    def fail(*_args: object, **_kwargs: object):
        raise RuntimeError("credential-shaped private diagnostic")

    monkeypatch.setattr(check_secret_history, "urlopen", fail)

    with pytest.raises(check_secret_history.ScanError) as error:
        check_secret_history.download_archive(tmp_path / "archive")

    assert "RuntimeError" in str(error.value)
    assert "private diagnostic" not in str(error.value)


def test_stream_failure_is_wrapped_without_private_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    class FailingResponse(_Response):
        def read(self, _size: int) -> bytes:
            raise OSError("private transport diagnostic")

    monkeypatch.setattr(
        check_secret_history,
        "urlopen",
        lambda *_args, **_kwargs: FailingResponse(b""),
    )
    archive = tmp_path / "archive"

    with pytest.raises(check_secret_history.ScanError) as error:
        check_secret_history.download_archive(archive)

    assert "OSError" in str(error.value)
    assert "private transport diagnostic" not in str(error.value)
    assert not archive.exists()


def test_main_fails_closed_without_an_unhandled_traceback(capsys, monkeypatch) -> None:
    def fail(_repository: Path):
        raise RuntimeError("private internal diagnostic")

    monkeypatch.setattr(check_secret_history, "run", fail)

    assert check_secret_history.main([]) == 2
    output = capsys.readouterr()
    assert "unexpected internal error" in output.err
    assert "private internal diagnostic" not in output.err
