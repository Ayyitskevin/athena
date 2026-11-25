"""Generate the settings tables in the documents from ``athena.config.SETTINGS``.

``SETTINGS`` is the one registry of every ``ATHENA_*`` environment variable the
code reads (``tests/test_settings_registry.py`` fails on a read it does not
declare and on an entry nothing reads). This script renders one table per
setting group into docs/OPERATIONS.md's generated block of the same name,
``settings:<group>``, and the few settings a topic document shows beside its
prose (``EXCERPTS``: dispatch in docs/DISPATCH.md, the worker registry in
docs/WORKERS.md) into that document's block, from the same registry rows. Every
other line of each document stays as written by hand.

Usage:
    python scripts/generate_settings_doc.py            # rewrite the documents
    python scripts/generate_settings_doc.py --check    # exit 1 if a table drifted

``tests/test_settings_registry.py`` runs the check, so the gate fails when a
committed table no longer matches the registry.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "OPERATIONS.md"
SOURCE = "scripts/generate_settings_doc.py"
PREFIX = "settings:"

# A topic document's own settings, shown beside its prose and rendered from the
# registry rather than kept by hand (G9: DISPATCH.md and WORKERS.md kept hand
# tables whose defaults and wording could drift from the code). Document path
# -> block name -> the settings it shows, in that order. A name the registry
# does not declare stops the run.
EXCERPTS: dict[str, dict[str, tuple[str, ...]]] = {
    "docs/DISPATCH.md": {
        "settings:dispatch": (
            "ATHENA_ICARUS_URL",
            "ATHENA_ICARUS_SECRET",
            "ATHENA_ICARUS_TIMEOUT_SECONDS",
            "ATHENA_EGRESS_PRIVATE_HOSTS",
            "ATHENA_ANON_RATE_LIMIT_PER_MINUTE",
        ),
    },
    "docs/WORKERS.md": {
        "settings:workers": (
            "ATHENA_WORKER_STALE_SECONDS",
            "ATHENA_WORKER_MAX_PER_AGENT",
        ),
    },
}

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


def render_rows(chosen) -> str:
    """The table of the ``chosen`` settings, one row each, in the order given."""
    rows = [
        "| Variable | Default | Secret | Use |",
        "|----------|---------|--------|-----|",
    ]
    for setting in chosen:
        use = setting.description.replace("|", "\\|")
        secret = "yes" if setting.secret else "no"
        rows.append(
            f"| `{setting.name}` | {shown_default(setting)} | {secret} | {use} |"
        )
    return "\n".join(rows)


def render_group(group: str, settings) -> str:
    chosen = [setting for setting in settings.values() if setting.group == group]
    if not chosen:
        raise SystemExit(f"setting group {group!r} declares no settings")
    return render_rows(chosen)


def render_excerpt(names: tuple[str, ...], settings) -> str:
    unknown = [name for name in names if name not in settings]
    if unknown:
        raise SystemExit(f"settings the registry does not declare: {unknown}")
    return render_rows(settings[name] for name in names)


def _splice(doc: Path, blocks: dict[str, str], text: str | None) -> str:
    current = doc.read_text() if text is None else text
    try:
        return generated_blocks.splice(current, blocks, prefix=PREFIX, source=SOURCE)
    except ValueError as exc:
        raise SystemExit(f"{doc.relative_to(REPO)}: {exc}") from None


def render(text: str | None = None) -> str:
    """docs/OPERATIONS.md with every ``settings:<group>`` block regenerated."""
    groups, settings = _settings()
    blocks = {f"{PREFIX}{group}": render_group(group, settings) for group in groups}
    return _splice(DOC, blocks, text)


def render_topic(relative: str, text: str | None = None) -> str:
    """One ``EXCERPTS`` document with its settings blocks regenerated."""
    _, settings = _settings()
    blocks = {
        name: render_excerpt(names, settings)
        for name, names in EXCERPTS[relative].items()
    }
    return _splice(REPO / relative, blocks, text)


def main() -> int:
    command = f"python {SOURCE}"
    stale = generated_blocks.write_or_check(DOC, render(), repo=REPO, command=command)
    for relative in EXCERPTS:
        stale |= generated_blocks.write_or_check(
            REPO / relative, render_topic(relative), repo=REPO, command=command
        )
    return stale


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
