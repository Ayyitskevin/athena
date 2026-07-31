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
from io import BytesIO
import runpy
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

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


def test_reference_executor_sends_cumulative_safe_callbacks():
    # Pin the wire example itself, not merely the final field-exercise row. The
    # terminal report must repeat the canonical evidence so network reordering
    # converges, and malformed signature text must be a refusal rather than a
    # compare_digest TypeError.
    executor = runpy.run_path(str(EXECUTOR))
    payloads = []

    def capture(payload):
        payloads.append(payload)
        return 202, "accepted"

    do_the_work = executor["_do_the_work"]
    do_the_work.__globals__["_call_back"] = capture
    do_the_work(
        {"policy_digest": "digest", "idempotency_key": "key"},
        "executor-run",
    )
    assert len(payloads) == 2
    assert payloads[0]["evidence_ref"] == payloads[1]["evidence_ref"]
    assert payloads[1]["outcome"] == "completed"

    body = b'{"dispatch_id":1}'
    assert executor["_verified"]({"X-Athena-Signature": executor["_sign"](body)}, body)
    assert not executor["_verified"]({"X-Athena-Signature": "sha256=ÿ"}, body)
    first_run, should_launch = executor["_accept_once"]("dispatch-key")
    repeated_run, should_relaunch = executor["_accept_once"]("dispatch-key")
    assert first_run == repeated_run == "icarus-ref-dispatch-key"
    assert should_launch is True
    assert should_relaunch is False


def test_reference_executor_retries_the_pre_acceptance_404(monkeypatch):
    executor = runpy.run_path(str(EXECUTOR))
    callback = executor["_call_back"]

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"accepted"

    class FlakyOpener:
        attempts = 0

        def open(self, request, timeout):
            self.attempts += 1
            if self.attempts < 3:
                raise HTTPError(
                    request.full_url,
                    404,
                    "acceptance not visible yet",
                    {},
                    BytesIO(b'{"detail":"no such dispatch"}'),
                )
            return Response()

    opener = FlakyOpener()
    callback.__globals__["_OPENER"] = opener
    monkeypatch.setattr(callback.__globals__["time"], "sleep", lambda _delay: None)
    status, detail = callback({"icarus_run_id": "run", "policy_digest": "digest"})
    assert (status, detail) == (202, "accepted")
    assert opener.attempts == 3


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
