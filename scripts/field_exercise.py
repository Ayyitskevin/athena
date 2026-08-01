"""Drive the whole operator loop against a real Athena process, over real HTTP.

`scripts/smoke_app.py` proves the process boots and serves. This goes the rest
of the way: two real processes — Athena and the reference Icarus executor from
`examples/icarus_executor.py` — talking over loopback sockets with real HMAC
signatures, exercising the loop the product documentation promises:

    onboard an agent  →  delegate work  →  the agent claims it under a run,
    heartbeats, and works  →  a gated dispatch is refused, approved, retried,
    delivered, and completed by the executor's signed callbacks  →  the agent
    promotes what it learned into the issue's runbook, which the work-context
    packet then serves  →  the operator undoes the close  →  every step is on
    the activity trail.

Nothing here is mocked, monkeypatched, or reached in-process. If a contract
only works because a test stubbed the transport, this script is where that
breaks — it exists because the dispatch loop shipped without ever having been
spoken by a real counterparty, and the first attempt to do so surfaced that the
SSRF guard (correctly, by its own rules) refused loopback delivery entirely,
which became `ATHENA_EGRESS_PRIVATE_HOSTS`.

Run it from a checkout with the venv installed:

    .venv/bin/python scripts/field_exercise.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

SECRET = "field-exercise-shared-secret"
BOOTSTRAP_TOKEN = "field-exercise-bootstrap-token-0001"
STARTUP_TIMEOUT_SECONDS = 20
DISPATCH_SETTLE_TIMEOUT_SECONDS = 15
AGENT_RUN_ID = "field-exercise-run-1"

# Loopback traffic must never detour through a proxy the environment happens to
# configure — same choice smoke_app.py makes.
_OPENER = build_opener(ProxyHandler({}))

_steps_passed = 0


def step(name: str, detail: str = "") -> None:
    global _steps_passed
    _steps_passed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  ok {(_steps_passed):2d}. {name}{suffix}", flush=True)


class Failure(RuntimeError):
    pass


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    expect: tuple[int, ...] = (200,),
) -> tuple[int, dict | list | None, dict]:
    data = None
    all_headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        all_headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=all_headers, method=method)
    try:
        with _OPENER.open(request, timeout=10) as response:  # noqa: S310 - loopback
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers)
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
        response_headers = dict(exc.headers)
    if status not in expect:
        raise Failure(
            f"{method} {url} answered {status}, wanted {expect}: {raw[:300]!r}"
        )
    parsed = json.loads(raw) if raw else None
    return status, parsed, response_headers


def _wait_for(predicate, *, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result is not None:
                return result
        except (OSError, URLError, Failure) as exc:
            last = exc
        time.sleep(0.2)
    raise Failure(
        f"timed out waiting for {what}" + (f" (last: {last})" if last else "")
    )


def _spawn(
    cmd: list[str], *, env: dict, log_path: Path, pass_fds=()
) -> subprocess.Popen:
    log = log_path.open("w", encoding="utf-8")
    try:
        return subprocess.Popen(
            cmd,
            env=env,
            pass_fds=pass_fds,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    finally:
        log.close()


def _stop(process: subprocess.Popen, name: str) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        print(f"  !! {name} needed a forced kill on teardown", flush=True)


def main() -> int:  # noqa: PLR0915 - a transcript reads top to bottom on purpose
    repo = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory(prefix="athena-field-exercise-") as temp:
        root = Path(temp)

        # Athena's port is chosen first (bound socket handed to uvicorn), so the
        # executor can be told its callback target before either process starts.
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        athena_port = listener.getsockname()[1]
        athena = f"http://127.0.0.1:{athena_port}"

        executor_log = root / "executor.log"
        executor = _spawn(
            [sys.executable, str(repo / "examples" / "icarus_executor.py")],
            env={
                **os.environ,
                "ATHENA_URL": athena,
                "EXECUTOR_SECRET": SECRET,
                "EXECUTOR_PORT": "0",
                "PYTHONUNBUFFERED": "1",
            },
            log_path=executor_log,
        )

        athena_log = root / "athena.log"
        server: subprocess.Popen | None = None
        try:
            executor_port = _wait_for(
                lambda: (
                    (
                        (
                            match := re.search(
                                r"on http://127\.0\.0\.1:(\d+)",
                                executor_log.read_text(encoding="utf-8"),
                            )
                        )
                        and int(match.group(1))
                    )
                    or None
                ),
                timeout=STARTUP_TIMEOUT_SECONDS,
                what="the reference executor to announce its port",
            )
            step("reference executor is listening", f"port {executor_port}")

            server = _spawn(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "athena.main:app",
                    "--fd",
                    str(listener.fileno()),
                    "--log-level",
                    "warning",
                    "--no-proxy-headers",
                    "--no-server-header",
                ],
                env={
                    **os.environ,
                    "ATHENA_ALLOWED_AUTHORITIES": f"127.0.0.1:{athena_port}",
                    "ATHENA_DB": str(root / "athena.db"),
                    "ATHENA_ATTACH_DIR": str(root / "attachments"),
                    "ATHENA_AUTOMATION": "0",
                    "ATHENA_BOOTSTRAP_TOKEN": BOOTSTRAP_TOKEN,
                    "ATHENA_WEBHOOK_DELIVERY": "0",
                    "ATHENA_LOG_LEVEL": "WARNING",
                    "ATHENA_TRUST_ACTOR_HEADER": "1",
                    "ATHENA_ICARUS_URL": f"http://127.0.0.1:{executor_port}",
                    "ATHENA_ICARUS_SECRET": SECRET,
                    # The finding this stage exists to record: without naming the
                    # loopback host, every local dispatch is `undeliverable`.
                    "ATHENA_EGRESS_PRIVATE_HOSTS": "127.0.0.1",
                    "PYTHONUNBUFFERED": "1",
                },
                log_path=athena_log,
                pass_fds=(listener.fileno(),),
            )
            listener.close()

            _wait_for(
                lambda: (
                    _request("GET", f"{athena}/healthz")[1] == {"status": "ok"} or None
                ),
                timeout=STARTUP_TIMEOUT_SECONDS,
                what="Athena to answer /healthz",
            )
            step("Athena is up", athena)

            # --- Onboard -----------------------------------------------------
            _, admin, _ = _request(
                "POST",
                f"{athena}/users",
                headers={"X-Athena-Bootstrap-Token": BOOTSTRAP_TOKEN},
                body={"email": "op@example.com", "name": "Operator", "password": "pw"},
                expect=(201,),
            )
            assert admin is not None
            admin_h = {"X-Athena-Actor": str(admin["id"])}
            step("operator bootstrapped as admin", f"user #{admin['id']}")

            _, onboarded, _ = _request(
                "POST",
                f"{athena}/users/onboard_agent",
                headers=admin_h,
                body={
                    "email": "sol@example.com",
                    "name": "Sol",
                    "scopes": ["read", "issue:write", "docs:write"],
                },
                expect=(201,),
            )
            assert onboarded is not None
            agent_id = onboarded["user"]["id"]
            agent_h = {"Authorization": f"Bearer {onboarded['token']['token']}"}
            agent_run_h = {**agent_h, "X-Athena-Run": AGENT_RUN_ID}
            step("agent onboarded with a scoped token", f"user #{agent_id}")

            _, me, _ = _request("GET", f"{athena}/users/me", headers=agent_h)
            assert me is not None
            if me["approval_required"] != []:
                raise Failure(f"fresh agent should be ungated: {me}")
            step("agent discovered its boundaries via whoami", str(me["scopes"]))

            # --- Delegate ----------------------------------------------------
            _, project, _ = _request(
                "POST",
                f"{athena}/projects",
                headers=admin_h,
                body={"name": "Field Exercise", "key": "FX"},
                expect=(201,),
            )
            assert project is not None
            _, issue, _ = _request(
                "POST",
                f"{athena}/issues",
                headers=admin_h,
                body={
                    "title": "Prove the loop composes",
                    "body": "Driven by scripts/field_exercise.py",
                    "project_id": project["id"],
                },
                expect=(201,),
            )
            assert issue is not None
            _request(
                "POST",
                f"{athena}/issues/{issue['id']}/delegate",
                headers=admin_h,
                body={"user_id": agent_id},
                expect=(201,),
            )
            step("issue created and delegated to the agent", f"issue #{issue['id']}")

            _, inbox, _ = _request("GET", f"{athena}/delegations/me", headers=agent_h)
            assert inbox is not None
            if issue["id"] not in [i["issue"]["id"] for i in inbox["items"]]:
                raise Failure(f"delegated issue missing from the inbox: {inbox}")
            step("agent sees the work in its delegation inbox")

            # --- Work, under a run ------------------------------------------
            _, _, issue_headers = _request(
                "GET", f"{athena}/issues/{issue['id']}", headers=agent_h
            )
            # Header names are case-insensitive on the wire; uvicorn lowercases.
            etag = {k.lower(): v for k, v in issue_headers.items()}.get("etag")
            if not etag:
                raise Failure("issue read returned no ETag to claim against")
            _request(
                "POST",
                f"{athena}/issues/{issue['id']}/claim",
                headers={**agent_run_h, "If-Match": etag},
                body={},
                expect=(201,),
            )
            step("agent claimed the issue against the reviewed ETag")

            _request(
                "PUT",
                f"{athena}/agent-runs/heartbeat",
                headers=agent_h,
                body={"run_id": AGENT_RUN_ID},
            )
            step("agent heartbeated its run", AGENT_RUN_ID)

            _request(
                "PATCH",
                f"{athena}/issues/{issue['id']}",
                headers=agent_run_h,
                body={"status": "in_progress"},
            )
            step("agent moved the issue to in_progress under its run")

            # --- A gated dispatch, approved and delivered -------------------
            _request(
                "PUT",
                f"{athena}/approvals/policies/{agent_id}",
                headers=admin_h,
                body={"action_kind": "dispatch.request"},
            )
            dispatch_body = {
                "repo": "git@example.com:acme/app.git",
                "base_commit": "f1e2d3c",
                "capability": "repo.edit",
            }
            status, refused, _ = _request(
                "POST",
                f"{athena}/issues/{issue['id']}/dispatch",
                headers=agent_run_h,
                body=dispatch_body,
                expect=(202,),
            )
            assert refused is not None
            if refused.get("code") != "approval_required":
                raise Failure(f"gated dispatch should ask, got: {refused}")
            step("gated dispatch was refused with a recorded ask")

            _, pending, _ = _request(
                "GET", f"{athena}/approvals?state=pending", headers=admin_h
            )
            assert pending
            if pending[0]["action_kind"] != "dispatch.request":
                raise Failure(f"the ask names the wrong intent: {pending[0]}")
            _request(
                "POST",
                f"{athena}/approvals/{pending[0]['id']}/decision",
                headers=admin_h,
                body={"decision": "approve"},
            )
            step("operator approved the dispatch ask", f"#{pending[0]['id']}")

            _, dispatched, _ = _request(
                "POST",
                f"{athena}/issues/{issue['id']}/dispatch",
                headers=agent_run_h,
                body=dispatch_body,
                expect=(201,),
            )
            assert dispatched is not None
            if dispatched["approval_state"] != "approved":
                raise Failure(f"retry should consume the approval: {dispatched}")
            if dispatched["state"] != "accepted":
                raise Failure(
                    "the executor did not accept over the wire: "
                    f"{dispatched['state']} ({dispatched.get('last_error')})"
                )
            step(
                "approved retry was delivered and accepted by the real executor",
                f"executor run {dispatched['icarus_run_id']}",
            )

            settled = _wait_for(
                lambda: (
                    (
                        (
                            record := _request(
                                "GET",
                                f"{athena}/dispatches/{dispatched['id']}",
                                headers=admin_h,
                            )[1]
                        )
                        and record["state"] == "completed"
                        and record
                    )
                    or None
                ),
                timeout=DISPATCH_SETTLE_TIMEOUT_SECONDS,
                what="the executor's signed callbacks to settle the dispatch",
            )
            if not settled["evidence_ref"] or not settled["completion_ref"]:
                raise Failure(f"callbacks landed without refs: {settled}")
            step(
                "executor's signed callbacks recorded evidence and completion",
                settled["completion_ref"],
            )

            _, mismatches, _ = _request(
                "GET",
                f"{athena}/activity?verb=dispatch_policy_digest_mismatch",
                headers=admin_h,
            )
            if mismatches:
                raise Failure(f"digest mismatch recorded unexpectedly: {mismatches}")
            step("policy digest matched end to end")

            # --- Learn -------------------------------------------------------
            _, space, _ = _request(
                "POST",
                f"{athena}/spaces",
                headers=admin_h,
                body={"key": "FX", "name": "Field Notes"},
                expect=(201,),
            )
            assert space is not None
            _, learning, _ = _request(
                "POST",
                f"{athena}/issues/{issue['id']}/learnings",
                headers=agent_run_h,
                body={
                    "summary": "The whole loop composes over real sockets.",
                    "run_id": AGENT_RUN_ID,
                    "space_id": space["id"],
                },
                expect=(201,),
            )
            assert learning is not None
            step("agent promoted a learning into the issue's runbook")

            # --- the operator reads the plan ---------------------------------
            # Q added a roadmap step to the loop: after work is placed, the
            # operator should be able to SEE where it sits and how far along a
            # parent is, without opening a tracker. Exercised over real HTTP
            # because a view that only works in a unit test is not a view.
            _, plan, _ = _request(
                "GET",
                f"{athena}/projects/{project['id']}/timeline",
                headers=admin_h,
            )
            assert plan is not None
            if issue["id"] not in [card["id"] for card in plan["cards"]]:
                raise Failure(f"the delegated issue is missing from the plan: {plan}")
            if plan["shown"] > plan["total"]:
                raise Failure(f"timeline reported more drawn than exist: {plan}")
            step(
                "operator read the project timeline",
                f"{plan['shown']} of {plan['total']} across {len(plan['lanes'])} lanes",
            )

            _, child, _ = _request(
                "POST",
                f"{athena}/issues",
                headers=admin_h,
                body={
                    "title": "Sub-task of the field exercise",
                    "project_id": project["id"],
                },
                expect=(201,),
            )
            assert child is not None
            _request(
                "PUT",
                f"{athena}/issues/{child['id']}/parent",
                headers=admin_h,
                body={"parent_id": issue["id"]},
            )
            _, rolled, _ = _request(
                "POST",
                f"{athena}/embeds/resolve",
                headers=admin_h,
                body={"text": (f"```athena\nkind: rollup\nissue: {issue['id']}\n```")},
            )
            assert isinstance(rolled, list)
            rollup = rolled[0]
            if rollup.get("error") or rollup["rollup"]["total"] != 1:
                raise Failure(f"rollup embed did not count the sub-issue: {rollup}")
            step(
                "rollup embed counted the sub-issue live",
                f"{rollup['rollup']['percent_done']}% done",
            )

            _, context, _ = _request(
                "GET",
                f"{athena}/issues/{issue['id']}/work-context",
                headers=agent_h,
            )
            assert context is not None
            backlinks = context["references"]["backlinks"]["items"]
            if learning["page_id"] not in [
                b["id"] for b in backlinks if b["kind"] == "page"
            ]:
                raise Failure(
                    f"runbook page {learning['page_id']} missing from context "
                    f"backlinks: {backlinks}"
                )
            step("the next agent's work-context packet serves the runbook")

            # --- the runbook shows the issue's LIVE state --------------------
            # The payoff of embeds: a runbook that does not just describe work but
            # displays it. Appended through the ordinary page command, then read
            # back through the resolver an agent uses, over real HTTP.
            _, page_before, _ = _request(
                "GET", f"{athena}/pages/{learning['page_id']}", headers=agent_h
            )
            assert page_before is not None
            directive = (
                "```athena\n"
                "kind: issue\n"
                f"issue: {issue['id']}\n"
                "title: This issue, right now\n"
                "```"
            )
            _request(
                "PATCH",
                f"{athena}/pages/{learning['page_id']}",
                headers={**agent_run_h, "If-Match": page_before["_etag"]}
                if page_before.get("_etag")
                else agent_run_h,
                body={"body": f"{page_before.get('body') or ''}\n\n{directive}"},
            )
            _, resolved, _ = _request(
                "POST",
                f"{athena}/embeds/resolve",
                headers=agent_h,
                body={"text": directive},
            )
            assert isinstance(resolved, list) and resolved
            embed = resolved[0]
            if embed.get("error") is not None:
                raise Failure(f"the runbook's embed did not resolve: {embed}")
            if embed["item"]["id"] != issue["id"]:
                raise Failure(f"embed resolved the wrong issue: {embed}")
            if embed["item"]["status"] != "in_progress":
                raise Failure(
                    "the embed reports a stale status: "
                    f"{embed['item']['status']} (the issue is in_progress)"
                )
            step(
                "the runbook's embed shows the issue's live state",
                f"{embed['item']['key'] or embed['item']['id']} "
                f"= {embed['item']['status']}",
            )

            # --- the graph grows: an unlinked mention becomes an edge --------
            # A page names the issue by key in prose. Athena must SUGGEST that
            # edge and never take it on its own; the operator's click is an
            # ordinary audited edit of the mentioning page.
            _, note_space, _ = _request(
                "POST",
                f"{athena}/spaces",
                headers=admin_h,
                body={"key": "OPS", "name": "Operator notes"},
                expect=(200, 201),
            )
            assert note_space is not None
            issue_key = issue.get("key") or f"FX-{issue['id']}"
            _, mentioning, _ = _request(
                "POST",
                f"{athena}/spaces/{note_space['id']}/pages",
                headers=admin_h,
                body={
                    "title": "Shift handover",
                    "body": (
                        f"Watch {issue_key} through the night. "
                        f"Do not touch `{issue_key}` in the deploy script."
                    ),
                },
                expect=(200, 201),
            )
            assert mentioning is not None
            _, found, _ = _request(
                "GET",
                f"{athena}/issues/{issue['id']}/unlinked-mentions",
                headers=admin_h,
            )
            assert found is not None
            if found["needle"] != issue_key:
                raise Failure(f"mentions searched for the wrong name: {found}")
            if [i["id"] for i in found["items"]] != [mentioning["id"]]:
                raise Failure(f"the prose mention was not suggested: {found}")
            # The SAME page also names the issue inside an inline code span. If the
            # code rule were broken, that occurrence would be linkified and the
            # deploy script line would be corrupted.
            _request(
                "POST",
                f"{athena}/pages/{mentioning['id']}/link-mention",
                headers=admin_h,
                body={"target_kind": "issue", "target_id": issue["id"]},
            )
            _, linked_page, _ = _request(
                "GET", f"{athena}/pages/{mentioning['id']}", headers=admin_h
            )
            assert linked_page is not None
            if f"[[{issue_key}]]" not in linked_page["body"]:
                raise Failure(f"the mention was not linked: {linked_page['body']}")
            if f"`{issue_key}`" not in linked_page["body"]:
                raise Failure(f"the code span was rewritten: {linked_page['body']}")
            _, issue_backlinks, _ = _request(
                "GET", f"{athena}/issues/{issue['id']}/backlinks", headers=admin_h
            )
            assert issue_backlinks is not None
            if mentioning["id"] not in [b["id"] for b in issue_backlinks]:
                raise Failure(f"the new edge is not a backlink: {issue_backlinks}")
            _, ego, _ = _request(
                "GET", f"{athena}/issues/{issue['id']}/graph", headers=admin_h
            )
            assert ego is not None
            if mentioning["id"] not in [
                n["id"] for n in ego["nodes"] if n["kind"] == "page"
            ]:
                raise Failure(f"the new edge is not in the graph: {ego}")
            step(
                "an unlinked mention became a real edge, and code was left alone",
                f"{issue_key} \u2190 page {mentioning['id']}",
            )

            # --- the operator's daily note ----------------------------------
            _, day_one, _ = _request(
                "POST",
                f"{athena}/spaces/{note_space['id']}/daily",
                headers=admin_h,
                expect=(200, 201),
            )
            _, day_two, _ = _request(
                "POST",
                f"{athena}/spaces/{note_space['id']}/daily",
                headers=admin_h,
                expect=(200, 201),
            )
            assert day_one is not None and day_two is not None
            if day_one["id"] != day_two["id"]:
                raise Failure(
                    f"the daily note is not idempotent: {day_one['id']} "
                    f"vs {day_two['id']}"
                )
            step("today's note opens twice and is one page", day_one["title"])

            # --- the forge reports in ---------------------------------------
            # Inbound integration over real HTTP: a registered source signs a
            # synthetic push naming this issue, and Athena lands it as FOREIGN
            # history. The assertions that matter are the ones about what it is
            # NOT — not a native event, not part of any run, not undoable, and
            # not able to move the issue.
            _, forge_source, _ = _request(
                "POST",
                f"{athena}/event-sources",
                headers=admin_h,
                body={"name": "fx-forge", "host": "github.com"},
                expect=(200, 201),
            )
            assert forge_source is not None
            forge_secret = forge_source["secret"]

            issue_key = issue.get("key") or f"FX-{issue['id']}"
            push_payload = {
                "ref": f"refs/heads/{issue_key}-work",
                "commits": [
                    {
                        "message": f"{issue_key} wire the executor callback",
                        "url": "https://github.com/example/repo/commit/f1e1d",
                    }
                ],
            }
            # Sign the EXACT bytes _request will send.
            push_body = json.dumps(push_payload).encode()
            push_signature = (
                "sha256="
                + hmac.new(forge_secret.encode(), push_body, hashlib.sha256).hexdigest()
            )
            _, accepted, _ = _request(
                "POST",
                f"{athena}/forge/fx-forge",
                headers={
                    "X-Hub-Signature-256": push_signature,
                    "X-GitHub-Event": "push",
                },
                body=push_payload,
                expect=(202,),
            )
            assert accepted is not None
            if accepted["landed"] != 1 or accepted["issues"] != [issue["id"]]:
                raise Failure(f"the forge event did not land on the issue: {accepted}")

            # An unsigned replay of the same delivery must be refused.
            _request(
                "POST",
                f"{athena}/forge/fx-forge",
                headers={"X-GitHub-Event": "push"},
                body=push_payload,
                expect=(401,),
            )

            _, trail, _ = _request(
                "GET",
                f"{athena}/activity?target_kind=issue&target_id={issue['id']}",
                headers=admin_h,
            )
            assert trail is not None
            landed_event = next((e for e in trail if e["verb"] == "forge_commit"), None)
            if landed_event is None:
                raise Failure("the forge commit is not on the issue trail")
            if landed_event["imported_at"] is None:
                raise Failure(
                    f"the forge event was recorded as NATIVE history: {landed_event}"
                )
            if landed_event["run_id"] is not None:
                raise Failure(f"the forge event claimed an Athena run: {landed_event}")
            # 0041's guarantee, exercised rather than assumed.
            _request(
                "POST",
                f"{athena}/activity/{landed_event['id']}/undo",
                headers=admin_h,
                expect=(422,),
            )
            _, still, _ = _request(
                "GET", f"{athena}/issues/{issue['id']}", headers=admin_h
            )
            assert still is not None
            if still["status"] != "in_progress":
                raise Failure(f"the forge event moved the issue: {still['status']}")
            step(
                "the forge reported in; it landed as imported and moved nothing",
                f"{issue_key} \u2190 commit f1e1d",
            )

            # --- Close, and the operator's undo -----------------------------
            _request(
                "PATCH",
                f"{athena}/issues/{issue['id']}",
                headers=agent_run_h,
                body={"status": "done"},
            )
            _, closes, _ = _request(
                "GET", f"{athena}/activity?verb=changed_status", headers=admin_h
            )
            assert closes
            close_event = closes[0]
            _, undone, _ = _request(
                "POST",
                f"{athena}/activity/{close_event['id']}/undo",
                headers=admin_h,
                expect=(201,),
            )
            assert undone is not None
            _, after, _ = _request(
                "GET", f"{athena}/issues/{issue['id']}", headers=admin_h
            )
            assert after is not None
            if after["status"] != "in_progress":
                raise Failure(f"undo did not restore the prior status: {after}")
            step(
                "operator undid the close; the trail keeps both rows",
                f"reversal #{undone['reversal']['id']}",
            )

        except BaseException:
            for name, path in (("executor", executor_log), ("athena", athena_log)):
                if path.exists():
                    text = path.read_text(encoding="utf-8").strip()
                    if text:
                        print(f"--- {name} log ---\n{text}", file=sys.stderr)
            raise
        finally:
            if server is not None:
                _stop(server, "athena")
            _stop(executor, "executor")

    print(f"FIELD EXERCISE PASSED: {_steps_passed} steps over real HTTP", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
