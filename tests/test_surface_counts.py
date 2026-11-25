"""The surface's size is accounted for, and growth says what it earns (B6b).

WHY: the prune ledger's counts were from August (362 routes, 124 tools, 65
tables) against 417, 146 and 73, and nothing made an addition say what it was
for. ``scripts/check_surface_counts.py`` measures the three and fails unless
``scripts/surface_counts.toml`` accounts for each, with ``earns`` on every
addition.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import gate  # noqa: E402


def _checker():
    spec = importlib.util.spec_from_file_location(
        "check_surface_counts", REPO / "scripts" / "check_surface_counts.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["check_surface_counts"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _checker()


@pytest.fixture(scope="module")
def measured(checker):
    return checker.measure()


BASELINE = """
[baseline]
date = "2026-11-25"
routes = 10
tools = 5
tables = 3
"""


def _ledger(checker, tmp_path, text: str):
    path = tmp_path / "surface_counts.toml"
    path.write_text(BASELINE + text, encoding="utf-8")
    return checker.read_ledger(path)


def test_the_checkout_is_accounted_for(checker, measured):
    assert checker.check(measured, checker.read_ledger(checker.LEDGER)) == []
    assert checker.main([]) == 0


def test_the_counts_measure_what_the_header_says(checker, measured):
    from athena.mcp import server as mcp_server

    assert measured["tools"] == len(mcp_server.declared())
    # Every count is a real size, not a stub.
    assert measured["routes"] > 300 and measured["tables"] > 50


def test_an_unrecorded_addition_fails_and_says_how_to_record_it(checker, tmp_path):
    ledger = _ledger(checker, tmp_path, "")
    problems = checker.check({"routes": 11, "tools": 5, "tables": 3}, ledger)
    assert problems == [
        "routes: measured 11, the ledger accounts for 10; add a [[change]] to "
        "scripts/surface_counts.toml with routes = +1 and `earns`, what the "
        "addition is for"
    ]


def test_an_unrecorded_removal_fails_too(checker, tmp_path):
    ledger = _ledger(checker, tmp_path, "")
    (problem,) = checker.check({"routes": 10, "tools": 4, "tables": 3}, ledger)
    assert problem.startswith("tools: measured 4, the ledger accounts for 5")
    assert "tools = -1" in problem and "earns" not in problem


def test_a_recorded_change_balances_the_counts(checker, tmp_path):
    ledger = _ledger(
        checker,
        tmp_path,
        """
[[change]]
change = "X1"
routes = 2
tables = 1
earns = "the operator can do X without a shell"

[[change]]
change = "X2 prune"
tools = -1
""",
    )
    assert checker.check({"routes": 12, "tools": 4, "tables": 4}, ledger) == []


def test_an_addition_without_earns_fails(checker, tmp_path):
    ledger = _ledger(
        checker, tmp_path, '[[change]]\nchange = "X1"\nroutes = 1\nearns = "  "\n'
    )
    (problem,) = checker.check({"routes": 11, "tools": 5, "tables": 3}, ledger)
    assert problem.startswith("[[change]] 1 (X1) adds to the surface without `earns`")


@pytest.mark.parametrize(
    ("entry", "says"),
    [
        ('routes = 1\nearns = "x"\n', "names no `change`"),
        ('change = "  "\nroutes = 1\nearns = "x"\n', "names no `change`"),
        ('change = "X"\n', "changes no count"),
        ('change = "X"\nroutes = 1\nearns = "x"\nlines = 3\n', "unknown keys: lines"),
        ('change = "X"\nroutes = "1"\nearns = "x"\n', "not an integer"),
    ],
)
def test_a_malformed_change_fails(checker, tmp_path, entry, says):
    ledger = _ledger(checker, tmp_path, "[[change]]\n" + entry)
    problems = checker.check({"routes": 11, "tools": 5, "tables": 3}, ledger)
    assert any(says in problem for problem in problems), problems


def test_a_baseline_missing_a_count_fails(checker, tmp_path):
    path = tmp_path / "surface_counts.toml"
    path.write_text("[baseline]\nroutes = 1\ntools = 1\n", encoding="utf-8")
    problems = checker.check(
        {"routes": 1, "tools": 1, "tables": 1}, checker.read_ledger(path)
    )
    assert problems == ["[baseline] has no integer tables"]


def test_the_cli_fails_on_a_ledger_that_does_not_balance(checker, tmp_path, capsys):
    path = tmp_path / "surface_counts.toml"
    path.write_text("[baseline]\nroutes = 1\ntools = 1\ntables = 1\n", encoding="utf-8")
    assert checker.main(["--ledger", str(path)]) == 1
    assert "Athena surface counts failed" in capsys.readouterr().out


def test_the_product_gate_and_ci_run_it():
    (stage,) = [stage for stage in gate.STAGES if stage.name == "surface-counts"]
    assert stage.command[1:] == ("scripts/check_surface_counts.py",)
    workflow = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "run: .venv/bin/python scripts/check_surface_counts.py" in workflow
