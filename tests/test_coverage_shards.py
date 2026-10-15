"""Bound coverage memory without losing tests, evidence, or the required gate."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/coverage_shards.py"
SPEC = importlib.util.spec_from_file_location("coverage_shards", SCRIPT)
assert SPEC and SPEC.loader
shards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shards)


def test_required_test_gate_requires_every_coverage_shard():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert "coverage" in jobs, "instrumented suites need isolated, bounded workers"
    assert jobs["coverage"]["strategy"]["matrix"]["shard"] == [0, 1, 2, 3]
    assert jobs["coverage"]["strategy"]["fail-fast"] is False
    assert jobs["test"]["needs"] == ["coverage"]
    assert jobs["test"]["if"] == "${{ always() }}"
    steps = jobs["test"]["steps"]
    assert steps[0]["env"]["SHARD_RESULT"] == "${{ needs.coverage.result }}"
    assert steps[0]["run"] == 'test "$SHARD_RESULT" = success'
    assert all(not step.get("continue-on-error", False) for step in steps)
    assert any("coverage_shards.py combine" in step.get("run", "") for step in steps)


def test_partition_conserves_exact_ids_including_lookalikes():
    ids = sorted(["a", "a ", "a\u00a0", "\u0430", "b", "c", "d", "e", "f"])
    parts = [shards.partition(ids, i) for i in range(4)]
    assert sorted(node for part in parts for node in part) == ids
    assert parts[0] == ids[0::4]
    assert max(map(len, parts)) - min(map(len, parts)) <= 1


@pytest.mark.parametrize("ids", [[], ["a", "a"], ["b", "a"]])
def test_partition_refuses_empty_duplicate_or_unsorted_inventory(ids):
    with pytest.raises(ValueError):
        shards.partition(ids, 0)


def reports(ids):
    return [
        {"id": node, "phase": phase, "outcome": "passed"}
        for node in ids
        for phase in ("setup", "call", "teardown")
    ]


def write_receipts(root):
    ids = [f"tests/test_demo.py::test_case[{i}]" for i in range(8)]
    for i in range(4):
        directory = root / f"coverage-shard-{i}"
        directory.mkdir()
        data = directory / ".coverage"
        data.write_bytes(b"synthetic coverage bytes; only manifest validation")
        selected = shards.partition(ids, i)
        (directory / "receipt.json").write_text(
            json.dumps(
                {
                    "head": "candidate",
                    "index": i,
                    "shards": 4,
                    "inventory": ids,
                    "selected": selected,
                    "reports": reports(selected),
                    "policy_sha256": "policy",
                    "data_sha256": shards.digest(data),
                }
            )
        )
    return ids


def test_complete_receipts_admit_only_the_four_explicit_data_files(tmp_path):
    ids = write_receipts(tmp_path)
    paths = shards.verify_receipts(tmp_path, ids, "candidate", "policy")
    assert paths == [tmp_path / f"coverage-shard-{i}/.coverage" for i in range(4)]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-shard",
        "extra-shard",
        "stale-head",
        "policy-drift",
        "missing-id",
        "duplicate-id",
        "extra-id",
        "missing-report",
        "duplicate-report",
        "skip",
        "failed-teardown",
        "wrong-index",
        "wrong-shard-count",
        "coverage-drift",
        "symlink-data",
        "empty-data",
        "inventory-drift",
    ],
)
def test_fanin_refuses_incomplete_or_mismatched_evidence(tmp_path, mutation):
    ids = write_receipts(tmp_path)
    directory = tmp_path / "coverage-shard-0"
    path = directory / "receipt.json"
    receipt = json.loads(path.read_text())
    if mutation == "missing-shard":
        directory.rename(tmp_path / "missing")
    elif mutation == "extra-shard":
        (tmp_path / "coverage-shard-4").mkdir()
    elif mutation == "coverage-drift":
        (directory / ".coverage").write_bytes(b"changed")
    elif mutation == "empty-data":
        (directory / ".coverage").write_bytes(b"")
    elif mutation == "symlink-data":
        (directory / ".coverage").rename(directory / "original")
        (directory / ".coverage").symlink_to(directory / "original")
    else:
        if mutation == "stale-head":
            receipt["head"] = "old-candidate"
        elif mutation == "policy-drift":
            receipt["policy_sha256"] = "other"
        elif mutation == "missing-id":
            receipt["selected"].pop()
        elif mutation == "duplicate-id":
            receipt["selected"].append(receipt["selected"][0])
        elif mutation == "extra-id":
            receipt["selected"].append("invented")
        elif mutation == "missing-report":
            receipt["reports"].pop()
        elif mutation == "duplicate-report":
            receipt["reports"].append(receipt["reports"][0])
        elif mutation == "skip":
            receipt["reports"][1]["outcome"] = "skipped"
        elif mutation == "failed-teardown":
            receipt["reports"][-1]["outcome"] = "failed"
        elif mutation == "wrong-index":
            receipt["index"] = 1
        elif mutation == "wrong-shard-count":
            receipt["shards"] = 3
        elif mutation == "inventory-drift":
            receipt["inventory"].pop()
        path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        shards.verify_receipts(tmp_path, ids, "candidate", "policy")


def test_real_pytest_collection_records_parameter_ids_without_parsing_output(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n@pytest.mark.parametrize('value', [0,1,2,3])\n"
        "def test_sample(value): assert value >= 0\n"
    )
    inventory = tmp_path / "inventory.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "inventory", str(inventory)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(inventory.read_text()) == [
        f"test_sample.py::test_sample[{i}]" for i in range(4)
    ]


def test_non_success_shard_results_fail_the_real_required_job_guard():
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    command = jobs["test"]["steps"][0]["run"]
    for result in ["failure", "cancelled", "skipped", "", "success"]:
        run = subprocess.run(["bash", "-c", command], env={"SHARD_RESULT": result})
        assert (run.returncode == 0) is (result == "success")


def test_real_shards_combine_branches_and_refuse_a_failed_test(tmp_path):
    repo = tmp_path / "repo"
    package = repo / "src/athena"
    package.mkdir(parents=True)
    (repo / "scripts").mkdir()
    shutil.copyfile(
        ROOT / "scripts/check_coverage.py", repo / "scripts/check_coverage.py"
    )
    (package / "__init__.py").write_text(
        "def choose(value):\n    if value:\n        return 1\n    return 0\n"
    )
    (package / "demo.py").write_text(
        'if __name__ == "__main__":  # pragma: no cover - console-script parity\n'
        "    raise SystemExit(0)\n"
    )
    (repo / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath=["src"]\n'
        '[tool.coverage.run]\nbranch=true\nrelative_files=true\nsource=["src/athena"]\n'
        "[tool.coverage.report]\nprecision=2\nshow_missing=true\nskip_empty=false\n"
        "[tool.athena.coverage]\nline_floor=100\nbranch_floor=100\n"
        "combined_floor=100\nexcluded_line_count=2\n"
    )
    test = repo / "test_demo.py"
    test.write_text(
        "import pytest\nfrom athena import choose\n"
        "@pytest.mark.parametrize('value', [0,1,2,3])\n"
        "def test_choose(value): assert choose(value) == bool(value)\n"
    )
    for args in [
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
    ]:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    environment = os.environ.copy()
    # Do not let the enclosing coverage run redirect this isolated fixture.
    environment.pop("COVERAGE_FILE", None)
    inputs = tmp_path / "shards"
    for i in range(4):
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "run",
                str(i),
                str(inputs / f"coverage-shard-{i}"),
            ],
            cwd=repo,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "combine",
            str(inputs),
            str(tmp_path / "combined"),
        ],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 exact test IDs, 4 shards" in result.stdout
    assert (
        json.loads((tmp_path / "combined/coverage.json").read_text())["totals"][
            "missing_branches"
        ]
        == 0
    )
    # Controlled behavioral failure: real pytest must fail, not emit a receipt.
    test.write_text(test.read_text().replace("== bool(value)", "== 99"))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "0", str(tmp_path / "failed")],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    assert "1 failed" in result.stdout
    assert not (tmp_path / "failed/receipt.json").exists()
