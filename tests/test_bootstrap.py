"""The committed bootstrap builds the environment CONTRIBUTING.md describes (B3).

WHY: a cloud checkout used to arrive shallow and with no ``.venv``, and every
session rebuilt the environment from memory. ``scripts/bootstrap.sh`` is the one
bootstrap every lane runs; ``.claude/hooks/session-start.sh`` runs it at the
start of a Claude Code cloud session. "Exactly as CONTRIBUTING.md does" is a
promise that drifts unless something holds the two together, so these tests do:
the script's install commands are the first commands of the documented block,
in order, and the committed ``.claude/`` files name real, executable paths and
allow-list only the gate and the bootstrap.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"
HOOK = ROOT / ".claude" / "hooks" / "session-start.sh"
SETTINGS = ROOT / ".claude" / "settings.json"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

# The commands that build and verify the environment: the first five of the
# "Development setup" block. The rest of that block is the gate's static stages.
SETUP_COMMANDS = 5


def _commands(text: str) -> list[str]:
    """Shell commands in ``text``: continuation lines joined, whitespace and
    pip's ``--quiet`` (output only) removed, comments and blanks dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    commands = []
    for line in joined.splitlines():
        line = re.sub(r"\s+", " ", line.strip()).replace(" --quiet", "")
        if line and not line.startswith("#"):
            commands.append(line)
    return commands


def _documented_setup() -> list[str]:
    text = CONTRIBUTING.read_text()
    section = text[text.index("## Development setup") :]
    block = re.search(r"```bash\n(.*?)```", section, re.S)
    assert block, "CONTRIBUTING.md no longer shows the setup commands"
    return _commands(block.group(1))[:SETUP_COMMANDS]


def test_the_bootstrap_installs_exactly_as_contributing_documents():
    documented = _documented_setup()
    assert documented[0] == "python3.12 -m venv .venv"
    assert documented[-1].endswith("| diff -u constraints/ci-py312.txt -")
    script = _commands(BOOTSTRAP.read_text())
    positions = [script.index(command) for command in documented]
    assert positions == sorted(positions), "the bootstrap runs the steps out of order"
    # Nothing else installs: every pip command in the script is a documented one.
    assert [c for c in script if " -m pip " in c] == documented[1:]


def test_the_bootstrap_hashes_every_file_that_decides_the_install():
    text = BOOTSTRAP.read_text()
    inputs = re.search(r"^inputs=\((.*?)\)$", text, re.M)
    assert inputs, "the bootstrap no longer lists its install inputs"
    listed = inputs.group(1).split()
    assert listed == [
        "scripts/bootstrap.sh",
        "pyproject.toml",
        "constraints/bootstrap-py312.txt",
        "constraints/ci-py312.txt",
    ]
    for path in listed:
        assert (ROOT / path).is_file(), path
    # The hash file lives inside the ignored .venv, so it goes when the packages go.
    assert "hashes=.venv/.bootstrap.sha256" in text


def test_the_bootstrap_fetches_full_history_and_the_default_base():
    text = BOOTSTRAP.read_text()
    assert "git fetch --quiet --unshallow origin" in text
    assert "origin main:refs/remotes/origin/main" in text


def test_both_scripts_are_executable_and_parse():
    for script in (BOOTSTRAP, HOOK):
        assert os.access(script, os.X_OK), f"{script.name} is not executable"
        checked = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert checked.returncode == 0, checked.stderr


def test_the_session_hook_runs_the_same_bootstrap_only_in_a_cloud_session(tmp_path):
    text = HOOK.read_text()
    assert "exec scripts/bootstrap.sh" in text
    # Outside a cloud session it does nothing at all, wherever it is run from.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_REMOTE"}
    env["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    ran = subprocess.run(
        [str(HOOK)], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")
    assert list(tmp_path.iterdir()) == []


def test_the_committed_settings_register_the_hook_and_allow_only_the_gate():
    settings = json.loads(SETTINGS.read_text())
    assert set(settings) == {"permissions", "hooks"}
    assert set(settings["permissions"]) == {"allow"}
    assert settings["permissions"]["allow"] == [
        "Bash(scripts/gate.sh:*)",
        "Bash(./scripts/gate.sh:*)",
        "Bash(scripts/bootstrap.sh)",
        "Bash(./scripts/bootstrap.sh)",
    ]
    assert (ROOT / "scripts" / "gate.sh").is_file()
    assert set(settings["hooks"]) == {"SessionStart"}
    commands = [
        hook["command"]
        for entry in settings["hooks"]["SessionStart"]
        for hook in entry["hooks"]
    ]
    assert commands == ["$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh"]


def test_local_settings_stay_out_of_git():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".claude/settings.local.json"],
        cwd=ROOT,
        check=False,
    )
    assert ignored.returncode == 0, ".claude/settings.local.json is not ignored"
