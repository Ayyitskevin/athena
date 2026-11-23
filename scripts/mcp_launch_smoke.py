"""Launch athena-mcp exactly as a printed MCP config says, against a real Athena.

Every in-process MCP test builds the server itself. This one does what an
operator's MCP client does with the block Athena prints: run the config's
``command`` with its ``env``, over stdio, through the MCP SDK's own stdio
client, against a listening Athena. It checks both callers of
``claude_mcp_config`` that a seat meets first:

- ``POST /users/onboard_agent`` for a ``read`` + ``issue:write`` token, which
  names the ``seat`` profile;
- the config ``athena-demo`` prints for its seeded agent, whose token carries
  ``docs:write`` and so names ``seat+knowledge``.

For each it lists the tools (the count must equal the profile's), calls
``my_desk`` and checks the identity is the token's account and the answer is
sent as compact JSON equal to its structured content (F12). Athena is
``athena-serve`` on an ephemeral loopback port, seeded by ``seed_demo``. Both
processes are stopped by pid, and the script fails if either outlives it.

Run it from a checkout with the venv installed:

    .venv/bin/python scripts/mcp_launch_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from athena.core import db, token_commands, tokens
from athena.demo import seed_demo
from athena.mcp.config import athena_mcp_command, claude_mcp_config, seat_profile_for
from athena.mcp.server import PROFILES

STARTUP_TIMEOUT_SECONDS = 20
# Loopback traffic must never detour through a proxy the environment configures
# (the same choice smoke_app.py makes).
_OPENER = build_opener(ProxyHandler({}))


class Failure(RuntimeError):
    pass


def _http(method: str, url: str, *, token: str | None = None, body: Any = None) -> Any:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(request, timeout=5) as response:  # noqa: S310 — loopback
        return json.loads(response.read() or b"null")


def _children_running(command_name: str) -> set[int]:
    """Pids of this process's children whose command line names the script."""
    found: set[int] = set()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
            cmdline = Path(entry.path, "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        parent = int(stat.rpartition(")")[2].split()[1])
        if parent == os.getpid() and any(
            Path(part.decode(errors="replace")).name == command_name for part in cmdline
        ):
            found.add(int(entry.name))
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    try:  # a zombie has exited; it only waits to be reaped
        return stat.read_text().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


async def _launch(config: dict, expected_profile: str, account_id: int) -> str:
    """Run the config's athena-mcp over stdio; list tools, call my_desk."""
    server = config["mcpServers"]["athena"]
    env = server["env"]
    if env["ATHENA_MCP_PROFILE"] != expected_profile:
        raise Failure(
            f"config names {env['ATHENA_MCP_PROFILE']}, not {expected_profile}"
        )
    if not Path(server["command"]).is_absolute():
        raise Failure(f"config command is not absolute: {server['command']}")
    parameters = StdioServerParameters(command=server["command"], args=[], env=env)
    name = Path(server["command"]).name
    pids: set[int] = set()
    with open(os.devnull, "w") as quiet:
        async with stdio_client(parameters, errlog=quiet) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                pids = _children_running(name)
                if len(pids) != 1:
                    raise Failure(f"expected one {name} process, found {sorted(pids)}")
                print(f"  .. athena-mcp pid {min(pids)} launched", flush=True)
                listed = (await session.list_tools()).tools
                expected = len(PROFILES[expected_profile] or ())
                if len(listed) != expected:
                    raise Failure(
                        f"{expected_profile}: {len(listed)} tools listed, "
                        f"the profile has {expected}"
                    )
                result = await session.call_tool("my_desk", {})
                if result.isError:
                    raise Failure(f"my_desk refused: {result.content}")
                text = "".join(getattr(b, "text", "") for b in result.content)
                desk = json.loads(text)
                # F12: the text is the structured content as compact JSON.
                if text != json.dumps(desk, ensure_ascii=False, separators=(",", ":")):
                    raise Failure("my_desk was not sent as compact JSON")
                if desk != result.structuredContent:
                    raise Failure("my_desk's text is not its structured content")
                if desk["identity"]["id"] != account_id:
                    raise Failure(f"my_desk answered for {desk['identity']['id']}")
    (pid,) = pids
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _alive(pid):
        os.kill(pid, signal.SIGKILL)
        raise Failure(f"athena-mcp (pid {pid}) outlived its client and was killed")
    return (
        f"{expected_profile}: pid {pid}, {len(listed)} tools, my_desk as {account_id}"
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    serve = Path(sys.executable).with_name("athena-serve")
    if not os.access(serve, os.X_OK):
        raise Failure(f"installed athena-serve console script is missing: {serve}")
    with tempfile.TemporaryDirectory(prefix="athena-mcp-launch-") as temp:
        root = Path(temp)
        db_path, attach_dir = root / "athena.db", root / "attachments"
        seeded = seed_demo(db_path, attach_dir=attach_dir)
        conn = db.connect(db_path)
        try:
            operator_token = token_commands.mint_token(
                conn,
                actor_id=seeded["ids"]["operator"],
                name="launch-smoke",
                scopes=[tokens.ADMIN_SCOPE],
            )["token"]
        finally:
            conn.close()

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("ATHENA_")}
        env.update(
            {
                "ATHENA_ATTACH_DIR": str(attach_dir),
                "ATHENA_AUTOMATION": "0",
                "ATHENA_DB": str(db_path),
                "ATHENA_LOG_LEVEL": "WARNING",
                "ATHENA_NETWORK_MODE": "local",
                "ATHENA_TRUST_ACTOR_HEADER": "0",
                "ATHENA_WEBHOOK_DELIVERY": "0",
            }
        )
        log = (root / "athena-serve.log").open("w")
        athena = subprocess.Popen(
            [str(serve), "--fd", str(listener.fileno())],
            env=env,
            pass_fds=(listener.fileno(),),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        print(f"  .. athena-serve pid {athena.pid} launched", flush=True)
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
            while True:
                if athena.poll() is not None:
                    raise Failure(f"athena-serve exited with {athena.returncode}")
                try:
                    if _http("GET", f"{base_url}/readyz") == {
                        "status": "ok",
                        "database": "ok",
                    }:
                        break
                except (OSError, URLError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise Failure("athena-serve did not become ready")
                time.sleep(0.1)
            print(f"  ok athena-serve pid {athena.pid} ready at {base_url}", flush=True)

            onboarded = _http(
                "POST",
                f"{base_url}/users/onboard_agent",
                token=operator_token,
                body={
                    "email": "seat@athena.local",
                    "name": "Launch Seat",
                    "scopes": ["read", "issue:write"],
                },
            )
            demo_config = claude_mcp_config(
                base_url=base_url,
                token=seeded["agent_token"],
                profile=seat_profile_for(seeded["agent_scopes"]),
                command=athena_mcp_command(),
            )
            for config, profile, account in (
                (onboarded["mcp_config"], "seat", onboarded["user"]["id"]),
                (demo_config, "seat+knowledge", seeded["ids"]["sol"]),
            ):
                print(
                    f"  ok {asyncio.run(_launch(config, profile, account))}", flush=True
                )
        finally:
            _stop(athena)
            listener.close()
            log.close()
        if _alive(athena.pid):
            raise Failure(f"athena-serve (pid {athena.pid}) is still running")
        print(f"  ok athena-serve pid {athena.pid} stopped", flush=True)
    print("MCP LAUNCH SMOKE PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"MCP LAUNCH SMOKE FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
