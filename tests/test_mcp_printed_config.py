"""Every MCP config Athena prints names a profile and a command that runs.

WHY: `claude_mcp_config` used to emit a bare `"command": "athena-mcp"` and no
`ATHENA_MCP_PROFILE`. Its three callers — `athena-demo`, `POST
/users/onboard_agent` and the cockpit's onboarding page — therefore handed every
new agent the full surface (114 tools on a member token) and a command that is
not on PATH unless the venv is activated. Decision D1 keeps an UNSET profile
meaning `full` at runtime, so the fix is in what Athena prints: `seat+knowledge`
for a token that may write pages, `seat` otherwise, and the absolute path of the
`athena-mcp` script beside the running interpreter.

Each emitter is exercised for a docs-scoped and a non-docs-scoped token, and the
printed config is then "launched" the way athena-mcp would launch it: its env
picks the profile, its token's whoami picks the scopes, and the server built
from those two carries the documented tool count.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path
import re
import sys

from fastapi.testclient import TestClient
import pytest

from athena import demo
from athena.core import tokens
from athena.main import create_app
from athena.mcp import config as mcp_config
from athena.mcp import server as mcp_server
from athena.mcp.client import AthenaClient

ISSUE_ONLY = ["read", "issue:write"]
WITH_DOCS = ["read", "issue:write", "docs:write"]
# (token scopes, the profile a printed config must name, its tool count; H6 added
# get_issue_comment to seat)
CASES = [
    pytest.param(ISSUE_ONLY, "seat", 29, id="issue-token"),
    pytest.param(WITH_DOCS, "seat+knowledge", 42, id="docs-token"),
]


def _launch(tc: TestClient, config: dict) -> set[str]:
    """Start the printed config the way athena-mcp does, in process: the env's
    profile, and the scopes a whoami with the env's token returns."""
    server = config["mcpServers"]["athena"]
    env = server["env"]
    tc.headers.pop("X-Athena-Actor", None)
    tc.headers["Authorization"] = f"Bearer {env['ATHENA_TOKEN']}"
    client = AthenaClient(client=tc)
    built = mcp_server.build_server(
        client,
        scopes=mcp_server.probe_scopes(client),
        profile=mcp_server.profile_from_env(env),
    )
    return {tool.name for tool in asyncio.run(built.list_tools())}


def _assert_printed(config: dict, profile: str) -> None:
    server = config["mcpServers"]["athena"]
    assert server["env"]["ATHENA_MCP_PROFILE"] == profile
    command = Path(server["command"])
    assert command.is_absolute(), server["command"]
    assert command.is_file() and os.access(command, os.X_OK), server["command"]
    assert command.parent == Path(sys.executable).absolute().parent


def _assert_launches(tc: TestClient, config: dict, profile: str, count: int) -> None:
    surface = _launch(tc, config)
    assert len(surface) == count
    assert surface <= mcp_server.PROFILES[profile]


def _bootstrap(tc: TestClient) -> None:
    tc.post("/users", json={"email": "ann@e.com", "name": "Ann", "password": "pw"})


# --- the three emitters --------------------------------------------------------


@pytest.mark.parametrize(("scopes", "profile", "count"), CASES)
def test_the_onboarding_api_prints_the_seat_profile_and_a_real_command(
    tmp_path, scopes, profile, count
):
    with TestClient(create_app(tmp_path / "api.db")) as tc:
        _bootstrap(tc)
        response = tc.post(
            "/users/onboard_agent",
            json={"email": "sol@e.com", "name": "Sol", "scopes": scopes},
            headers={"X-Athena-Actor": "1"},
        )
        assert response.status_code == 201, response.text
        config = response.json()["mcp_config"]
        _assert_printed(config, profile)
        _assert_launches(tc, config, profile, count)


@pytest.mark.parametrize(("scopes", "profile", "count"), CASES)
def test_the_cockpit_onboarding_page_prints_the_seat_profile_and_a_real_command(
    tmp_path, scopes, profile, count
):
    form = {"email": "sol@e.com", "name": "Sol"}
    form.update({f"scope_{scope.replace(':', '_')}": "1" for scope in scopes})
    with TestClient(create_app(tmp_path / "cockpit.db")) as tc:
        _bootstrap(tc)
        tc.post(
            "/login",
            data={"email": "ann@e.com", "password": "pw"},
            follow_redirects=False,
        )
        tc.headers["X-CSRF-Token"] = tc.cookies.get("athena_csrf", "")
        response = tc.post("/admin/agents/onboard", data=form)
        assert response.status_code == 201, response.text
        blocks = re.findall(
            r'<pre class="token-secret">(.*?)</pre>', response.text, re.S
        )
        config = json.loads(html.unescape(blocks[-1]))
        _assert_printed(config, profile)
        tc.cookies.clear()
        _assert_launches(tc, config, profile, count)


@pytest.mark.parametrize(("scopes", "profile", "count"), CASES)
def test_the_demo_prints_the_profile_its_token_earns_and_a_real_command(
    tmp_path, capsys, monkeypatch, scopes, profile, count
):
    # The demo's own token carries docs:write, so it prints seat+knowledge; the
    # issue-only case narrows that mint to prove the profile follows the token
    # rather than being written into the demo.
    if scopes != WITH_DOCS:
        real_mint = demo.token_commands.mint_token

        def narrowed_mint(conn, **kwargs):
            if kwargs.get("name") == "demo-mcp":
                kwargs["scopes"] = list(scopes)
            return real_mint(conn, **kwargs)

        monkeypatch.setattr(demo.token_commands, "mint_token", narrowed_mint)
    db_path = tmp_path / "demo.db"
    assert demo.main(["--db", str(db_path), "--seed-only", "--port", "8986"]) == 0
    printed = capsys.readouterr().out
    config = json.loads(printed[printed.index("{") :])
    assert config["mcpServers"]["athena"]["env"]["ATHENA_BASE_URL"] == (
        "http://127.0.0.1:8986"
    )
    _assert_printed(config, profile)
    with TestClient(create_app(db_path)) as tc:
        _assert_launches(tc, config, profile, count)


# --- the two rules, directly ----------------------------------------------------


def test_the_printed_profile_names_are_the_servers():
    assert mcp_config.SEAT_PROFILE in mcp_server.PROFILES
    assert mcp_config.SEAT_KNOWLEDGE_PROFILE in mcp_server.PROFILES
    assert mcp_config.seat_profile_for([]) == "seat"
    assert mcp_config.seat_profile_for(["read"]) == "seat"
    assert mcp_config.seat_profile_for(ISSUE_ONLY) == "seat"
    assert mcp_config.seat_profile_for(["read", "docs:write"]) == "seat+knowledge"
    # admin implies every scope, exactly as identity.token_has_scope decides
    assert mcp_config.seat_profile_for([tokens.ADMIN_SCOPE]) == "seat+knowledge"
    assert mcp_config._DOCS_WRITE_SCOPE == tokens.DOCS_WRITE_SCOPE
    assert mcp_config._ADMIN_SCOPE == tokens.ADMIN_SCOPE


def test_the_command_falls_back_to_the_bare_name_without_a_script(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "bin" / "python"))
    assert mcp_config.athena_mcp_command() == "athena-mcp"
    (tmp_path / "bin").mkdir()
    script = tmp_path / "bin" / "athena-mcp"
    script.write_text("#!/bin/sh\n")
    assert mcp_config.athena_mcp_command() == str(script)


def test_an_unset_profile_still_means_full_at_runtime():
    # D1: printing a profile changes no existing install.
    assert mcp_server.profile_from_env({}) is None
    assert mcp_server.PROFILES["full"] is None
