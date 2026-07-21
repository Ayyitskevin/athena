"""Static policy tests for the comment-triggered OpenCode workflow."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO_ROOT / ".github/workflows/opencode.yml").read_text()
CONTROL_DOC = (REPO_ROOT / "docs/OPENCODE_WORKFLOW.md").read_text()


def test_opencode_rejects_untrusted_commenters_before_runner_allocation():
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        assert f"author_association == '{association}'" in WORKFLOW

    for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
        assert f"author_association == '{association}'" not in WORKFLOW


def test_opencode_cli_archive_is_version_and_digest_pinned():
    assert "releases/download/v1.18.4/opencode-linux-x64.tar.gz" in WORKFLOW
    assert (
        "bab463c3fb3224d388bb7cfad63f38703df9cf0be2cfd2ce8cb49d886b53a174" in WORKFLOW
    )
    assert "sha256sum --check --strict" in WORKFLOW
    assert "--proto-redir '=https'" in WORKFLOW
    assert 'test -x "${install_dir}/opencode"' in WORKFLOW
    assert 'test "${installed_version}" = "1.18.4"' in WORKFLOW
    assert "run: opencode github run" in WORKFLOW

    assert re.search(
        r"^\s*uses: actions/checkout@[0-9a-f]{40}(?:\s+#.*)?$",
        WORKFLOW,
        re.MULTILINE,
    )


def test_opencode_workflow_forbids_moving_or_composite_install_paths():
    for forbidden in (
        "anomalyco/opencode/github@",
        "actions/cache@",
        "@latest",
        "opencode.ai/install",
        "curl | bash",
    ):
        assert forbidden not in WORKFLOW


def test_opencode_job_permissions_privacy_and_run_caps_stay_narrow():
    assert "id-token: write" in WORKFLOW
    assert "contents: read" in WORKFLOW
    assert 'USE_GITHUB_TOKEN: "false"' in WORKFLOW
    assert 'SHARE: "false"' in WORKFLOW
    assert "MODEL: opencode/claude-sonnet-4-6" in WORKFLOW
    assert "timeout-minutes: 30" in WORKFLOW
    assert "cancel-in-progress: true" in WORKFLOW

    for forbidden in ("contents: write", "issues:", "pull-requests:"):
        assert forbidden not in WORKFLOW


def test_opencode_operator_controls_record_cost_privacy_and_provenance_limits():
    assert "provider-enforced hard budget/rate limit" in CONTROL_DOC
    assert "keep this workflow disabled" in CONTROL_DOC
    assert 'SHARE: "false"' in CONTROL_DOC
    assert "49c69c5ed3ccf706b61b3febb43c8aaff7f8325e" in CONTROL_DOC
    assert "`unsigned`" in CONTROL_DOC
    assert "does **not** establish signed source provenance" in CONTROL_DOC
    assert "Forbidden regressions" in CONTROL_DOC
