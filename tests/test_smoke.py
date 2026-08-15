"""The first test. Proves the package imports and the test runner works.

Run it with:  pytest
"""

import athena


def test_version_is_present():
    assert athena.__version__ == "0.1.0a1"


def test_the_packaged_version_and_the_dunder_version_agree():
    """The version lives in two files, and them drifting apart is the classic
    release bug: the wheel says one thing and the running app reports another.

    The publish workflow refuses to run when they disagree — this is the same check
    at the point where it is cheap to notice, rather than at the moment someone is
    trying to cut a release. A bump is one commit that changes both, plus the
    literal above; RELEASING.md says so too, but a test is what enforces it.
    """
    import pathlib
    import tomllib

    import athena

    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    packaged = tomllib.loads(pyproject.read_text())["project"]["version"]
    assert packaged == athena.__version__, (
        f"pyproject.toml says {packaged}, src/athena/__init__.py says "
        f"{athena.__version__}"
    )
