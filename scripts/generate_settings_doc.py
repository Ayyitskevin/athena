"""Generate the settings tables in docs/OPERATIONS.md from ``athena.config.SETTINGS``.

``SETTINGS`` is the one registry of every ``ATHENA_*`` environment variable the
code reads (``tests/test_settings_registry.py`` fails on a read it does not
declare and on an entry nothing reads). This script renders one table per
setting group into the generated block of the same name, ``settings:<group>``,
and leaves every other line of the document as written by hand.

Usage:
    python scripts/generate_settings_doc.py            # rewrite docs/OPERATIONS.md
    python scripts/generate_settings_doc.py --check    # exit 1 if the tables drifted

``tests/test_settings_registry.py`` runs the check, so the gate fails when the
committed tables no longer match the registry.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "OPERATIONS.md"
SOURCE = "scripts/generate_settings_doc.py"
PREFIX = "settings:"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generated_blocks  # noqa: E402 — after the sys.path entry above


def _settings():
    sys.path.insert(0, str(REPO / "src"))
    from athena import config  # noqa: PLC0415 — after sys.path

    return config.SETTING_GROUPS, config.SETTINGS


def shown_default(setting) -> str:
    """The default as the table shows it."""
    if setting.shown_default is not None:
        return setting.shown_default
    value = setting.default
    if value is None:
        return "*(unset)*"
    if isinstance(value, bool):
        return f"`{'true' if value else 'false'}`"
    if isinstance(value, float) and value.is_integer():
        return f"`{int(value)}`"
    if value == "":
        return "*(empty)*"
    return f"`{value}`"


def render_group(group: str, settings) -> str:
    rows = [
        "| Variable | Default | Secret | Use |",
        "|----------|---------|--------|-----|",
    ]
    for setting in settings.values():
        if setting.group != group:
            continue
        use = setting.description.replace("|", "\\|")
        secret = "yes" if setting.secret else "no"
        rows.append(
            f"| `{setting.name}` | {shown_default(setting)} | {secret} | {use} |"
        )
    if len(rows) == 2:
        raise SystemExit(f"setting group {group!r} declares no settings")
    return "\n".join(rows)


def render(text: str | None = None) -> str:
    """docs/OPERATIONS.md with every ``settings:<group>`` block regenerated."""
    groups, settings = _settings()
    blocks = {f"{PREFIX}{group}": render_group(group, settings) for group in groups}
    current = DOC.read_text() if text is None else text
    try:
        return generated_blocks.splice(current, blocks, prefix=PREFIX, source=SOURCE)
    except ValueError as exc:
        raise SystemExit(f"docs/OPERATIONS.md: {exc}") from None


def main() -> int:
    return generated_blocks.write_or_check(
        DOC, render(), repo=REPO, command=f"python {SOURCE}"
    )


if __name__ == "__main__":
    # --help answers without doing the work (tests/test_scripts_runnable.py).
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the document drifted, write nothing",
    )
    parser.parse_args()
    raise SystemExit(main())
