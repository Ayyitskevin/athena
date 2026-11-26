"""Every example in the entry documents runs, and returns what the document says.

WHY (A15b): the document tests checked that a named route and tool exist, not
that the example works. A request with the wrong body, a call missing a
required argument, a tool the reader's profile does not carry, or a count that
drifted all passed. An agent reads these five documents first and does what
they show, so each example is executed here against a seeded Athena:

- every fenced request (``http`` blocks, ``curl`` in ``bash``/``sh`` blocks)
  runs through ``TestClient`` and must answer the status the document shows
  (``HTTP NNN`` in the prose after it) or a 2xx when it shows none, and the
  response shown beside it must name only fields the real response has;
- every tool call shown (inline ``name(args)``, or a JSON block of arguments)
  is checked against the tool's real input schema, must be in the document's
  profile or in a profile its paragraph names, and is run where it can be;
- the counts and sizes the documents state are compared with the code.

Nothing is skipped silently: a block is executed, validated against a real
response, or listed in ``EXCLUDED`` below with its reason, and a block no rule
classifies fails. ``tests/doc_examples.py`` is the machinery; the tables here
are the only per-document knowledge.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import dataclasses
import re
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from athena import config
from athena.aegis import lease_commands, leases, project_brief
from athena.core import db, reported_usage, token_commands, tokens
from athena.demo import seed_demo
from athena.main import create_app
from athena.mcp import server as mcp_server

import doc_examples as harness
from doc_examples import Workspace

ENTRY_DOCUMENTS = (
    "AGENT_GUIDE.md",
    "AGENT_BOOT.md",
    "RUNTIME_RECIPE.md",
    "QUICKSTART.md",
    "CLOSEOUT_RECIPE.md",
)
BASE_URL = "http://127.0.0.1:8000"

# --- which workspace each document runs against, and its reader's profile ---------
#
# The agent-facing documents run against the seeded demo, as Sol, the demo's
# agent (AGENT_GUIDE.md: "Every request and response below comes from a real run
# of athena-demo"). QUICKSTART.md path B starts from an empty database, and
# RUNTIME_RECIPE.md steps 1 and 2 are for the instance QUICKSTART bootstraps
# (the recipe tells a demo user to skip them: the demo already has Sol).
WORKSPACE = {
    "AGENT_GUIDE.md": "demo",
    "AGENT_BOOT.md": "demo",
    "CLOSEOUT_RECIPE.md": "demo",
    "QUICKSTART.md": "empty",
    "RUNTIME_RECIPE.md": "bootstrapped",
}
# The profile a document's reader carries. A seat is `seat`; the recipe's agent
# carries `seat+knowledge`, the profile its onboarding prints for its scopes
# (the harness compares the recipe's config block with the one Athena emits).
DEFAULT_PROFILE = {
    "AGENT_GUIDE.md": "seat",
    "AGENT_BOOT.md": "seat",
    "CLOSEOUT_RECIPE.md": "seat",
    "QUICKSTART.md": "seat",
    "RUNTIME_RECIPE.md": "seat+knowledge",
    "SYNTHETIC.md": "seat",
    "SYNTHETIC_LOOP.md": "seat",
    # The prompt the recipe hands its agent, which carries the recipe's profile.
    "desk_loop.md": "seat+knowledge",
}

# examples/desk_loop.md is the prompt RUNTIME_RECIPE.md step 4 pastes into the
# agent it onboarded: a sequence of steps, each a call the agent makes. G9: it
# was checked for tool names and never run, so it told a `seat+knowledge` agent
# to call `begin_run`, which that profile does not carry, and to pick work from
# a `delegated` lane the desk does not have. It now runs, writes included, in
# the workspace the recipe leaves (test below).
DESK_LOOP = "desk_loop.md"
DESK_LOOP_PATH = harness.REPO / "examples" / DESK_LOOP

# --- blocks that are not requests --------------------------------------------------
#
# (document, text the block contains) -> why it is not executed. An entry that
# matches no block fails, so this list cannot go stale.
EXCLUDED: dict[tuple[str, str], str] = {
    ("QUICKSTART.md", "git clone"): (
        "installation: clones the repository and builds the venv this suite runs in"
    ),
    ("QUICKSTART.md", ".venv/bin/athena-demo --db"): (
        "starts a server; the demo workspace the other documents run against is "
        "seeded by the same seed_demo() this command calls"
    ),
    ("QUICKSTART.md", ".venv/bin/athena-serve --host 127.0.0.1 --port 8000"): (
        "starts the server; the TestClient app stands in for it"
    ),
    ("QUICKSTART.md", "athena-field-guide ~/athena-data/athena.db"): (
        "an offline CLI over the database file, not a request "
        "(tests/test_field_guide.py runs it)"
    ),
    ("QUICKSTART.md", "athena-field-guide ~/athena-data/athena.db --check"): (
        "an offline CLI over the database file, not a request "
        "(tests/test_field_guide.py runs it)"
    ),
}

# Commands that may stand beside a curl in a shell block, and why they are not run.
SHELL_COMMANDS: dict[str, str] = {
    "athena-seat-doctor": "a CLI over the same GET /desk (tests/test_seat_doctor.py)",
    "mkdir": "creates the data directory; the test uses a temporary one",
    "export": "sets the variables the harness substitutes ($ATHENA_*)",
    "unset": "clears a variable; nothing to run",
    ".venv/bin/athena-serve": "starts the server; the TestClient app stands in",
    "kill": "stops the server started above",
}


# --- state the prose implies before a block ----------------------------------------


def _hold(ws: Workspace, issue_id: int, headers: dict[str, str] | None = None) -> dict:
    """The caller holds the issue: its current lease, or a fresh claim."""
    caller = headers or ws.bearer()
    lease = ws.client.get(f"/issues/{issue_id}/lease", headers=caller).json()
    me = ws.client.get("/users/me", headers=caller).json()["id"]
    if lease and lease.get("active") and lease.get("holder_id") == me:
        return lease
    tag = ws.client.get(f"/issues/{issue_id}", headers=caller).headers["ETag"]
    claimed = ws.client.post(
        f"/issues/{issue_id}/claim", headers={**caller, "If-Match": tag}, json={}
    )
    assert claimed.status_code == 201, claimed.text
    return claimed.json()


def _guide_yield_issue(ws: Workspace) -> None:
    """AGENT_GUIDE section 8 yields issue 4, which the caller holds ("Yield when
    you cannot continue"). The demo seeds three issues; the operator files the
    fourth, delegates it to Sol, and Sol claims it, so the id is the document's
    own and not a substitution."""
    made = ws.client.post(
        "/issues",
        headers=ws.as_admin(),
        json={"title": "Decide the follow-up's scope", "project_id": 1},
    )
    assert made.status_code == 201 and made.json()["id"] == 4, made.text
    sol = ws.facts["seeded"]["ids"]["sol"]
    delegated = ws.client.post(
        "/issues/4/delegate", headers=ws.as_admin(), json={"user_id": sol}
    )
    assert delegated.status_code == 201, delegated.text
    _hold(ws, 4)


def _operator_steers_the_run(ws: Workspace) -> None:
    """RUNTIME_RECIPE step 5: "While the agent is working, send a steer", to
    the agent's session run, which step 7 of the prompt then answers."""
    sent = ws.client.post(
        "/run-controls",
        headers=ws.as_admin(),
        json={
            "run_id": ws.facts["run_id"],
            "kind": "steer",
            "payload": "Narrow the scope: just the stale steps, leave the wording alone.",
        },
    )
    assert sent.status_code == 201, sent.text


PRECONDITIONS: dict[tuple[str, str], Callable[[Workspace], None]] = {
    ("AGENT_GUIDE.md", "POST /issues/4/yield"): _guide_yield_issue,
    (DESK_LOOP, "record_run_learning("): _operator_steers_the_run,
}


# --- responses whose request is described in prose ----------------------------------


def _resume_as_next_claimant(ws: Workspace) -> httpx.Response:
    """ "The next claimant sees the handoff in the claim response ... then resume
    it explicitly": the operator claims issue 4 and resumes the open handoff."""
    claimed = _hold(ws, 4, ws.as_admin())
    handoff = claimed["open_claim_handoff"]
    assert handoff, claimed
    return ws.client.post(
        f"/issues/4/claim-handoffs/{handoff['handoff_token']}/resume",
        headers=ws.as_admin(),
        json={"generation": claimed["generation"]},
    )


def _claim_without_generation(ws: Workspace) -> httpx.Response:
    # "Claim again without the generation, and the server refuses" (Sol holds 1).
    tag = ws.client.get("/issues/1", headers=ws.bearer()).headers["ETag"]
    return ws.client.post(
        "/issues/1/claim", headers={**ws.bearer(), "If-Match": tag}, json={}
    )


PROVOKED: dict[tuple[str, str], Callable[[Workspace], httpx.Response]] = {
    # "A misspelled atom is an error, not an empty list"
    ("AGENT_GUIDE.md", '"code":"invalid_query"'): lambda ws: ws.client.get(
        "/issues", params={"q": "is:open asignee:@me"}, headers=ws.bearer()
    ),
    # "MCP get_issue_work_context(ref) · REST GET /issues/{ref}/work-context"
    ("AGENT_GUIDE.md", '"issue_etag"'): lambda ws: ws.client.get(
        "/issues/ATH-1/work-context", headers=ws.bearer()
    ),
    ("AGENT_GUIDE.md", '"code":"lease_generation_required"'): _claim_without_generation,
    # "REST PATCH /issues/{issue_id} {"status":"done"}" by the gated demo agent
    ("AGENT_GUIDE.md", '"code":"approval_required"'): lambda ws: ws.client.patch(
        "/issues/1", headers=ws.bearer(), json={"status": "done"}
    ),
    ("AGENT_GUIDE.md", '"state":"resumed"'): _resume_as_next_claimant,
}


# --- JSON blocks that are a tool call's arguments ------------------------------------


def _complete_claim_with(ws: Workspace, shown: dict[str, Any]) -> tuple[str, dict]:
    """CLOSEOUT_RECIPE shows complete_claim's report arguments; "Include the
    ordinary issue ID and exact held generation too". Sol completes issue 1,
    claiming it again first when an earlier block released it."""
    held = _hold(ws, 1)
    return "complete_claim", {**shown, "issue_id": 1, "generation": held["generation"]}


ARGUMENTS: dict[
    tuple[str, str],
    Callable[[Workspace, dict[str, Any]], tuple[str, dict[str, Any]]],
] = {
    ("CLOSEOUT_RECIPE.md", '"verdict": "PASS-WITH-NOTES"'): _complete_claim_with,
    ("CLOSEOUT_RECIPE.md", '"usage": {'): _complete_claim_with,
    ("CLOSEOUT_RECIPE.md", '"candidate_submission": {'): _complete_claim_with,
}


# --- the values an inline call leaves to its reader ----------------------------------


def _current_issue(ws: Workspace) -> dict | None:
    if ws.issue_id is None:
        return None
    return ws.client.get(f"/issues/{ws.issue_id}", headers=ws.bearer()).json()


def _issue_etag(ws: Workspace) -> str | None:
    """The issue's current ETag, as the caller reads it ("<the issue's current
    ETag>")."""
    if ws.issue_id is None:
        return None
    return ws.client.get(f"/issues/{ws.issue_id}", headers=ws.bearer()).headers.get(
        "ETag"
    )


def _held_generation(ws: Workspace) -> str | None:
    """The generation of the caller's lease on the issue ("<the lease generation
    you were given>"); None when the caller holds none."""
    if ws.issue_id is None:
        return None
    lease = ws.client.get(f"/issues/{ws.issue_id}/lease", headers=ws.bearer())
    held = lease.json() if lease.status_code == 200 else None
    return held.get("generation") if isinstance(held, dict) else None


def _open_control(ws: Workspace) -> int | None:
    """The oldest control in the caller's inbox nobody has settled."""
    inbox = ws.client.get(
        "/run-controls", params={"state": "open"}, headers=ws.bearer()
    ).json()
    return min((control["id"] for control in inbox), default=None)


FILLERS: dict[str, Callable[[Workspace], Any]] = {
    "issue_id": lambda ws: ws.issue_id,
    "ref": lambda ws: (_current_issue(ws) or {}).get("key"),
    "project_id": lambda ws: (_current_issue(ws) or {}).get("project_id"),
    "if_match": _issue_etag,
    "generation": _held_generation,
    "control_id": _open_control,
}

TABLES = harness.Tables(
    default_profile=DEFAULT_PROFILE,
    excluded=EXCLUDED,
    shell_commands=SHELL_COMMANDS,
    preconditions=PRECONDITIONS,
    provoked=PROVOKED,
    arguments=ARGUMENTS,
    fillers=FILLERS,
    runs_writes=frozenset({DESK_LOOP}),
)


# --- workspaces -----------------------------------------------------------------------


def _admin_token(db_file, admin_id: int) -> str:
    """A token with the Admin scope, as RUNTIME_RECIPE step 1 has the operator
    create at /settings/tokens (the same command that page calls)."""
    conn = db.connect(db_file)
    try:
        minted = token_commands.mint_token(
            conn, actor={"id": admin_id}, name="docs-admin", scopes=[tokens.ADMIN_SCOPE]
        )
    finally:
        conn.close()
    return minted["token"]


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Iterator[Callable[[str], Workspace]]:
    """A factory: the workspace a document runs against, opened once per call."""
    opened: list[TestClient] = []

    def open_(kind: str) -> Workspace:
        db_file = tmp_path / f"{kind}-{len(opened)}.db"
        env = {"ATHENA_BASE_URL": BASE_URL}
        seeded = None
        if kind == "demo":
            seeded = seed_demo(db_file, attach_dir=tmp_path / f"attach-{len(opened)}")
            env["ATHENA_TOKEN"] = seeded["agent_token"]
        app = create_app(db_file)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        ws = Workspace(app, client, env)
        if kind == "empty":
            # athena-serve --bootstrap requires a password and reads the process's
            # bootstrap token, which the document exports.
            monkeypatch.setattr(config, "BOOTSTRAP_PASSWORD_REQUIRED", True)
            env["ATHENA_BOOTSTRAP_TOKEN"] = config.BOOTSTRAP_TOKEN
        if kind == "bootstrapped":
            made = client.post(
                "/users",
                headers={"X-Athena-Bootstrap-Token": config.BOOTSTRAP_TOKEN},
                json={"email": "you@example.com", "name": "You", "password": "pw"},
            )
            assert made.status_code == 201, made.text
            ws.admin_token = env["ATHENA_ADMIN_TOKEN"] = _admin_token(db_file, 1)
        if seeded is not None:
            ws.facts["seeded"] = seeded
            ws.admin_token = _admin_token(db_file, seeded["ids"]["operator"])
            ws.session = harness.session_for(app, seeded["agent_token"], "seat")
        return ws

    yield open_
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture(scope="module")
def schemas() -> dict[str, Any]:
    return harness.input_schemas()


def _run(doc: str, text: str, workspace, schemas) -> harness.Report:
    kind = WORKSPACE.get(doc, "demo")
    return harness.run_document(doc, text, workspace(kind), TABLES, schemas)


# --- the five documents --------------------------------------------------------------


@pytest.mark.parametrize("doc", ENTRY_DOCUMENTS)
def test_every_example_in_the_document_runs_as_shown(doc, workspace, schemas):
    text = (harness.DOCS / doc).read_text(encoding="utf-8")
    report = _run(doc, text, workspace, schemas)
    assert report.failures == [], "\n".join(report.failures)
    # Every block was executed, validated against a real answer, or excluded.
    seen = len(report.executed) + len(report.validated) + len(report.excluded)
    assert seen == report.blocks, (report.blocks, report)
    # Every inline call was run or validated.
    assert len(report.calls_run) + len(report.calls_validated) == report.calls
    # The harness really reached each document: none is all exclusions.
    assert report.executed or report.calls_run, report


def test_each_document_runs_its_agent_on_the_profile_it_names():
    """RUNTIME_RECIPE's reader carries `seat+knowledge` because its config block
    names it; that block is compared with the config Athena emits (above)."""
    recipe = (harness.DOCS / "RUNTIME_RECIPE.md").read_text(encoding="utf-8")
    assert '"ATHENA_MCP_PROFILE": "seat+knowledge"' in recipe
    assert DEFAULT_PROFILE[DESK_LOOP] == DEFAULT_PROFILE["RUNTIME_RECIPE.md"]
    synthetic = {"SYNTHETIC.md", "SYNTHETIC_LOOP.md", DESK_LOOP}
    assert set(DEFAULT_PROFILE) - synthetic == set(ENTRY_DOCUMENTS)
    assert set(WORKSPACE) == set(ENTRY_DOCUMENTS)


# --- the desk-loop prompt, run where the recipe leaves its agent ------------------------


def _lane(desk: dict, path: str) -> object:
    value: object = desk
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def test_the_desk_loop_prompt_runs_where_the_recipe_leaves_its_agent(
    workspace, schemas
):
    """RUNTIME_RECIPE.md onboards an agent, delegates an issue to it and hands
    it examples/desk_loop.md. Here the recipe runs, then the prompt runs as
    that agent's session (the profile its printed config names, and a run
    identity of its own as athena-mcp gives every session): every call each
    step shows is made in order, writes included, and must succeed."""
    ws = workspace("bootstrapped")
    recipe = harness.run_document(
        "RUNTIME_RECIPE.md", _read("RUNTIME_RECIPE.md"), ws, TABLES, schemas
    )
    assert recipe.failures == [], "\n".join(recipe.failures)
    env = ws.facts["mcp_config"]["mcpServers"]["athena"]["env"]
    ws.facts["run_id"] = "mcp-0123456789ab"
    ws.session = harness.session_for(
        ws.app,
        env["ATHENA_TOKEN"],
        env["ATHENA_MCP_PROFILE"],
        run_id=ws.facts["run_id"],
    )
    ws.issue_id = None  # the prompt picks its issue from the desk
    ws.facts["answers"] = []

    prompt = DESK_LOOP_PATH.read_text(encoding="utf-8")
    report = harness.run_document(DESK_LOOP, prompt, ws, TABLES, schemas)
    assert report.failures == [], "\n".join(report.failures)
    seen = len(report.executed) + len(report.validated) + len(report.excluded)
    assert seen == report.blocks, report
    assert len(report.calls_run) + len(report.calls_validated) == report.calls

    # Each step's call ran: the loop is executed, not only named.
    ran = [tool for tool, _ in ws.facts["answers"]]
    for step in (
        "my_desk",
        "current_run",
        "heartbeat_agent_run",
        "get_issue_work_context",
        "get_issue_lease",
        "claim_issue",
        "record_run_learning",
        "complete_claim",
        "list_run_controls",
        "acknowledge_run_control",
        "complete_run_control",
    ):
        assert step in ran, (step, report.calls_validated)
    # ...on the issue the recipe delegated, which the desk offered.
    assert ws.issue_id == 1

    # The lanes the prompt names are lanes of the desk it read.
    desk = next(answer for tool, answer in ws.facts["answers"] if tool == "my_desk")
    lanes = re.findall(r"`([a-z_]+(?:\.[a-z_]+)*)` lane", prompt)
    assert lanes, "the prompt no longer names a desk lane"
    assert [lane for lane in lanes if _lane(desk, lane) is None] == []
    # ...and the runbook fields it reads are fields of the work context it read.
    context = next(
        answer
        for tool, answer in ws.facts["answers"]
        if tool == "get_issue_work_context"
    )
    fields = re.findall(r"`(runbook\.[a-z_]+)`", prompt)
    assert fields, "the prompt no longer says how a first learning finds its space"
    assert [path for path in fields if _lane(context, path) is None] == []

    # What the trail now holds is what the recipe says the operator will see.
    admin = ws.as_admin()
    assert ws.client.get("/issues/1/lease", headers=admin).json() in (None, {})
    controls = ws.client.get(
        "/run-controls", params={"run_id": ws.facts["run_id"]}, headers=admin
    ).json()
    assert [control["state"] for control in controls] == ["completed"]


SYNTHETIC_LOOP = """# A synthetic prompt whose steps no longer run

Claim the issue you already hold:

```
claim_issue(issue_id=<id>, if_match="<the issue's current ETag>")
```

```
my_desk()
then watch the loop close
```

Decline what you will not do with `decline_run_control(control_id=..., ...)`,
which shows no reason, so it is checked and not sent.

Then watch it with `watch(target_kind="issue", target_id=1)`.
"""


def test_a_prompt_step_that_no_longer_runs_fails(workspace, schemas):
    """The desk-loop run fails on a step the agent could not take: a write the
    server refuses (a renewal without the generation), a fenced line that is
    not a call, and a tool the prompt's profile does not carry. A call that
    elides a required argument is checked and not sent, so it adds nothing."""
    ws = workspace("demo")
    ws.issue_id = ws.facts["seeded"]["ids"]["command_issue"]  # Sol holds it
    tables = dataclasses.replace(
        TABLES, runs_writes=TABLES.runs_writes | {"SYNTHETIC_LOOP.md"}
    )
    report = harness.run_document(
        "SYNTHETIC_LOOP.md", SYNTHETIC_LOOP, ws, tables, schemas
    )
    failures = "\n".join(report.failures)
    assert "claim_issue(issue_id=<id>" in failures
    assert "lease_generation_required" in failures
    assert "a ``` block whose lines are not each one tool call" in failures
    assert "watch is not in the document's `seat` profile" in failures
    assert len(report.failures) == 3, failures
    assert any("decline_run_control" in call for call in report.calls_validated)
    assert any("my_desk()" in call for call in report.calls_run)
    # Without runs_writes the same claim is only checked, never sent.
    checked = harness.run_document(
        "SYNTHETIC_LOOP.md", SYNTHETIC_LOOP, ws, TABLES, schemas
    )
    assert not any("lease_generation_required" in f for f in checked.failures)


# --- the harness fails on what it is for -----------------------------------------------

SYNTHETIC = """# A synthetic document with three mistakes

```http
GET /desks
Authorization: Bearer <token>
```

```http
GET /desk
Authorization: Bearer <token>
```

```json
{"schema":"athena.agent_desk.v2","no_such_field":1}
```

HTTP 201.

MCP `claim_issue(issue_ident, if_match)` · REST `POST /issues/{issue_id}/claim`

MCP `watch("space", <id>)` for a seat.

```yaml
key: an unclassified block
```

```bash
rm -rf ~/athena-data
```
"""


def test_the_harness_fails_a_wrong_route_status_argument_and_block(workspace, schemas):
    """A synthetic document with one mistake of each kind fails on each."""
    report = _run("SYNTHETIC.md", SYNTHETIC, workspace, schemas)
    failures = "\n".join(report.failures)
    # A wrong route: 404 where the document implies success.
    assert "GET /desks: status 404, the document implies success" in failures
    # A wrong status: the desk answers 200 where the document shows 201.
    assert "GET /desk: status 200, the document shows 201" in failures
    # A wrong argument name, and the required one it hides.
    assert "argument 'issue_ident' is not in the tool's input schema" in failures
    assert "required arguments not shown: ['issue_id']" in failures
    # A response field the real response does not have.
    assert "the response lacks: ['no_such_field']" in failures
    # A tool the seat profile does not carry, with no profile named.
    assert "watch is not in the document's `seat` profile" in failures
    # A block no rule classifies, and a shell command no table names.
    assert "a ```yaml block no rule classifies" in failures
    assert "unknown shell command 'rm'" in failures
    assert "a shell block with no request" in failures
    assert len(report.failures) == 9, failures


def test_a_stale_table_entry_fails(workspace, schemas):
    report = _run("QUICKSTART.md", "# nothing here\n", workspace, schemas)
    assert any("matches no block of QUICKSTART.md" in f for f in report.failures)


# --- counts and sizes the documents state ----------------------------------------------


def _read(doc: str) -> str:
    return (harness.DOCS / doc).read_text(encoding="utf-8")


SEAT = mcp_server.PROFILES["seat"] or frozenset()
KNOWLEDGE = mcp_server.PROFILES["seat+knowledge"] or frozenset()
CONDUCTOR = mcp_server.PROFILES["conductor"] or frozenset()

# (document, pattern with one number group, the value in the code). Every pattern
# must match at least once and every match must equal the code's value.
STATED_NUMBERS: list[tuple[str, str, int]] = [
    ("AGENT_BOOT.md", r"(\d+) tools for a\s+normal seat", len(SEAT)),
    ("AGENT_BOOT.md", r"seat, (\d+) with knowledge", len(KNOWLEDGE)),
    ("AGENT_BOOT.md", r"or (\d+) for a\s+conductor", len(CONDUCTOR)),
    ("RUNTIME_RECIPE.md", r"\((\d+) tools for\s+this token class", len(KNOWLEDGE)),
    ("RUNTIME_RECIPE.md", r"`seat` otherwise \((\d+)\s+tools", len(SEAT)),
    ("RUNTIME_RECIPE.md", r"the\s+(\d+)-tool `conductor` profile", len(CONDUCTOR)),
    (
        "QUICKSTART.md",
        r"the (\d+) tools\s+of the `seat\+knowledge` profile",
        len(KNOWLEDGE),
    ),
    (
        "AGENT_GUIDE.md",
        r"budget of ([\d,]+) characters",
        mcp_server.DEFAULT_RESPONSE_BUDGET,
    ),
    (
        "AGENT_GUIDE.md",
        r"`get_issue_work_context` ([\d,]+)\)",
        mcp_server.RESPONSE_BUDGET_EXCEPTIONS["get_issue_work_context"],
    ),
    ("AGENT_GUIDE.md", r"lasts (\d+) seconds by default", leases.DEFAULT_LEASE_SECONDS),
    ("AGENT_GUIDE.md", r"between (\d+) and \d+", leases.MIN_LEASE_SECONDS),
    ("AGENT_GUIDE.md", r"between \d+ and (\d+)", leases.MAX_LEASE_SECONDS),
    (
        "CLOSEOUT_RECIPE.md",
        r"Up to (\d+) results",
        lease_commands.MAX_COMPLETION_RESULTS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"each accept up to (\d+) nonblank strings",
        lease_commands.MAX_COMPLETION_TEXT_ITEMS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"strings of at most (\d+) characters",
        lease_commands.MAX_COMPLETION_TEXT_CHARS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"Evidence accepts up to (\d+) objects",
        lease_commands.MAX_HANDOFF_EVIDENCE_ITEMS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"The complete compact JSON report is limited to (\d+) characters",
        lease_commands.MAX_COMPLETION_REPORT_JSON_CHARS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"and revision\), at most (\d+) characters",
        lease_commands.MAX_COMPLETION_SOURCE_CHARS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"`model`: 1–(\d+) printable",
        reported_usage.MAX_MODEL_CHARS,
    ),
    (
        "CLOSEOUT_RECIPE.md",
        r"with a UTC offset or `Z`, at most (\d+) characters",
        reported_usage.MAX_REPORTED_AT_CHARS,
    ),
]


@pytest.mark.parametrize(
    ("doc", "pattern", "value"), STATED_NUMBERS, ids=lambda v: str(v)[:40]
)
def test_a_stated_count_or_size_is_the_codes(doc, pattern, value):
    found = [int(m.replace(",", "")) for m in re.findall(pattern, _read(doc))]
    assert found, f"{doc} no longer states {pattern!r}; update this table"
    assert set(found) == {value}, (doc, pattern, found, value)


def test_claim_next_ready_bounds_are_the_ones_the_documents_state(schemas):
    limit = schemas["claim_next_ready"]["properties"]["limit"]
    assert (limit["minimum"], limit["maximum"]) == (1, 5)
    assert "The bound 1–5 controls candidate attempts" in _read("AGENT_GUIDE.md")
    assert "`limit` is 1–5 candidates" in _read("RUNTIME_RECIPE.md")
    brief = schemas["get_project_brief"]["properties"]["limit"]
    assert (brief["minimum"], brief["maximum"]) == (1, project_brief.MAX_LIMIT)
    assert f"per-section limit is 1..{project_brief.MAX_LIMIT}" in _read(
        "RUNTIME_RECIPE.md"
    )


def _row(table_text: str, profile: str) -> set[str]:
    row = next(
        line for line in table_text.splitlines() if line.startswith(f"| `{profile}` |")
    )
    names = set(re.findall(r"`([a-z_]+)`", row.split("|")[2]))
    return names & set(mcp_server.TOOL_SCOPES)  # "`seat` plus ..." names a profile


def test_the_profile_table_lists_each_profile_exactly():
    text = _read("AGENT_BOOT.md")
    seat_row = _row(text, "seat")
    extra = re.search(r"The seat\s+profile also includes `(\w+)` and `(\w+)`", text)
    assert extra, "AGENT_BOOT.md no longer names the two tools the table omits"
    assert seat_row | set(extra.groups()) == SEAT
    assert _row(text, "seat+knowledge") == KNOWLEDGE - SEAT
    assert _row(text, "conductor") == CONDUCTOR - SEAT
