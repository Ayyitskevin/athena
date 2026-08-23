"""Build identity must describe the loaded checkout without inventing provenance."""

from pathlib import Path
import subprocess

from athena import __version__
from athena.core import provenance


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def test_non_checkout_reports_an_honest_installed_package(tmp_path):
    observed = provenance.detect_build_provenance(tmp_path)

    assert observed.as_dict() == {
        "version": __version__,
        "commit": None,
        "tree_state": "unknown",
        "source": "installed-package",
    }


def test_checkout_snapshot_distinguishes_clean_and_dirty_trees(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Athena test")
    _git(repo, "config", "user.email", "athena-test@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "test fixture")

    clean = provenance.detect_build_provenance(repo)
    assert clean.commit == _git(repo, "rev-parse", "--verify", "HEAD")
    assert clean.tree_state == "clean"
    assert clean.source == "git-checkout"

    tracked.write_text("dirty\n", encoding="utf-8")
    dirty = provenance.detect_build_provenance(repo)
    assert dirty.commit == clean.commit
    assert dirty.tree_state == "dirty"
    assert dirty.source == "git-checkout"


def test_installed_package_nested_inside_a_checkout_does_not_borrow_its_commit(
    tmp_path,
):
    repo = tmp_path / "checkout"
    package_parent = repo / ".venv" / "lib" / "python3.12"
    package_parent.mkdir(parents=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Athena test")
    _git(repo, "config", "user.email", "athena-test@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("checkout\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "--quiet", "-m", "test fixture")

    observed = provenance.detect_build_provenance(package_parent)

    assert observed.commit is None
    assert observed.tree_state == "unknown"
    assert observed.source == "installed-package"
