"""The two command-migration debts can only go down (G1).

WHY: ``docs/COMMAND_MIGRATION.md`` generates two debt tables — commands that take
a bare actor id, and writes that sit outside a command — and holds them equal to
the code. That kept the tables honest but let them grow: a new command taking a
bare ``actor_id`` simply appeared as one more row. Each count now has a ceiling
in ``[tool.athena.command_debts]`` of ``pyproject.toml``, held from both sides
the way ``scripts/check_coverage.py`` holds the coverage floors:

* above its ceiling fails and names the new debtor, and the generator will not
  rewrite the table to absorb it, so the committed table stays the last list
  the ratchet accepted;
* below its ceiling fails with "lower the ceiling to N", so a change that paid
  debt lowers the ceiling in the same commit and the debt cannot grow back.

The gate runs it as the ``command-debts`` stage (``tests/test_gate_profiles.py``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "generate_command_debtors.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_command_debtors", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["generate_command_debtors"] = module
    spec.loader.exec_module(module)
    return module


g = _load_generator()


def test_the_real_counts_sit_exactly_at_their_ceilings():
    assert g.ratchet(g.debtors(), g.load_ceilings(), g.listed(g.DOC.read_text())) == []
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "command debts at their ceilings: bare_actor_id " in run.stdout


def test_the_committed_table_lists_exactly_the_debtors_the_code_states():
    # The table is the accepted list a new debtor is named against, so reading it
    # back must give the generator's own names, no more and no fewer.
    current = g.debtors()
    assert {block: set(names) for block, names in current.items()} == g.listed(
        g.DOC.read_text()
    )


# --- a synthetic repository the ratchet runs over -----------------------------

SKELETON = "# Debts\n\n" + "\n\n".join(
    f"{g.generated_blocks.begin_marker(block, g.SOURCE)}\n"
    f"{g.generated_blocks.end_marker(block)}"
    for block in (g.BARE_ACTOR, g.OUTSIDE_COMMAND)
)
WRITES = {("*", "athena.core.notes", "save_note"): "personal state"}


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """Two command modules holding two bare-actor debtors (and a paid command and
    a private helper, which are not debtors), one write outside a command,
    ceilings at 2 and 1, and the document generated."""
    package = tmp_path / "src" / "athena"
    package.mkdir(parents=True)
    (package / "lab_commands.py").write_text(
        "def old_debt(conn, *, actor_id): ...\n"
        "def paid(conn, *, actor): ...\n"
        "def _private(conn, *, actor_id): ...\n"
    )
    (package / "keep_commands.py").write_text("def kept(conn, *, actor_id): ...\n")
    writes = dict(WRITES)
    monkeypatch.setattr(g, "REPO", tmp_path)
    monkeypatch.setattr(g, "PACKAGE", package)
    monkeypatch.setattr(g, "DOC", tmp_path / "COMMAND_MIGRATION.md")
    monkeypatch.setattr(g, "CONFIG", tmp_path / "pyproject.toml")
    monkeypatch.setattr(g, "outside_command_writes", lambda: dict(writes))
    _ceilings(tmp_path, bare=2, outside=1)
    g.DOC.write_text(g.render(SKELETON))
    return {"package": package, "writes": writes, "root": tmp_path}


def _ceilings(root: Path, *, bare: int, outside: int) -> None:
    (root / "pyproject.toml").write_text(
        f"[tool.athena.command_debts]\nbare_actor_id = {bare}\n"
        f"outside_command = {outside}\n"
    )


def _add_debtor(repo) -> None:
    module = repo["package"] / "lab_commands.py"
    module.write_text(module.read_text() + "def new_debt(conn, *, actor_id): ...\n")


def _failures():
    return g.ratchet(g.debtors(), g.load_ceilings(), g.listed(g.DOC.read_text()))


def test_equal_counts_pass(repo, capsys):
    assert _failures() == []
    assert g.main(check=True) == 0
    assert "command debts at their ceilings: bare_actor_id 2, outside_command 1" in (
        capsys.readouterr().out
    )


def test_a_new_bare_actor_debtor_fails_naming_it(repo, capsys):
    _add_debtor(repo)
    [failure] = _failures()
    assert failure.grew and failure.block == g.BARE_ACTOR
    assert "3 commands take a bare actor id, above the ceiling of 2" in failure.message
    assert "new debtor: athena.lab_commands.new_debt." in failure.message
    assert "old_debt" not in failure.message  # the accepted one is not blamed
    assert "tool.athena.command_debts.bare_actor_id" in failure.message
    assert g.main(check=True) == 1
    assert "athena.lab_commands.new_debt" in capsys.readouterr().err


def test_a_new_write_outside_a_command_fails_naming_it(repo):
    repo["writes"][("athena.web.lab", "athena.core.notes", "erase")] = "a reason"
    [failure] = _failures()
    assert failure.grew and failure.block == g.OUTSIDE_COMMAND
    assert "2 writes sit outside a command, above the ceiling of 1" in failure.message
    assert "new debtor: athena.web.lab -> athena.core.notes.erase." in failure.message


def test_the_generator_will_not_rewrite_the_table_past_its_ceiling(repo, capsys):
    # Regenerating would make the new debtor part of the accepted list, and the
    # next run could no longer say which one is new. So it refuses, and the
    # failure still names it after a regeneration attempt.
    before = g.DOC.read_text()
    _add_debtor(repo)
    assert g.main(check=False) == 1
    assert g.DOC.read_text() == before
    err = capsys.readouterr().err
    assert "refusing to rewrite docs/COMMAND_MIGRATION.md" in err
    assert "new debtor: athena.lab_commands.new_debt" in err
    [failure] = _failures()
    assert "athena.lab_commands.new_debt" in failure.message


def test_paid_debt_asks_for_the_ceiling_to_come_down(repo, capsys):
    module = repo["package"] / "lab_commands.py"
    module.write_text(
        module.read_text().replace(
            "old_debt(conn, *, actor_id)", "old_debt(conn, *, actor)"
        )
    )
    [failure] = _failures()
    assert not failure.grew
    assert failure.message == (
        "1 commands take a bare actor id, below the ceiling of 2: lower the ceiling "
        "to 1 (tool.athena.command_debts.bare_actor_id in pyproject.toml) so the "
        "paid debt cannot grow back."
    )
    # Regenerating the table is allowed (it shrinks), but the run still fails
    # until the ceiling comes down in the same change.
    assert g.main(check=False) == 1
    assert "lower the ceiling to 1" in capsys.readouterr().err
    assert g.main(check=True) == 1
    _ceilings(repo["root"], bare=1, outside=1)
    assert g.main(check=True) == 0


def test_paid_outside_command_debt_asks_for_the_ceiling_to_come_down(repo):
    repo["writes"].clear()
    [failure] = _failures()
    assert not failure.grew and failure.block == g.OUTSIDE_COMMAND
    assert "lower the ceiling to 0 (tool.athena.command_debts.outside_command" in (
        failure.message
    )


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("", "has no [tool.athena.command_debts] table"),
        ("[tool.athena.command_debts]\noutside_command = 1\n", "bare_actor_id"),
        (
            "[tool.athena.command_debts]\nbare_actor_id = -1\noutside_command = 1\n",
            "non-negative integer",
        ),
        (
            "[tool.athena.command_debts]\nbare_actor_id = true\noutside_command = 1\n",
            "non-negative integer",
        ),
        (
            "[tool.athena.command_debts]\nbare_actor_id = 1\noutside_command = 1\n"
            "other = 3\n",
            "keys no count reads",
        ),
    ],
    ids=["missing-table", "missing-key", "negative", "bool", "unknown-key"],
)
def test_malformed_ceilings_are_refused(tmp_path, text, complaint):
    config = tmp_path / "pyproject.toml"
    config.write_text(text)
    with pytest.raises(g.CeilingError, match=complaint.replace("[", r"\[")):
        g.load_ceilings(config)
