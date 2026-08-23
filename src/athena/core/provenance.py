"""Honest, row-free identity for the code a running Athena process loaded."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import subprocess
from typing import Literal

from athena import __version__

TreeState = Literal["clean", "dirty", "unknown"]
BuildSource = Literal["git-checkout", "installed-package"]
_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class BuildProvenance:
    version: str
    commit: str | None
    tree_state: TreeState
    source: BuildSource

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def detect_build_provenance(repo_root: Path | None = None) -> BuildProvenance:
    """Snapshot a checkout when available; otherwise report an honest package build."""
    root = (
        Path(__file__).resolve().parents[3]
        if repo_root is None
        else Path(repo_root).resolve()
    )
    git_root = _git(root, "rev-parse", "--show-toplevel")
    if git_root is None or Path(git_root).resolve() != root:
        return BuildProvenance(
            version=__version__,
            commit=None,
            tree_state="unknown",
            source="installed-package",
        )
    commit = _git(root, "rev-parse", "--verify", "HEAD")
    if commit is None or _FULL_COMMIT.fullmatch(commit) is None:
        return BuildProvenance(
            version=__version__,
            commit=None,
            tree_state="unknown",
            source="installed-package",
        )
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=normal")
    tree_state: TreeState = (
        "unknown" if status is None else ("dirty" if status else "clean")
    )
    return BuildProvenance(
        version=__version__,
        commit=commit,
        tree_state=tree_state,
        source="git-checkout",
    )


CURRENT_BUILD = detect_build_provenance()
