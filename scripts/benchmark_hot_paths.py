#!/usr/bin/env python3
"""Time the hot paths over real HTTP against a seeded fleet workspace (H8).

    python scripts/seed_benchmark.py /tmp/fleet10k.db --shape fleet --issues 10000
    python scripts/benchmark_hot_paths.py /tmp/fleet10k.db
    python scripts/benchmark_hot_paths.py /tmp/fleet10k.db --record docs/PERFORMANCE_MEDIANS.json

It copies the seeded database with SQLite's backup API (it never writes the
original), starts the installed ``athena-serve`` on the copy, on a port the
kernel picks, with the token rate limit and the background loops off, and asks
each path in ``READS`` ``--calls`` times on one keep-alive connection,
sequentially, after ``WARMUP`` unrecorded calls. It times the same desk, ready
and since reads in process, then runs the write loop: each seat in turn takes
work with ``POST /issues/claim-next``, comments on it, completes it and closes
it, until each has done ``--rounds`` or has nothing ready. It prints one line
per path and, with ``--record``, writes the medians to a JSON file.

The copy and the server's log live in a directory of their own under
``--scratch-root`` (the system's temporary directory by default). The server
runs in its own process group. A watcher process, started first, removes both
when this process ends for any reason, a SIGKILL included.

These numbers are for people, not for the gate. They move with the machine and
its load, so ``--record`` refuses when the machine is busier than it has CPUs
or the checkout differs from the commit the file would name, unless
``--record-anyway`` gives the reason, which is written into the file.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any
import uuid

import httpx

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "athena.hot_path_medians.v1"
WARMUP = 3
STARTUP_SECONDS = 120
LOG_TAIL_CHARS = 4_000

#: The reads, in the order they run: (name, who asks, path, query). "seat" is
#: seat 0, an agent with no desk cursor; "operator" is the admin. Each is a
#: path the round-2 review's performance table timed, or the desk as the other
#: kind of caller.
READS: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    ("GET /desk (seat)", "seat", "/desk", {}),
    ("GET /desk (operator)", "operator", "/desk", {}),
    ("GET /desk/ready (seat)", "seat", "/desk/ready", {}),
    ("GET /desk/since (seat, no cursor)", "seat", "/desk/since", {}),
    ("GET work-context (hot issue)", "seat", "/issues/{hot}/work-context", {}),
    ("GET /events (seat, 50 behind)", "seat", "/events", {"after": "{behind}"}),
    (
        "GET /notifications/priority (operator)",
        "operator",
        "/notifications/priority",
        {},
    ),
    ("GET /projects/{id}/brief (seat)", "seat", "/projects/{project}/brief", {}),
)
WRITES = (
    "POST /issues/claim-next (seat)",
    "POST /issues/{id}/comments (seat)",
)
#: The same reads in process, as seat 0, without HTTP or the server's other
#: work: the domain function each route calls.
IN_PROCESS = (
    "in process: desk.build_desk (seat)",
    "in process: desk.latest_visible_event_id (seat)",
    "in process: ready_queue.build_ready (seat)",
    "in process: operator_since.build_since (seat, no cursor)",
)

#: The watcher: reads the server's process group from its stdin, then blocks
#: until the stdin closes, which happens when the benchmark ends however it
#: ends. It then stops that group and removes the scratch directory.
_WATCHER = r"""
import os, shutil, signal, sys, time
scratch = sys.argv[1]
group = None
for line in sys.stdin:
    if line.strip().isdigit():
        group = int(line)
if group is not None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            break
        for _ in range(100):
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
shutil.rmtree(scratch, ignore_errors=True)
"""


def _summary(ms: list[float]) -> dict[str, Any]:
    ordered = sorted(ms)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "median_ms": round(statistics.median(ordered), 2),
        "p95_ms": round(p95, 2),
        "n": len(ordered),
    }


def _git(*args: str) -> str | None:
    """The command's output, or None when git is missing or fails here."""
    try:
        done = subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd=REPO, check=False
        )
    except OSError:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


