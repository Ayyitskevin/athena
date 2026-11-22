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

Usage:
    python scripts/generate_command_debtors.py            # rewrite the document
    python scripts/generate_command_debtors.py --check    # exit 1 if it drifted

``tests/test_command_debtors_doc.py`` runs the check, so the gate fails when
the committed tables no longer match the code.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "src" / "athena"
DOC = REPO / "docs" / "COMMAND_MIGRATION.md"
OWNERSHIP_CHECK = REPO / "scripts" / "check_write_ownership.py"
SOURCE = "scripts/generate_command_debtors.py"
PREFIX = "debtors:"
AUTOMATION_SUFFIX = "_as_automation"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generated_blocks  # noqa: E402 — after the sys.path entry above


def _module_name(path: Path) -> str:
    return "athena." + ".".join(path.relative_to(PACKAGE).with_suffix("").parts)


def bare_actor_commands() -> dict[str, list[str]]:
    """Command module -> its public functions that take ``actor_id`` and not
    ``actor``, in definition order."""
    found: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*_commands.py")):
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
                found.setdefault(_module_name(path), []).append(node.name)
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


def render_bare_actor() -> str:
    commands = bare_actor_commands()
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


def render_outside_command() -> str:
    writes = outside_command_writes()
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
        f"{PREFIX}bare-actor-id": render_bare_actor(),
        f"{PREFIX}outside-command": render_outside_command(),
    }
    current = DOC.read_text() if text is None else text
    try:
        return generated_blocks.splice(current, blocks, prefix=PREFIX, source=SOURCE)
    except ValueError as exc:
        raise SystemExit(f"docs/COMMAND_MIGRATION.md: {exc}") from None


def main() -> int:
    return generated_blocks.write_or_check(
        DOC, render(), repo=REPO, command=f"python {SOURCE}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
