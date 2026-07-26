"""The field exercise is a gate, not a demo.

`scripts/field_exercise.py` drives the full operator loop — onboard, delegate,
claim, heartbeat, gated dispatch, approval, real delivery, signed callbacks,
learning, runbook, undo — against a real Athena process and the reference
executor from `examples/`, over real loopback HTTP with real HMAC signatures on
both sides. Its first run surfaced two defects every stubbed test had missed
(the SSRF guard refusing all local executors, and the poster discarding the
acceptance body that carries the executor's run id), which is exactly why it
must keep running in CI rather than rotting as a one-time demo.

The independence test pins the other half of the dispatch contract's design:
the executor is a SEPARATE SYSTEM. The example must never import Athena — the
two share a secret and a wire format, not code — and keeping it stdlib-only is
what makes that provable rather than aspirational.
"""

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXECUTOR = REPO / "examples" / "icarus_executor.py"


def test_the_reference_executor_is_a_genuinely_separate_system():
    tree = ast.parse(EXECUTOR.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "athena" not in imported, "the executor must not import Athena"
    non_stdlib = imported - set(sys.stdlib_module_names)
    assert not non_stdlib, f"the executor must be stdlib-only, found: {non_stdlib}"


def test_the_loop_composes_over_real_http():
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "field_exercise.py")],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=REPO,
    )
    assert result.returncode == 0, (
        f"field exercise failed\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    assert "FIELD EXERCISE PASSED" in result.stdout


def test_the_sdist_ships_everything_the_exercise_spawns():
    """A source distribution carrying the exercise must carry the executor too.

    `scripts/field_exercise.py` spawns `examples/icarus_executor.py` by path, so
    an sdist with one and not the other ships a release gate that cannot run.
    That is exactly what the 0.1.0a1 packaging build produced: MANIFEST.in
    included `scripts/` (added long before) but not `examples/` (added by the
    stage that wrote the exercise). This asserts the declaration rather than
    building an sdist, so it costs nothing and still fails the moment the two
    files drift apart again.
    """
    manifest = (REPO / "MANIFEST.in").read_text(encoding="utf-8")
    spawned = EXECUTOR.relative_to(REPO).parts[0]
    assert f"recursive-include {spawned}" in manifest, (
        f"scripts/field_exercise.py spawns {EXECUTOR.relative_to(REPO)}, but "
        f"MANIFEST.in does not ship '{spawned}/' in the sdist"
    )
