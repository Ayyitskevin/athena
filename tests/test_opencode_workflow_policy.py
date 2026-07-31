"""The comment-triggered OpenCode lane must keep its execution closure pinned."""

import ast
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "install_verified_opencode.py"
REDACTOR = ROOT / ".github" / "scripts" / "redact_opencode_output.py"
WORKFLOW = ROOT / ".github" / "workflows" / "opencode.yml"
INSTALL_STEP = "Install verified OpenCode 1.18.10 and ripgrep 15.1.0"
SPEC = importlib.util.spec_from_file_location("athena_install_opencode", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
install_opencode = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = install_opencode
SPEC.loader.exec_module(install_opencode)
REDACTOR_SPEC = importlib.util.spec_from_file_location(
    "athena_redact_opencode_output", REDACTOR
)
assert REDACTOR_SPEC is not None and REDACTOR_SPEC.loader is not None
redact_opencode_output = importlib.util.module_from_spec(REDACTOR_SPEC)
sys.modules[REDACTOR_SPEC.name] = redact_opencode_output
REDACTOR_SPEC.loader.exec_module(redact_opencode_output)


BINARY = b"#!/bin/sh\nprintf 'synthetic opencode\\n'\n"
RG_BINARY = b"#!/bin/sh\nprintf 'ripgrep 15.1.0\\n'\n"


def _step_block(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert workflow.count(marker) == 1
    start = workflow.index(marker)
    end = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if end == -1 else workflow[start:end]


def _active_lines(block: str) -> list[str]:
    return [
        line.strip()
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Athena policy test",
            "-c",
            "user.email=athena-policy@example.invalid",
            *arguments,
        ],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _regular(name: str, content: bytes) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = 0o755
    return member, content


def _symlink(name: str) -> tuple[tarfile.TarInfo, bytes]:
    member = tarfile.TarInfo(name)
    member.type = tarfile.SYMTYPE
    member.linkname = "/tmp/not-opencode"
    return member, b""


def _archive(
    path: Path, members: list[tuple[tarfile.TarInfo, bytes]] | None = None
) -> Path:
    with tarfile.open(path, mode="w:gz") as release:
        for member, content in members or [_regular("opencode", BINARY)]:
            release.addfile(member, io.BytesIO(content) if member.isreg() else None)
    return path


def _install(
    archive: Path,
    install_dir: Path,
    *,
    archive_bytes: int | None = None,
    archive_sha256: str | None = None,
    binary_bytes: int = len(BINARY),
    binary_sha256: str = hashlib.sha256(BINARY).hexdigest(),
    archive_member: str = "opencode",
    binary_name: str = "opencode",
    expected_member_count: int = 1,
    label: str = "OpenCode",
) -> Path:
    content = archive.read_bytes()
    return install_opencode.install_verified_archive(
        archive,
        install_dir,
        expected_archive_bytes=len(content) if archive_bytes is None else archive_bytes,
        expected_archive_sha256=(
            hashlib.sha256(content).hexdigest()
            if archive_sha256 is None
            else archive_sha256
        ),
        expected_binary_bytes=binary_bytes,
        expected_binary_sha256=binary_sha256,
        archive_member=archive_member,
        binary_name=binary_name,
        expected_member_count=expected_member_count,
        label=label,
    )


def test_verified_archive_installs_exact_private_executable(tmp_path):
    installed = _install(_archive(tmp_path / "release.tar.gz"), tmp_path / "install")

    assert installed.read_bytes() == BINARY
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700
    assert stat.S_IMODE(installed.parent.stat().st_mode) == 0o700


def test_output_redactor_masks_dynamic_github_credentials():
    app_token = b"ghs_abcdefghijklmnopqrstuvwxyz012345"
    fine_grained_token = b"github_pat_abcdefghijklmnopqrstuvwxyz012345"
    encoded_extraheader = b"eC1hY2Nlc3MtdG9rZW46Z2hzX3NlY3JldA=="
    source = io.BytesIO(
        b"Command failed: AUTHORIZATION: basic "
        + encoded_extraheader
        + b"\nAuthorization: Bearer "
        + app_token
        + b"\nraw="
        + fine_grained_token
        + b"\nordinary=YWJjZA==\n"
    )
    destination = io.BytesIO()

    redact_opencode_output.redact_stream(source, destination)

    output = destination.getvalue()
    assert encoded_extraheader not in output
    assert app_token not in output
    assert fine_grained_token not in output
    assert output.count(redact_opencode_output.REDACTED) == 3
    assert b"ordinary=YWJjZA==" in output


def test_verified_archive_selects_one_exact_nested_member_without_extracting_others(
    tmp_path,
):
    member_name = "ripgrep-15.1.0-x86_64-unknown-linux-musl/rg"
    archive = _archive(
        tmp_path / "ripgrep.tar.gz",
        [
            _regular("ripgrep-15.1.0/README.md", b"documentation"),
            _regular(member_name, RG_BINARY),
            _regular("ripgrep-15.1.0/LICENSE", b"license"),
        ],
    )

    installed = _install(
        archive,
        tmp_path / "install",
        binary_bytes=len(RG_BINARY),
        binary_sha256=hashlib.sha256(RG_BINARY).hexdigest(),
        archive_member=member_name,
        binary_name="rg",
        expected_member_count=3,
        label="ripgrep",
    )

    assert installed.name == "rg"
    assert installed.read_bytes() == RG_BINARY
    assert list(installed.parent.iterdir()) == [installed]


@pytest.mark.parametrize("binary_name", ["../rg", "bin/rg", ".", "", "rg name"])
def test_destination_binary_name_must_be_one_safe_filename(tmp_path, binary_name):
    archive = _archive(tmp_path / "release.tar.gz")
    install_dir = tmp_path / "install"

    with pytest.raises(ValueError, match="single safe filename"):
        _install(archive, install_dir, binary_name=binary_name)

    assert not install_dir.exists()


@pytest.mark.parametrize("expected_member_count", [2, 4])
def test_archive_member_count_must_match_for_nested_release(
    tmp_path, expected_member_count
):
    member_name = "release/rg"
    archive = _archive(
        tmp_path / "ripgrep.tar.gz",
        [
            _regular("release/README", b"readme"),
            _regular(member_name, RG_BINARY),
            _regular("release/LICENSE", b"license"),
        ],
    )
    install_dir = tmp_path / "install"

    with pytest.raises(ValueError, match="members"):
        _install(
            archive,
            install_dir,
            binary_bytes=len(RG_BINARY),
            binary_sha256=hashlib.sha256(RG_BINARY).hexdigest(),
            archive_member=member_name,
            binary_name="rg",
            expected_member_count=expected_member_count,
            label="ripgrep",
        )

    assert not install_dir.exists()


def test_archive_path_must_not_be_a_symlink(tmp_path):
    archive = _archive(tmp_path / "release.tar.gz")
    linked = tmp_path / "linked.tar.gz"
    linked.symlink_to(archive)
    install_dir = tmp_path / "install"

    with pytest.raises(OSError):
        _install(linked, install_dir)

    assert not install_dir.exists()


@pytest.mark.parametrize("defect", ["size", "digest"])
def test_archive_byte_identity_must_match_before_extraction(tmp_path, defect):
    archive = _archive(tmp_path / "release.tar.gz")
    install_dir = tmp_path / "install"
    kwargs = (
        {"archive_bytes": archive.stat().st_size + 1}
        if defect == "size"
        else {"archive_sha256": "0" * 64}
    )

    with pytest.raises(ValueError, match="archive (byte count|SHA-256)"):
        _install(archive, install_dir, **kwargs)

    assert not install_dir.exists()


@pytest.mark.parametrize(
    "members",
    [
        [_regular("opencode", BINARY), _regular("surprise", b"extra")],
        [_regular("../opencode", BINARY)],
        [_regular("bin/opencode", BINARY)],
        [_symlink("opencode")],
    ],
    ids=["extra-member", "traversal", "nested-name", "symlink"],
)
def test_archive_shape_must_be_one_regular_root_member(tmp_path, members):
    archive = _archive(tmp_path / "release.tar.gz", members)
    install_dir = tmp_path / "install"

    with pytest.raises(
        ValueError, match="exactly one|expected member 'opencode'|regular file"
    ):
        _install(archive, install_dir)

    assert not install_dir.exists()


def test_archive_member_scan_stops_after_detecting_a_second_header():
    first, _ = _regular("opencode", BINARY)
    second, _ = _regular("surprise", b"extra")

    class Release:
        def __init__(self):
            self.calls = 0

        def next(self):
            self.calls += 1
            if self.calls == 1:
                return first
            if self.calls == 2:
                return second
            raise AssertionError("verifier scanned beyond the second member")

    release = Release()
    with pytest.raises(ValueError, match="multiple members"):
        install_opencode._verified_release_source(
            release, expected_binary_bytes=len(BINARY)
        )

    assert release.calls == 2


@pytest.mark.parametrize("defect", ["size", "digest"])
def test_binary_identity_drift_removes_partial_install(tmp_path, defect):
    archive = _archive(tmp_path / "release.tar.gz")
    install_dir = tmp_path / "install"
    kwargs = (
        {"binary_bytes": len(BINARY) + 1}
        if defect == "size"
        else {"binary_sha256": "0" * 64}
    )

    with pytest.raises(ValueError, match="binary (byte count|SHA-256)"):
        _install(archive, install_dir, **kwargs)

    assert not install_dir.exists()


def test_preexisting_install_directory_is_never_reused_or_removed(tmp_path):
    archive = _archive(tmp_path / "release.tar.gz")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    sentinel = install_dir / "owned-by-caller"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        _install(archive, install_dir)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_workflow_preserves_owner_gate_and_minimal_runtime_authority():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    authorizer = _step_block(workflow, "Authorize exact OpenCode command")

    assert "github.actor == github.repository_owner" in workflow
    assert "github.triggering_actor == github.repository_owner" in workflow
    assert "author_association" not in workflow
    assert "issue_comment:" in workflow
    assert "pull_request_review_comment:" in workflow
    assert "timeout-minutes: 20" in workflow
    assert "runs-on: ubuntu-24.04" in workflow
    assert "ubuntu-latest" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "\nconcurrency:\n" not in workflow
    assert "\n    concurrency:\n" in workflow
    assert (
        "group: opencode-${{ github.repository }}-${{ github.event.issue.number || "
        "github.event.pull_request.number || github.run_id }}"
    ) in workflow
    assert "id-token: write" in workflow
    assert "contents: read" in workflow
    assert "pull-requests:" not in workflow
    assert "issues:" not in workflow
    assert "id: command" in _active_lines(authorizer)
    assert workflow.index("- name: Authorize exact OpenCode command") < workflow.index(
        "- name: Checkout repository"
    )
    for step_name in (
        "Checkout repository",
        "Quarantine untrusted OpenCode project configuration",
        INSTALL_STEP,
        "Run verified OpenCode 1.18.10",
    ):
        assert "if: steps.command.outputs.run == 'true'" in _active_lines(
            _step_block(workflow, step_name)
        )


def test_workflow_quarantines_project_config_across_later_branch_checkouts():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    quarantine_lines = set(
        _active_lines(
            _step_block(workflow, "Quarantine untrusted OpenCode project configuration")
        )
    )

    assert {
        "git sparse-checkout init --no-cone",
        "git sparse-checkout set --no-cone \\",
        "'/*' \\",
        "'!**/.opencode' \\",
        "'!**/.opencode/' \\",
        "'!**/opencode.json' \\",
        "'!**/opencode.jsonc'",
        "if [[ -e /etc/opencode || -L /etc/opencode ]]; then",
        'echo "managed /etc/opencode configuration is not allowed on this runner" >&2',
        "exit 1",
        "fi",
    } <= quarantine_lines
    assert (
        workflow.index("- name: Checkout repository")
        < workflow.index("- name: Quarantine untrusted OpenCode project configuration")
        < workflow.index(f"- name: {INSTALL_STEP}")
    )


def test_sparse_quarantine_survives_future_symlink_branch_checkout(tmp_path):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    quarantine_lines = _active_lines(
        _step_block(workflow, "Quarantine untrusted OpenCode project configuration")
    )
    patterns = [
        ast.literal_eval(line.removesuffix("\\").strip())
        for line in quarantine_lines
        if line.startswith(("'/*'", "'!"))
    ]
    repository = tmp_path / "repository"
    repository.mkdir()

    _git(repository, "init", "--initial-branch=main")
    (repository / "trusted.txt").write_text("trusted\n", encoding="utf-8")
    _git(repository, "add", "trusted.txt")
    _git(repository, "commit", "--message=trusted")

    _git(repository, "switch", "--create=future-attacker")
    for directory in (repository / "payload", repository / "nested" / "payload"):
        (directory / "plugin").mkdir(parents=True)
        (directory / "opencode.json").write_text(
            '{"plugins":["./plugin/pwn.ts"]}\n', encoding="utf-8"
        )
        (directory / "plugin" / "pwn.ts").write_text(
            'throw new Error("must not load")\n', encoding="utf-8"
        )
    (repository / ".opencode").symlink_to("payload", target_is_directory=True)
    (repository / "nested" / ".opencode").symlink_to(
        "payload", target_is_directory=True
    )
    for config in (
        repository / "opencode.json",
        repository / "opencode.jsonc",
        repository / "nested" / "opencode.json",
        repository / "nested" / "opencode.jsonc",
    ):
        config.write_text('{"plugins":[]}\n', encoding="utf-8")
    (repository / "ordinary.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "--message=future-attacker")

    _git(repository, "switch", "main")
    _git(repository, "sparse-checkout", "init", "--no-cone")
    _git(repository, "sparse-checkout", "set", "--no-cone", *patterns)
    _git(repository, "switch", "future-attacker")

    statuses = {
        line[2:]: line[0] for line in _git(repository, "ls-files", "-t").splitlines()
    }
    denied = {
        ".opencode",
        "nested/.opencode",
        "opencode.json",
        "opencode.jsonc",
        "nested/opencode.json",
        "nested/opencode.jsonc",
        "payload/opencode.json",
        "nested/payload/opencode.json",
    }
    assert patterns == [
        "/*",
        "!**/.opencode",
        "!**/.opencode/",
        "!**/opencode.json",
        "!**/opencode.jsonc",
    ]
    assert {path: statuses[path] for path in denied} == dict.fromkeys(denied, "S")
    assert all(not os.path.lexists(repository / path) for path in denied)
    assert (repository / "ordinary.py").is_file()
    assert (repository / "payload" / "plugin" / "pwn.ts").is_file()


@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        ("/oc", True),
        ("/opencode", True),
        ("please /oc review this", True),
        ("first line\n/opencode", True),
        ("\t/oc\n", True),
        ("/occult", False),
        ("/opencodeWhatever", False),
        ("prefix/oc", False),
        ("please", False),
    ],
)
def test_workflow_authorizes_only_exact_command_boundaries(comment, expected):
    workflow = WORKFLOW.read_text(encoding="utf-8")
    lines = _active_lines(_step_block(workflow, "Authorize exact OpenCode command"))
    assignment = next(line for line in lines if line.startswith("pattern = "))
    pattern = re.compile(ast.literal_eval(assignment.partition("=")[2].strip()))

    assert (pattern.search(comment) is not None) is expected


def test_workflow_verifies_exact_release_bytes_before_secret_exposure():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    install_block = _step_block(workflow, INSTALL_STEP)
    install_lines = _active_lines(install_block)
    install_line_set = set(install_lines)
    run_lines = _active_lines(_step_block(workflow, "Run verified OpenCode 1.18.10"))

    expected_install_lines = {
        "--fail \\",
        "--location \\",
        "--retry-max-time 180 \\",
        "--connect-timeout 10 \\",
        "--max-time 120 \\",
        "--max-filesize 59327159 \\",
        "--proto '=https' \\",
        "--proto-redir '=https' \\",
        "--tlsv1.2 \\",
        "https://github.com/anomalyco/opencode/releases/download/"
        "v1.18.10/opencode-linux-x64.tar.gz",
        "--expected-archive-bytes 59327159 \\",
        "--expected-archive-sha256 "
        "6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976 \\",
        "--expected-binary-bytes 179206272 \\",
        "--expected-binary-sha256 "
        "2735f786be499db50c823d961fb8627dfb74f920e2320686b67e6c5c81c66f16",
        "--max-filesize 2263077 \\",
        "https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/"
        "ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz",
        "--expected-archive-bytes 2263077 \\",
        "--expected-archive-sha256 "
        "1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599 \\",
        "--expected-binary-bytes 5445512 \\",
        "--expected-binary-sha256 "
        "ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2 \\",
        "--archive-member ripgrep-15.1.0-x86_64-unknown-linux-musl/rg \\",
        "--binary-name rg \\",
        "--expected-member-count 16 \\",
        "--label ripgrep",
    }
    assert expected_install_lines <= install_line_set
    assert install_block.count("curl \\\n            --disable \\") == 2
    for repeated_curl_control in (
        "--disable \\",
        "--fail \\",
        "--location \\",
        "--retry-max-time 180 \\",
        "--connect-timeout 10 \\",
        "--max-time 120 \\",
        "--proto '=https' \\",
        "--proto-redir '=https' \\",
        "--tlsv1.2 \\",
    ):
        assert install_lines.count(repeated_curl_control) == 2
    assert not any("OPENCODE_API_KEY" in line for line in install_lines)
    assert (
        sum(
            line == "OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}"
            for line in _active_lines(workflow)
        )
        == 1
    )
    assert "OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}" in run_lines


def test_workflow_isolates_mutable_opencode_runtime_inputs():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    run_block = _step_block(workflow, "Run verified OpenCode 1.18.10")
    run_lines = set(_active_lines(run_block))

    expected_environment = {
        "HOME: ${{ runner.temp }}/athena-opencode-home",
        "XDG_CONFIG_HOME: ${{ runner.temp }}/athena-opencode-home/config",
        "XDG_DATA_HOME: ${{ runner.temp }}/athena-opencode-home/data",
        "XDG_STATE_HOME: ${{ runner.temp }}/athena-opencode-home/state",
        "XDG_CACHE_HOME: ${{ runner.temp }}/athena-opencode-home/cache",
        "TMPDIR: ${{ runner.temp }}/athena-opencode-tmp",
        'OTEL_EXPORTER_OTLP_ENDPOINT: ""',
        'OTEL_EXPORTER_OTLP_HEADERS: ""',
        'OTEL_RESOURCE_ATTRIBUTES: ""',
        "OIDC_BASE_URL: https://api.opencode.ai",
        'OPENCODE_AUTH_CONTENT: "{}"',
        "OPENCODE_CONFIG: ${{ runner.temp }}/athena-opencode-home/config/"
        "opencode/opencode.json",
        "OPENCODE_CONFIG_DIR: ${{ runner.temp }}/athena-opencode-home/config/opencode",
        'OPENCODE_CONFIG_CONTENT: \'{"formatter": false, "lsp": false}\'',
        "OPENCODE_DB: ${{ runner.temp }}/athena-opencode-home/data/opencode/"
        "opencode.db",
        "OPENCODE_MODELS_PATH: ${{ runner.temp }}/athena-opencode-home/config/"
        "opencode/embedded-models-only.json",
        'OPENCODE_PURE: "1"',
        'OPENCODE_DISABLE_PROJECT_CONFIG: "1"',
        'OPENCODE_DISABLE_DEFAULT_PLUGINS: "1"',
        'OPENCODE_DISABLE_MODELS_FETCH: "1"',
        'OPENCODE_DISABLE_AUTOUPDATE: "1"',
        'OPENCODE_DISABLE_SHARE: "1"',
        'OPENCODE_DISABLE_LSP_DOWNLOAD: "1"',
        'OPENCODE_DISABLE_EXTERNAL_SKILLS: "1"',
        'OPENCODE_DISABLE_CLAUDE_CODE: "1"',
        'GIT_LFS_SKIP_SMUDGE: "1"',
    }
    assert expected_environment <= run_lines
    assert "unset OPENCODE_TEST_HOME OPENCODE_TEST_MANAGED_CONFIG_DIR" in run_lines
    assert (
        'install -m 0400 "$GITHUB_WORKSPACE/AGENTS.md" "$config_dir/AGENTS.md"'
    ) in run_lines
    assert (
        '"$GITHUB_WORKSPACE/.github/scripts/redact_opencode_output.py" \\' in run_lines
    )
    assert '"$config_dir/redact_opencode_output.py"' in run_lines
    assert 'runtime_cache_dir="$XDG_CACHE_HOME/opencode"' in run_lines
    assert '"$TMPDIR" \\' in run_lines
    assert '"$runtime_cache_dir/bin" \\' in run_lines
    assert '"$runtime_cache_dir/packages"' in run_lines
    assert "printf '*\\n' > \"$config_dir/.gitignore\"" in run_lines
    assert (
        'printf \'{"formatter":false,"lsp":false}\\n\' > "$config_dir/opencode.json"'
        in run_lines
    )
    assert '"$config_dir/.gitignore" \\' in run_lines
    assert '"$config_dir/AGENTS.md" \\' in run_lines
    assert '"$config_dir/opencode.json" \\' in run_lines
    assert 'chmod 0500 "$config_dir"' in run_lines
    assert (
        "chmod 0500 \\\n"
        '            "$runtime_cache_dir/bin" \\\n'
        '            "$runtime_cache_dir/packages" \\\n'
        '            "$runtime_cache_dir"'
    ) in run_block


def test_workflow_runs_verified_binary_without_mutable_execution_helpers():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    checkout_lines = set(_active_lines(_step_block(workflow, "Checkout repository")))
    run_lines = set(
        _active_lines(_step_block(workflow, "Run verified OpenCode 1.18.10"))
    )

    assert 'observed_bytes="$(stat --format=\'%s\' -- "$binary")"' in run_lines
    assert 'if [[ "$observed_bytes" != "179206272" ]]; then' in run_lines
    assert 'observed_sha256="$(sha256sum --binary -- "$binary")"' in run_lines
    assert (
        'if [[ "$observed_sha256" != '
        '"2735f786be499db50c823d961fb8627dfb74f920e2320686b67e6c5c81c66f16" ]]; then'
    ) in run_lines
    assert 'version="$("$binary" --version)"' in run_lines
    assert 'if [[ "$version" != "1.18.10" ]]; then' in run_lines
    assert 'rg_binary="$RUNNER_TEMP/athena-ripgrep-15.1.0/rg"' in run_lines
    assert 'observed_rg_bytes="$(stat --format=\'%s\' -- "$rg_binary")"' in run_lines
    assert 'if [[ "$observed_rg_bytes" != "5445512" ]]; then' in run_lines
    assert 'observed_rg_sha256="$(sha256sum --binary -- "$rg_binary")"' in run_lines
    assert (
        'if [[ "$observed_rg_sha256" != '
        '"ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2" ]]; then'
    ) in run_lines
    assert 'rg_version="$("$rg_binary" --version)"' in run_lines
    assert 'if [[ "${rg_version%%$\'\\n\'*}" != "ripgrep 15.1.0" ]]; then' in run_lines
    assert 'export PATH="$RUNNER_TEMP/athena-ripgrep-15.1.0:$PATH"' in run_lines
    assert 'if [[ "$(command -v rg)" != "$rg_binary" ]]; then' in run_lines
    assert '"$binary" github run 2>&1 |' in run_lines
    assert 'python3 -I -u "$config_dir/redact_opencode_output.py"' in run_lines
    assert 'exec "$binary" github run' not in run_lines
    assert "MODEL: opencode/claude-sonnet-4-6" in run_lines
    assert 'SHARE: "false"' in run_lines
    assert 'USE_GITHUB_TOKEN: "false"' in run_lines
    for forbidden in (
        "anomalyco/opencode/github@",
        "actions/cache@",
        "opencode.ai/install",
        "@latest",
        "| bash",
        "| sh",
    ):
        assert forbidden not in workflow
    assert (
        "uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6"
        in checkout_lines
    )
    assert "ref: ${{ github.workflow_sha }}" in checkout_lines
    assert "fetch-depth: 1" in checkout_lines
    assert "persist-credentials: false" in checkout_lines
