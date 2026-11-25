"""Every command a transport reaches is reached by both, or declared (C10).

WHY: Athena has two transports over one command layer, the server-rendered
cockpit (``web/``) and the REST API (``*_api.py``, ``api.py``; MCP is a client
of REST). A capability that only one of them reaches is invisible from the
other: an operator cannot do in the browser what an agent can do over REST, or
an agent gets 401/404 for what the cockpit does every day. The review that set
C10 found five capability groups only in the cockpit and six only in the API,
none of them written down. Some of those gaps are right (a browser password
change, an agent's heartbeat) and some are not, but each must be a decision
on the record, not an accident.

``scripts/check_surface_parity.py`` finds every command function reached from
only one transport and fails unless ``scripts/surface_parity.toml`` declares
it with a reason. These tests pin that it passes on the source, runs on its
own (as the gate and CI run it), fails each undeclared or stale shape on a
small synthetic package, and sees a command however a transport reaches it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_surface_parity.py"
DECLARATIONS = REPO / "scripts" / "surface_parity.toml"

_COMMANDS = (
    "def make(conn):\n    return read_thing(conn)\n"
    "def make_by_name(conn):\n    return 1\n"
    "def read_thing(conn):\n    return 1\n"
    "def both(conn):\n    return 1\n"
    "def unused(conn):\n    return 1\n"
    "class Refused(Exception):\n    pass\n"
)
_IMPORT = "from athena.aegis import thing_commands\n"
_COMMAND = "aegis.thing_commands"


def _load():
    spec = importlib.util.spec_from_file_location("check_surface_parity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load()


def _calls(*names: str, imports: str = _IMPORT, prefix: str = "thing_commands.") -> str:
    body = "".join(f"    {prefix}{name}(conn)\n" for name in names) or "    pass\n"
    return f"{imports}def handler(conn):\n{body}"


def _package(
    tmp_path: Path,
    *,
    web: tuple[str, ...] = (),
    api: tuple[str, ...] = (),
    modules: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "src" / "athena"
    sources = {
        "aegis.thing_commands": _COMMANDS,
        "web.pages": _calls(*web),
        "aegis.things_api": _calls(*api),
        **(modules or {}),
    }
    for module in sources:
        package = root.joinpath(*module.split(".")[:-1])
        while True:
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").touch()
            if package == root:
                break
            package = package.parent
    for module, source in sources.items():
        path = root.joinpath(*module.split(".")).with_suffix(".py")
        path.write_text(source, encoding="utf-8")
    return root


def _row(kind: str, *, cockpit=(), api=(), reason: str = "a stated reason") -> str:
    def names(values):
        return "[" + ", ".join(f'"{_COMMAND}.{v}"' for v in values) + "]"

    return (
        f'[[row]]\nkind = "{kind}"\ncockpit = {names(cockpit)}\n'
        f'api = {names(api)}\nreason = "{reason}"\n'
    )


def _violations(root: Path, *rows: str) -> list:
    return checker.find_violations(root, checker.parse_declarations("".join(rows)))


def _subjects(found) -> list[str]:
    return [v.subject for v in found]


# --- the real tree ----------------------------------------------------------


def test_the_check_passes_on_the_source():
    assert checker.find_violations() == []


def test_the_checker_runs_alone():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("Athena surface parity passed:")


def test_the_declared_list_is_the_one_the_script_reads():
    assert checker.DECLARATIONS == DECLARATIONS
    rows = checker.load_declarations()
    # Every kind of row the list documents is in use, and each says why.
    assert {row.kind for row in rows} <= set(checker.KINDS)
    assert all(len(row.reason) > 40 for row in rows)


# --- what fails -------------------------------------------------------------


def test_a_command_both_transports_reach_needs_no_row(tmp_path):
    assert _violations(_package(tmp_path, web=("both",), api=("both",))) == []


def test_an_undeclared_cockpit_only_command_fails(tmp_path):
    found = _violations(_package(tmp_path, web=("make", "both"), api=("both",)))

    assert _subjects(found) == [f"{_COMMAND}.make"]
    assert "reached only from the cockpit" in found[0].detail
    assert "not declared" in found[0].detail


def test_an_undeclared_api_only_command_fails(tmp_path):
    found = _violations(_package(tmp_path, web=("both",), api=("make", "both")))

    assert _subjects(found) == [f"{_COMMAND}.make"]
    assert "reached only from the REST API" in found[0].detail


@pytest.mark.parametrize("side", ["cockpit", "api"])
def test_a_declared_one_sided_command_passes(tmp_path, side):
    root = _package(tmp_path, **{"web" if side == "cockpit" else "api": ("make",)})
    assert _violations(root, _row("by-design", **{side: ("make",)})) == []


def test_a_declared_pair_is_matched(tmp_path):
    # The cockpit attaches by name, the API by id: one capability, two names.
    root = _package(tmp_path, web=("make_by_name",), api=("make",))
    pair = _row("pair", cockpit=("make_by_name",), api=("make",))
    assert _violations(root, pair) == []


def test_a_row_naming_a_function_that_no_longer_exists_fails(tmp_path):
    root = _package(tmp_path, web=("make",))
    found = _violations(root, _row("by-design", cockpit=("make", "gone")))

    assert _subjects(found) == [f"{_COMMAND}.gone"]
    assert "no such command function" in found[0].detail


def test_a_row_naming_a_module_that_no_longer_exists_fails(tmp_path):
    root = _package(tmp_path)
    rows = '[[row]]\nkind = "read"\ncockpit = ["aegis.gone_commands.make"]\n'
    found = _violations(root, rows + 'api = []\nreason = "r"\n')

    assert _subjects(found) == ["aegis.gone_commands.make"]
    assert "no such command function" in found[0].detail


def test_a_row_whose_command_is_now_on_both_sides_fails(tmp_path):
    root = _package(tmp_path, web=("make",), api=("make",))
    found = _violations(root, _row("missing-twin", cockpit=("make",)))

    assert _subjects(found) == [f"{_COMMAND}.make"]
    assert "reached from both transports" in found[0].detail
    assert "delete it from the row" in found[0].detail


def test_a_pair_whose_half_is_now_on_both_sides_fails(tmp_path):
    root = _package(tmp_path, web=("make_by_name", "make"), api=("make",))
    pair = _row("pair", cockpit=("make_by_name",), api=("make",))
    assert _subjects(_violations(root, pair)) == [f"{_COMMAND}.make"]


def test_a_row_whose_command_no_transport_reaches_fails(tmp_path):
    found = _violations(_package(tmp_path), _row("read", api=("unused",)))

    assert _subjects(found) == [f"{_COMMAND}.unused"]
    assert "no transport reaches it" in found[0].detail


def test_a_row_on_the_wrong_side_fails(tmp_path):
    root = _package(tmp_path, web=("make",))
    found = _violations(root, _row("by-design", api=("make",)))

    assert [v.detail for v in found] == [
        "declared under api, but it is reached only from the cockpit"
    ]


def test_a_command_declared_twice_fails(tmp_path):
    root = _package(tmp_path, web=("make",))
    once = _row("by-design", cockpit=("make",))
    found = _violations(root, once, _row("missing-twin", cockpit=("make",)))

    assert _subjects(found) == [f"{_COMMAND}.make"]
    assert "declared in 2 rows" in found[0].detail


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (_row("someday", cockpit=("make",)), "kind"),
        (_row("by-design", cockpit=("make",), reason=" "), "reason"),
        (_row("by-design"), "names no command"),
        (_row("by-design", cockpit=("make",), api=("both",)), "one side"),
        (_row("pair", cockpit=("make",)), "both sides"),
        (_row("read", cockpit=("make",)) + 'owner = "kevin"\n', "unknown key"),
        *(
            (
                f'[[row]]\nkind = "read"\ncockpit = ["{name}"]\nreason = "r"\n',
                "_commands",
            )
            for name in (
                "aegis.issues.make",  # a data module, not a command module
                "thing_commands.make",  # no package
                "athena.aegis.thing_commands.make",  # the prefix is left off
            )
        ),
        ("[[rows]]\n", "unknown table"),
        ("row = [", "cannot parse"),
    ],
    ids=[
        "unknown-kind",
        "blank-reason",
        "empty",
        "one-sided-with-two-sides",
        "pair-with-one-side",
        "extra-key",
        "data-module",
        "no-package",
        "prefixed",
        "wrong-table",
        "not-toml",
    ],
)
def test_a_malformed_row_is_refused(rows, message):
    with pytest.raises(checker.ParityCheckError, match=message):
        checker.parse_declarations(rows)


# --- what counts as reached -------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        _calls("make"),
        _calls(
            "make",
            imports="from athena.aegis import thing_commands as t\n",
            prefix="t.",
        ),
        _calls(
            "make", imports="from athena.aegis.thing_commands import make\n", prefix=""
        ),
        _calls(
            "make",
            imports="from athena.aegis.thing_commands import make as build\n",
            prefix="",
        ).replace("make(", "build("),
        _calls(
            "make",
            imports="import athena.aegis.thing_commands\n",
            prefix="athena.aegis.thing_commands.",
        ),
        _calls("make", imports="from ..aegis import thing_commands\n"),
        # Passed as a value (a dependency, a callback), not called here.
        _IMPORT + "HANDLER = thing_commands.make\n",
        # Through a helper the cockpit calls, which calls the command.
        "from athena.aegis import thing_view\ndef page(conn):\n    thing_view.show(conn)\n",
        # Through a helper's helper.
        "from athena.aegis.thing_view import deep\ndef page(conn):\n    deep(conn)\n",
        # Through a module-level table in a helper.
        "from athena.aegis import thing_view\ndef page(conn):\n    thing_view.TABLE['m'](conn)\n",
        # Through a function of the other transport's module (the cockpit's
        # approvals page calls approvals_api.decide_for_actor).
        "from athena.aegis import things_api\n"
        "def page(conn):\n    things_api.handler(conn)\n",
    ],
    ids=[
        "module-attribute",
        "module-alias",
        "imported-name",
        "imported-name-alias",
        "dotted-import",
        "relative-import",
        "value",
        "helper",
        "helper-of-helper",
        "helper-table",
        "other-transport",
    ],
)
def test_every_way_a_transport_reaches_a_command_is_seen(tmp_path, source):
    view = (
        _IMPORT
        + "def show(conn):\n    return thing_commands.make(conn)\n"
        + "def deep(conn):\n    return show(conn)\n"
        + "TABLE = {'m': thing_commands.make}\n"
    )
    root = _package(
        tmp_path,
        api=("make",),
        modules={"web.pages": source, "aegis.thing_view": view},
    )
    report = checker.check(root, ())

    # Reached, and nothing more: make's own call to read_thing is not the cockpit's.
    assert report.cockpit == {f"{_COMMAND}.make"}
    assert report.violations == []


def test_a_command_reached_only_inside_the_command_layer_is_not_counted(tmp_path):
    # make calls read_thing; the cockpit reaching make does not reach read_thing.
    report = checker.check(_package(tmp_path, web=("make",), api=("make",)), ())
    assert report.cockpit == report.api == {f"{_COMMAND}.make"}


@pytest.mark.parametrize(
    ("module", "side"),
    [
        ("web.admin.pages", "cockpit"),
        ("aegis.api", "api"),
        ("mentor.notebook_api", "api"),
        ("mcp.tools_work", None),  # MCP is a client of REST, not a transport here.
        ("main", None),  # The composition root: wiring, not a transport.
        ("aegis.automation", None),  # Not a transport unless one reaches it.
    ],
)
def test_which_modules_are_transports(tmp_path, module, side):
    root = _package(tmp_path, modules={module: _calls("make")})
    report = checker.check(root, ())
    reached = {
        "cockpit": f"{_COMMAND}.make" in report.cockpit,
        "api": f"{_COMMAND}.make" in report.api,
    }
    assert reached == {"cockpit": side == "cockpit", "api": side == "api"}


def test_a_class_in_a_command_module_is_not_a_command(tmp_path):
    root = _package(tmp_path, web=("Refused",))
    assert checker.check(root, ()).cockpit == set()


def test_a_tree_without_a_cockpit_is_refused(tmp_path):
    root = _package(tmp_path)
    (root / "web" / "pages.py").unlink()
    (root / "web" / "__init__.py").unlink()
    (root / "web").rmdir()
    with pytest.raises(checker.ParityCheckError, match="cockpit"):
        checker.check(root, ())


def test_the_command_line_reports_each_finding(tmp_path):
    root = _package(tmp_path, web=("make",), api=("unused",))
    declared = tmp_path / "declared.toml"
    declared.write_text(_row("read", api=("unused",)), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--package-root",
            str(root),
            "--declarations",
            str(declared),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.startswith("Athena surface parity failed: 1 violation\n")
    assert f"{_COMMAND}.make: reached only from the cockpit" in result.stderr
