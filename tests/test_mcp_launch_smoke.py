"""The MCP config Athena prints launches a working server (A16).

`scripts/mcp_launch_smoke.py` starts a real Athena on an ephemeral loopback
port, then launches `athena-mcp` from the config `claude_mcp_config` emits, the
way an MCP client does: through the SDK's stdio client, with the config's
command and environment only. Each launch must list its profile's tool count
and answer `my_desk` as the token's account. In-process tests build the server
themselves, so a config that names a missing command, a wrong profile or an
environment the process cannot start with would pass all of them.

The script needs a listening server, so it runs as a subprocess, as
`tests/test_field_exercise.py` runs the field exercise. Every process it
starts prints its pid; whatever the outcome, this test kills the script's
process group and any printed pid still running, so a failure leaves nothing.
"""

import os
from pathlib import Path
import re
import signal
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "mcp_launch_smoke.py"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


def test_the_printed_mcp_config_launches_lists_its_profile_and_answers_my_desk():
    process = subprocess.Popen(
        [sys.executable, str(SCRIPT)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    launched = [int(pid) for pid in re.findall(r"pid (\d+) launched", stdout)]
    survivors = [pid for pid in launched if _alive(pid)]
    for pid in survivors:
        os.kill(pid, signal.SIGKILL)
    assert process.returncode == 0, (
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
    )
    assert "MCP LAUNCH SMOKE PASSED" in stdout
    assert "seat: pid" in stdout and "26 tools" in stdout
    assert "seat+knowledge: pid" in stdout and "37 tools" in stdout
    assert len(launched) == 3, stdout  # athena-serve and two athena-mcp
    assert survivors == [], f"processes outlived the smoke check: {survivors}"