class Server:
    """``athena-serve`` on a copy of the seeded database, in its own process
    group, with a watcher that cleans up after it whatever happens."""

    def __init__(self, seeded: Path, scratch_root: Path | None) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="athena-bench-", dir=scratch_root))
        self.process: subprocess.Popen[bytes] | None = None
        self.watcher = subprocess.Popen(
            [sys.executable, "-c", _WATCHER, str(self.scratch)],
            stdin=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            self._start(seeded)
        except BaseException:
            self.stop()
            raise

    def _start(self, seeded: Path) -> None:
        self.db = self.scratch / "bench.db"
        source = sqlite3.connect(f"file:{seeded}?mode=ro", uri=True)
        target = sqlite3.connect(self.db)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
        attachments = self.scratch / "attachments"
        attachments.mkdir()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.base = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        serve = Path(sys.executable).with_name("athena-serve")
        env = {k: v for k, v in os.environ.items() if not k.startswith("ATHENA_")}
        env.update(
            {
                "ATHENA_ATTACH_DIR": str(attachments),
                "ATHENA_AUTOMATION": "0",
                "ATHENA_DB": str(self.db),
                "ATHENA_LOG_LEVEL": "WARNING",
                "ATHENA_NETWORK_MODE": "local",
                "ATHENA_TOKEN_RATE_LIMIT_PER_MINUTE": "0",
                "ATHENA_TRUST_ACTOR_HEADER": "0",
                "ATHENA_WEBHOOK_DELIVERY": "0",
            }
        )
        self.log_path = self.scratch / "athena-serve.log"
        with self.log_path.open("w") as log:
            self.process = subprocess.Popen(
                [str(serve), "--fd", str(self.listener.fileno())],
                env=env,
                pass_fds=(self.listener.fileno(),),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        assert self.watcher.stdin is not None
        self.watcher.stdin.write(f"{self.process.pid}\n".encode())
        self.watcher.stdin.flush()
        (self.scratch / "server.pid").write_text(f"{self.process.pid}\n")
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"athena-serve exited with status {self.process.returncode} "
                    f"at start; its log ends:\n{self._log_tail()}"
                )
            try:
                if httpx.get(f"{self.base}/readyz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError(
            f"athena-serve was not ready in {STARTUP_SECONDS} s; its log ends:\n"
            f"{self._log_tail()}"
        )

    def _log_tail(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")[-LOG_TAIL_CHARS:]
        except OSError:
            return "(no log)"

    def stop(self) -> None:
        """Stop the server's process group, then let the watcher remove the
        scratch directory. Safe to call twice."""
        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
            except ProcessLookupError:
                pass
        listener = getattr(self, "listener", None)
        if listener is not None:
            listener.close()
        if self.watcher.stdin is not None and not self.watcher.stdin.closed:
            self.watcher.stdin.close()
        self.watcher.wait(timeout=60)


def _client(base: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=300
    )


def measure(
    seeded: Path, *, calls: int, rounds: int, scratch_root: Path | None = None
) -> dict[str, Any]:
    seed_map = json.loads(Path(f"{seeded}.json").read_text())
    if seed_map.get("shape") != "fleet":
        raise SystemExit(f"{seeded} was not seeded with --shape fleet")
    rows: dict[str, Any] = {}
    server = Server(seeded, scratch_root)
    try:
        seat = _client(server.base, seed_map["seats"][0]["token"])
        operator = _client(server.base, seed_map["operator"]["token"])
        callers = {"seat": seat, "operator": operator}
        head = operator.get("/desk").json()["signals"]["latest_visible_event_id"]
        values = {
            "hot": seed_map["hot_issue"],
            "behind": max(0, head - 50),
            "project": seed_map["projects"][0],
        }
        for name, who, path, query in READS:
            client = callers[who]
            url = path.format(**values)
            params = {key: str(value).format(**values) for key, value in query.items()}
            ms: list[float] = []
            status = size = None
            for n in range(WARMUP + calls):
                started = time.perf_counter()
                response = client.get(url, params=params)
                elapsed = (time.perf_counter() - started) * 1000
                status, size = response.status_code, len(response.content)
                if n >= WARMUP:
                    ms.append(elapsed)
            rows[name] = {**_summary(ms), "status": status, "bytes": size}
            print(
                f"{name:42s} {status} median={rows[name]['median_ms']:9.2f} "
                f"p95={rows[name]['p95_ms']:9.2f} bytes={size}",
                flush=True,
            )
        rows.update(_in_process(server.db, seed_map, calls=calls))
        rows.update(_write_loop(server.base, seed_map, rounds=rounds))
    finally:
        server.stop()
    return rows


def _in_process(database: Path, seed_map: dict, *, calls: int) -> dict[str, Any]:
    sys.path.insert(0, str(REPO / "src"))
    from athena.aegis import desk, operator_since, ready_queue
    from athena.core import db, tokens

    conn = db.connect(database)
    try:
        actor = tokens.inspect_token(conn, seed_map["seats"][0]["token"]).actor
        reads = dict(
            zip(
                IN_PROCESS,
                (
                    lambda: desk.build_desk(conn, actor=actor),
                    lambda: desk.latest_visible_event_id(conn, actor=actor),
                    lambda: ready_queue.build_ready(conn, actor=actor),
                    lambda: operator_since.build_since(conn, actor=actor),
                ),
                strict=True,
            )
        )
        out: dict[str, Any] = {}
        for name, read in reads.items():
            ms: list[float] = []
            for n in range(WARMUP + calls):
                started = time.perf_counter()
                read()
                if n >= WARMUP:
                    ms.append((time.perf_counter() - started) * 1000)
            out[name] = _summary(ms)
            print(
                f"{name:42s}     median={out[name]['median_ms']:9.2f} "
                f"p95={out[name]['p95_ms']:9.2f}",
                flush=True,
            )
        return out
    finally:
        conn.close()


def _write_loop(base: str, seed_map: dict, *, rounds: int) -> dict[str, Any]:
    timings: dict[str, list[float]] = {name: [] for name in WRITES}
    statuses: dict[str, set[int]] = {name: set() for name in WRITES}
    seats = [_client(base, seat["token"]) for seat in seed_map["seats"]]
    exhausted: set[int] = set()

    def call(name: str | None, client: httpx.Client, method: str, path: str, **kw):
        headers = {"Idempotency-Key": uuid.uuid4().hex}
        started = time.perf_counter()
        response = client.request(method, path, headers=headers, **kw)
        if name is not None:
            timings[name].append((time.perf_counter() - started) * 1000)
            statuses[name].add(response.status_code)
        if response.status_code >= 300:
            raise RuntimeError(
                f"{method} {path}: {response.status_code} {response.text}"
            )
        return response.json()

    for _round in range(rounds):
        for n, client in enumerate(seats):
            if n in exhausted:
                continue
            picked = call(WRITES[0], client, "POST", "/issues/claim-next")
            if not picked["claimed"]:
                exhausted.add(n)
                continue
            issue_id = picked["issue"]["id"]
            generation = picked["lease"]["generation"]
            if picked["open_handoff"] is not None:
                token = picked["open_handoff"]["handoff_token"]
                call(
                    None,
                    client,
                    "POST",
                    f"/issues/{issue_id}/claim-handoffs/{token}/resume",
                    json={"generation": generation, "resume_note": "Resumed."},
                )
            call(
                WRITES[1],
                client,
                "POST",
                f"/issues/{issue_id}/comments",
                json={"body": "Adjusted and verified against the case."},
            )
            call(
                None,
                client,
                "POST",
                f"/issues/{issue_id}/complete",
                json={"generation": generation},
            )
            call(None, client, "PATCH", f"/issues/{issue_id}", json={"status": "done"})
    out: dict[str, Any] = {}
    for name, ms in timings.items():
        if ms:
            out[name] = {**_summary(ms), "status": sorted(statuses[name])[0]}
            print(
                f"{name:42s} {out[name]['status']} median={out[name]['median_ms']:9.2f} "
                f"p95={out[name]['p95_ms']:9.2f} n={len(ms)}",
                flush=True,
            )
    return out


def record_refusals(
    *, load: float, cpus: int, commit: str | None, changes: str | None
) -> list[str]:
    """Why these medians are not a baseline, or nothing when they can be."""
    reasons = []
    if load > cpus:
        reasons.append(f"the 1-minute load average is {load:.1f} on {cpus} CPUs")
    if commit is None or changes is None:
        reasons.append("git cannot say which commit this checkout is")
    elif changes:
        reasons.append(f"the checkout differs from {commit}:\n{changes}")
    return reasons


def record(
    path: Path, seeded: Path, rows: dict[str, Any], *, calls: int, anyway: str | None
) -> None:
    seed_map = json.loads(Path(f"{seeded}.json").read_text())
    commit = _git("rev-parse", "--short", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=all")
    target = os.path.relpath(path.resolve(), REPO)
    changes = (
        None
        if status is None
        else "\n".join(line for line in status.splitlines() if line[3:] != target)
    )
    load = os.getloadavg()[0]
    refusals = record_refusals(
        load=load, cpus=os.cpu_count() or 1, commit=commit, changes=changes
    )
    if refusals and anyway is None:
        raise SystemExit(
            "not recording: " + "; ".join(refusals) + ". Measure on a quiet machine "
            "from the committed tree, or pass --record-anyway REASON."
        )
    document = {
        "schema": SCHEMA,
        "recorded": True,
        "meaning": (
            "Medians over real HTTP against athena-serve on a seeded fleet "
            "workspace, measured by scripts/benchmark_hot_paths.py. For people, "
            "not for the gate. See docs/PERFORMANCE.md."
        ),
        "commit": commit,
        "recorded_despite": None
        if not refusals
        else {"reason": anyway, "conditions": refusals},
        "measured_on": datetime.now(UTC).strftime("%Y-%m-%d"),
        "machine": f"{os.cpu_count()} CPUs, {sys.platform}, Python "
        f"{sys.version.split()[0]}, SQLite {sqlite3.sqlite_version}",
        "load_average_1m_at_end": round(load, 1),
        "workspace": {
            "shape": "fleet",
            "issues": seed_map["issues"],
            "analyzed": bool(seed_map.get("analyzed")),
            **seed_map["counts"],
        },
        "calls_per_read": calls,
        "paths": rows,
    }
    path.write_text(json.dumps(document, indent=1) + "\n")
    print(f"recorded {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "database",
        type=Path,
        help="a database seeded by scripts/seed_benchmark.py --shape fleet "
        "(its PATH.json sits beside it); it is copied, never written",
    )
    parser.add_argument("--calls", type=int, default=30, help="timed calls per read")
    parser.add_argument(
        "--rounds", type=int, default=10, help="write-loop rounds per seat"
    )
    parser.add_argument(
        "--record", type=Path, help="write the medians to this JSON file"
    )
    parser.add_argument(
        "--record-anyway",
        metavar="REASON",
        help="record although the machine is busy or the checkout is not a "
        "commit; the reason and the conditions are written into the file",
    )
    parser.add_argument(
        "--scratch-root",
        type=Path,
        help="where the copy and the server's log go (default: the system's "
        "temporary directory)",
    )
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGHUP, _terminate)
    rows = measure(
        args.database,
        calls=args.calls,
        rounds=args.rounds,
        scratch_root=args.scratch_root,
    )
    if args.record is not None:
        record(
            args.record,
            args.database,
            rows,
            calls=args.calls,
            anyway=args.record_anyway,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
