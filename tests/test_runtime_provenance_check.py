"""The deploy checker must fail closed when checkout and process identity drift."""

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_runtime_provenance.py"
SPEC = importlib.util.spec_from_file_location("athena_runtime_provenance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


def test_matching_clean_checkout_is_accepted():
    errors = CHECKER.compare_provenance(
        runtime={
            "version": "0.1.0a1",
            "commit": "a" * 40,
            "tree_state": "clean",
            "source": "git-checkout",
        },
        checkout_commit="a" * 40,
        checkout_tree_state="clean",
    )

    assert errors == []


def test_commit_source_and_tree_drift_are_all_reported():
    errors = CHECKER.compare_provenance(
        runtime={
            "version": "0.1.0a1",
            "commit": "b" * 40,
            "tree_state": "dirty",
            "source": "installed-package",
        },
        checkout_commit="a" * 40,
        checkout_tree_state="dirty",
    )

    assert errors == [
        "runtime source is 'installed-package', expected 'git-checkout'",
        f"runtime commit {'b' * 40} does not match checkout {'a' * 40}",
        "runtime startup snapshot was dirty",
        "checkout is dirty now",
    ]


def test_malformed_runtime_payload_fails_closed():
    errors = CHECKER.compare_provenance(
        runtime={"status": "ok"},
        checkout_commit="a" * 40,
        checkout_tree_state="clean",
    )

    assert "runtime version is missing" in errors
    assert "runtime commit is missing or malformed" in errors


def test_runtime_payload_rejects_additional_keys():
    errors = CHECKER.compare_provenance(
        runtime={
            "version": "0.1.0a1",
            "commit": "a" * 40,
            "tree_state": "clean",
            "source": "git-checkout",
            "unexpected": "must fail closed",
        },
        checkout_commit="a" * 40,
        checkout_tree_state="clean",
    )

    assert errors == ["runtime payload has unexpected keys: unexpected"]


def test_main_refuses_a_checkout_that_changes_during_the_probe(
    tmp_path, monkeypatch, capsys
):
    snapshots = iter([("a" * 40, "clean"), ("b" * 40, "clean")])
    monkeypatch.setattr(
        CHECKER, "_checkout_snapshot", lambda _checkout: next(snapshots)
    )
    monkeypatch.setattr(
        CHECKER,
        "_fetch_runtime",
        lambda _url, _timeout: {
            "version": "0.1.0a1",
            "commit": "a" * 40,
            "tree_state": "clean",
            "source": "git-checkout",
        },
    )

    result = CHECKER.main(
        ["--url", "http://127.0.0.1:1/version", "--checkout", str(tmp_path)]
    )

    assert result == 1
    assert "checkout changed during provenance check" in capsys.readouterr().err


def test_checkout_snapshot_refuses_head_change_while_reading_status(
    tmp_path, monkeypatch
):
    results = iter(["a" * 40, "", "b" * 40])
    monkeypatch.setattr(CHECKER, "_git", lambda _checkout, *_args: next(results))

    try:
        CHECKER._checkout_snapshot(tmp_path)
    except RuntimeError as exc:
        assert str(exc) == "checkout changed while reading provenance"
    else:
        raise AssertionError("an unstable checkout snapshot was accepted")
