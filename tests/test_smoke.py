"""The first test. Proves the package imports and the test runner works.

Run it with:  pytest
"""
import athena


def test_version_is_present():
    assert athena.__version__ == "0.0.1"
