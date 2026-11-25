"""Generate the command-migration debtor tables in docs/COMMAND_MIGRATION.md.

COMMAND_MIGRATION.md defines a completed migration slice; this generator reads
the two parts of that definition the code states mechanically, and renders them
into the document's generated blocks:

1. **Commands that take a bare actor id** (slice rule 1: "the command accepts a
   resolved actor rather than trusting a bare actor id"). The rule: every public
   top-level function (name not starting with ``_``) of a ``*_commands.py``
   module under ``src/athena/`` that has a parameter named ``actor_id`` and none
   named ``actor``. A name ending in ``_as_automation`` is the automation
   engine's documented entry point and is marked as such, not dropped.
2. **Writes outside a command** (the transport side of "one command owns each
   write"): every exception in ``ALLOWED_TRANSPORT_WRITES`` of
   ``scripts/check_write_ownership.py``, the allowlist that check fails the
   build against — a transport calling a data-module writer that is not a
   command, with the reason the doctrine permits it. That file says it records
   debt; personal state and the login flow are the documented, by-design part.

What it cannot see: authorization or uniqueness checks that run at the
transport outside the command's transaction (slice rule 2), a command that
takes ``actor`` and still trusts it, writes that do not commit atomically with
their audit event (rule 3), REST/browser adapters that do not share one command
(rule 4), writes from non-transport modules (the ownership check covers
transports only), and dynamic dispatch. Those stay in the hand-written
"Known migration debt" column.

**The ratchet (G1).** Both counts can only go down. Each has a ceiling in
``[tool.athena.command_debts]`` of ``pyproject.toml``, beside the coverage
floors, under the same two-sided rule as those floors
(``scripts/check_coverage.py``):

- a count ABOVE its ceiling fails, naming each debtor the committed table does
  not list. The generator refuses to rewrite the document while a count is
  above its ceiling, so the committed table stays the last accepted list and a
  new debtor can always be named;
- a count BELOW its ceiling fails with "lower the ceiling to N", so debt one
  change paid cannot silently grow back in a later one.

Usage:
    python scripts/generate_command_debtors.py            # rewrite the document
    python scripts/generate_command_debtors.py --check    # exit 1 if it drifted
                                                          # or a count is off its
                                                          # ceiling

The gate's ``command-debts`` stage runs ``--check`` (``scripts/gate.py``), and
``tests/test_command_debtors_doc.py`` holds the tables to the code.
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Mapping
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import re
import sys
import tomllib

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "athena"
DOC = REPO / "docs" / "COMMAND_MIGRATION.md"
OWNERSHIP_CHECK = REPO / "scripts" / "check_write_ownership.py"
CONFIG = REPO / "pyproject.toml"
SOURCE = "scripts/generate_command_debtors.py"
PREFIX = "debtors:"
AUTOMATION_SUFFIX = "_as_automation"
BARE_ACTOR = f"{PREFIX}bare-actor-id"
OUTSIDE_COMMAND = f"{PREFIX}outside-command"
CEILINGS_TABLE = "tool.athena.command_debts"
# Each generated block -> its ceiling's key in CEILINGS_TABLE, what its count
# counts, and how a change pays one debtor off.
DEBTS: dict[str, tuple[str, str, str]] = {
    BARE_ACTOR: (
        "bare_actor_id",
        "commands take a bare actor id",
        "take the resolved actor and own the gate (slice rule 1)",
    ),
    OUTSIDE_COMMAND: (
        "outside_command",
        "writes sit outside a command",
        "move the write behind a command",
    ),
}

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generated_blocks  # noqa: E402 — after the sys.path entry above


def bare_actor_commands(package: Path | None = None) -> dict[str, list[str]]:
    """Command module -> its public functions that take ``actor_id`` and not
    ``actor``, in definition order."""
    package = PACKAGE if package is None else package
    found: dict[str, list[str]] = {}
    for path in sorted(package.rglob("*_commands.py")):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            arguments = node.args
            params = {
                a.arg
                for a in [
                    *arguments.posonlyargs,
                    *arguments.args,
                    *arguments.kwonlyargs,
                ]
            }
            if "actor_id" in params and "actor" not in params:
                module = "athena." + ".".join(
                    path.relative_to(package).with_suffix("").parts
                )
                found.setdefault(module, []).append(node.name)
    return found


def outside_command_writes() -> dict[tuple[str, str, str], str]:
    spec = importlib.util.spec_from_file_location(
        "check_write_ownership", OWNERSHIP_CHECK
    )
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load scripts/check_write_ownership.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_write_ownership"] = module
    spec.loader.exec_module(module)
    return dict(module.ALLOWED_TRANSPORT_WRITES)


def render_bare_actor(package: Path | None = None) -> str:
    commands = bare_actor_commands(package)
    if not commands:
        raise SystemExit("no *_commands.py module found; the scan is broken")
    rows = [
        "| Command module | Takes `actor_id`, not the actor | Automation entry points |",
        "|---|---|---|",
    ]
    total = 0
    for module, names in commands.items():
        ordinary = [name for name in names if not name.endswith(AUTOMATION_SUFFIX)]
        automation = [name for name in names if name.endswith(AUTOMATION_SUFFIX)]
        total += len(names)
        rows.append(
            f"| `{module}` | "
            + (", ".join(f"`{name}`" for name in ordinary) or "—")
            + " | "
            + (", ".join(f"`{name}`" for name in automation) or "—")
            + " |"
        )
    rows += [
        "",
        f"*{total} commands in {len(commands)} modules.*",
    ]
    return "\n".join(rows)


def render_outside_command(
    writes: Mapping[tuple[str, str, str], str] | None = None,
) -> str:
    writes = outside_command_writes() if writes is None else writes
    rows = [
        "| Transport | Writer it calls | Why the ownership check permits it |",
        "|---|---|---|",
    ]
    for (transport, module, function), reason in writes.items():
        caller = "any transport" if transport == "*" else f"`{transport}`"
        reason_text = " ".join(reason.split()).replace("|", "\\|")
        rows.append(f"| {caller} | `{module}.{function}` | {reason_text} |")
    rows += ["", f"*{len(writes)} writers.*"]
    return "\n".join(rows)


def render(text: str | None = None) -> str:
    """docs/COMMAND_MIGRATION.md with every ``debtors:`` block regenerated."""
    blocks = {
        BARE_ACTOR: render_bare_actor(),
        OUTSIDE_COMMAND: render_outside_command(),
    }
    current = DOC.read_text() if text is None else text
    try:
        return generated_blocks.splice(current, blocks, prefix=PREFIX, source=SOURCE)
    except ValueError as exc:
        raise SystemExit(f"docs/COMMAND_MIGRATION.md: {exc}") from None


# --- The ratchet -------------------------------------------------------------


def debtors(
    package: Path | None = None,
    writes: Mapping[tuple[str, str, str], str] | None = None,
) -> dict[str, list[str]]:
    """Each generated block -> the debtors the code states, one name apiece:
    ``module.function`` for a command that takes a bare actor id, and
    ``caller -> module.function`` for a write outside a command."""
    writes = outside_command_writes() if writes is None else writes
    return {
        BARE_ACTOR: [
            f"{module}.{name}"
            for module, names in bare_actor_commands(package).items()
            for name in names
        ],
        OUTSIDE_COMMAND: [
            f"{'any transport' if transport == '*' else transport} -> "
            f"{module}.{function}"
            for transport, module, function in writes
        ],
    }


_CODE_SPAN = re.compile(r"`([^`]+)`")


def listed(text: str) -> dict[str, set[str]]:
    """The debtors the document's generated tables list, named as ``debtors``
    names them: the last list the ratchet accepted."""
    found: dict[str, set[str]] = {BARE_ACTOR: set(), OUTSIDE_COMMAND: set()}
    for block, names in found.items():
        begin = generated_blocks.begin_marker(block, SOURCE)
        end = generated_blocks.end_marker(block)
        if begin not in text or end not in text.split(begin, 1)[1]:
            continue
        for row in text.split(begin, 1)[1].split(end, 1)[0].splitlines():
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            if not row.startswith("| ") or len(cells) < 3:
                continue
            if block == BARE_ACTOR:
                spans = _CODE_SPAN.findall(row)
                if spans and spans[0].startswith("athena."):
                    names.update(f"{spans[0]}.{span}" for span in spans[1:])
            elif cells[1].startswith("`athena."):
                names.add(f"{cells[0].strip('`')} -> {cells[1].strip('`')}")
    return found


class CeilingError(ValueError):
    """``[tool.athena.command_debts]`` is missing or malformed."""


def load_ceilings(config: Path | None = None) -> dict[str, int]:
    """Each generated block -> its ceiling, read from ``pyproject.toml``."""
    config = CONFIG if config is None else config
    table: object = tomllib.loads(config.read_text(encoding="utf-8"))
    for key in CEILINGS_TABLE.split("."):
        table = table.get(key) if isinstance(table, dict) else None
    if not isinstance(table, dict):
        raise CeilingError(f"{config.name} has no [{CEILINGS_TABLE}] table")
    ceilings = {}
    for block, (key, _, _) in DEBTS.items():
        value = table.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CeilingError(
                f"{CEILINGS_TABLE}.{key} must be a non-negative integer, got {value!r}"
            )
        ceilings[block] = value
    unknown = sorted(set(table) - {key for key, _, _ in DEBTS.values()})
    if unknown:
        raise CeilingError(f"[{CEILINGS_TABLE}] has keys no count reads: {unknown}")
    return ceilings


@dataclass(frozen=True)
class RatchetFailure:
    block: str
    grew: bool  # above its ceiling (new debt), not below it (paid debt)
    message: str


def ratchet(
    current: Mapping[str, list[str]],
    ceilings: Mapping[str, int],
    accepted: Mapping[str, set[str]],
) -> list[RatchetFailure]:
    """Each count against its ceiling. Above it names the new debtors (those
    ``accepted`` does not list); below it asks for the ceiling to come down;
    equal passes."""
    failures = []
    for block, (key, counted, payment) in DEBTS.items():
        count, ceiling = len(current[block]), ceilings[block]
        where = f"{CEILINGS_TABLE}.{key} in pyproject.toml"
        if count > ceiling:
            new = [name for name in current[block] if name not in accepted[block]]
            named = ", ".join(new) if new else "none outside the committed table"
            failures.append(
                RatchetFailure(
                    block,
                    True,
                    f"{count} {counted}, above the ceiling of {ceiling} ({where}); "
                    f"new debtor: {named}. The debt only goes down: {payment}.",
                )
            )
        elif count < ceiling:
            failures.append(
                RatchetFailure(
                    block,
                    False,
                    f"{count} {counted}, below the ceiling of {ceiling}: lower the "
                    f"ceiling to {count} ({where}) so the paid debt cannot grow back.",
                )
            )
    return failures


def main(*, check: bool) -> int:
    text = DOC.read_text()
    try:
        ceilings = load_ceilings()
    except (CeilingError, OSError, tomllib.TOMLDecodeError) as exc:
        sys.stderr.write(f"command-debt ratchet: {exc}\n")
        return 1
    current = debtors()
    failures = ratchet(current, ceilings, listed(text))
    for failure in failures:
        sys.stderr.write(f"command-debt ratchet failed: {failure.message}\n")
    if not check and any(failure.grew for failure in failures):
        # The committed table is the accepted list the next run names new
        # debtors against; it never absorbs debt past its ceiling.
        sys.stderr.write(
            "refusing to rewrite docs/COMMAND_MIGRATION.md while a count is above "
            "its ceiling\n"
        )
        return 1
    code = generated_blocks.write_or_check(
        DOC, render(text), repo=REPO, command=f"python {SOURCE}", check=check
    )
    if failures:
        return 1
    print(
        "command debts at their ceilings: "
        + ", ".join(f"{DEBTS[block][0]} {len(current[block])}" for block in DEBTS)
    )
    return code


if __name__ == "__main__":
    # --help answers without doing the work (tests/test_scripts_runnable.py).
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the document drifted or a count is off its ceiling; "
        "write nothing",
    )
    raise SystemExit(main(check=parser.parse_args().check))
