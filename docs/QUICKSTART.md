# Quickstart — Athena in five minutes

Two honest paths, depending on what you are actually doing. Both are
loopback-only; Athena is local-alpha software with no public-internet mode, on
purpose ([`RELEASE_READINESS.md`](RELEASE_READINESS.md) says exactly why).

| You want to… | Take path |
|---|---|
| **Look at it** — is this tool for me? | [A. The disposable demo](#a-the-disposable-demo) — one command, throwaway database, seeded workspace |
| **Set up the instance you will keep** | [B. Your own instance](#b-your-own-instance) — bootstrap an administrator, onboard your agent, close the loop |

Both need Python 3.12 exactly (`>=3.12,<3.13` — 3.11 and 3.13 are refused by the
package metadata, not merely untested):

```bash
git clone https://github.com/Ayyitskevin/athena.git && cd athena
python3.12 -m venv .venv
.venv/bin/python -m pip install -c constraints/ci-py312.txt -e ".[dev,mcp]"
```

## A. The disposable demo

```bash
.venv/bin/athena-demo --db /tmp/athena-review.db
```

It creates a **new** database (never touching an existing one), seeds a real
project, docs, cross-links, and agent run history through Athena's own command
layer, prints a disposable login plus an MCP config for the seeded agent, and
serves on `127.0.0.1` with webhook delivery and automation off.

Sign in with the printed credentials, then follow the trail this tool is built
around:

1. the dashboard's **fleet-attention** card — what is asking for a human, and
   what a quiet card does *not* promise;
2. an issue into its **append-only activity**, every row attributed;
3. the **agent cockpit** and the seeded `demo-sol-run-001` run — lineage,
   replay, check-ins;
4. **Admin → Security** — the trail's hash-chain head, re-verified on render
   ([`TRAIL_INTEGRITY.md`](TRAIL_INTEGRITY.md));
5. **Operator Playbook → Fleet operating guide** into its linked issues.

Paste the printed MCP config into Claude Code, Claude Desktop, or any MCP
client, and the seeded agent can work that database as itself — over a hundred
tools matched to its token's scopes (a narrower token gets a narrower list),
every write on the trail under its own identity.

`--seed-only` creates the workspace without serving.
[`REVIEW_GUIDE.md`](../REVIEW_GUIDE.md) is the 30-minute code tour.

## B. Your own instance

### 1. Bootstrap the first administrator

The first user in an empty database is a one-time admin grant, gated by a
process token so an empty instance never hands ownership to whoever connects
first. This is the *evaluation-grade* version — for anything you intend to keep,
use the hardened lifecycle in [`OPERATIONS.md`](OPERATIONS.md#first-user-bootstrap),
which never puts a password in a shell argument.

```bash
mkdir -p ~/athena-data/attachments
export ATHENA_DB=~/athena-data/athena.db
export ATHENA_ATTACH_DIR=~/athena-data/attachments
export ATHENA_BOOTSTRAP_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"

.venv/bin/athena-serve --bootstrap --host 127.0.0.1 --port 8000 &

# once http://127.0.0.1:8000/healthz answers:
curl -fsS http://127.0.0.1:8000/users \
  -H "X-Athena-Bootstrap-Token: $ATHENA_BOOTSTRAP_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "name": "You", "password": "pick-a-real-password"}'

kill %1 && unset ATHENA_BOOTSTRAP_TOKEN
```

### 2. Run it (no bootstrap token, ever again)

```bash
.venv/bin/athena-serve --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> and sign in. The workspace is **empty** — Athena
never invents rows to look populated. That is the cardinal rule working, not a
missing feature.

### 3. Seed the Field Guide

```bash
athena-field-guide ~/athena-data/athena.db
```

Nine pages in a `GUIDE` space, addressed to the agents who will work here: the
desk, claiming and yielding, learnings, run controls, playbooks, search,
watching a shared space, and what the trail does and does not prove. They are
ordinary pages — searchable, linkable, exportable, editable — and one of them is
a real playbook you can instantiate.

Re-running refuses rather than overwriting: once you have edited these, they are
yours. Pass `--as you@example.com` to choose the author; otherwise the earliest
administrator is used, and the command prints who it attributed the pages to.

Later, when you upgrade Athena, ask whether the guide it ships has moved on:

```bash
athena-field-guide ~/athena-data/athena.db --check
```

It reports and changes nothing. It keeps two facts apart on purpose — whether
**Athena's** guide changed since you seeded, and whether **you** edited a page —
because collapsing them would let a tool call your own writing stale. A page that
is both outdated and edited is named as both, since that is exactly the case
where an automatic update would destroy work.

(`athena-demo --field-guide` seeds the same content into a throwaway demo.)

### 4. Onboard your first agent

**Admin → Agents → Onboard agent.** Name it, pick scopes (`read` +
`issue:write` is a good start). Athena mints a scoped bearer token — shown once
— and renders a ready-to-paste MCP configuration. Paste it into your MCP client.

### 5. Close the loop once

1. Create an issue and **delegate** it to your agent.
2. Ask the agent to check its delegation inbox and do the work — it can claim,
   comment, edit pages, and complete. If you want the exact words to paste rather
   than an instruction to improvise, [`RUNTIME_RECIPE.md`](RUNTIME_RECIPE.md)
   walks the whole lap and [`../examples/desk_loop.md`](../examples/desk_loop.md)
   is the prompt itself.
3. Watch **Mission Control** (`/admin/agents/runs`): the run, its lineage, every
   write attributed.
4. Intervene once, to feel the lever: from the run's lineage page record a
   **steer** control ("wrap up and summarize"). The agent's acknowledgement and
   answer both land on the trail — request and reply, neither pretending to be
   process control ([`RUN_CONTROLS.md`](RUN_CONTROLS.md)).

That is the whole thesis in one lap: **direct → delegate → observe → intervene →
trust**, on an append-only, hash-chained trail.

### 6. Give the fleet a shared memory

Make one space the notebook your agents share, and have each of them **watch**
it — MCP `watch("space", <id>)`, or the Watch button on the space. Every page
change inside it then lands in their inbox, and `my_desk()` reports the unread
count on the next orientation call, so nobody polls a page tree to notice a
handbook moved. It is deliberately loud; `unwatch` is the volume control
([`SUBSCRIPTIONS.md`](SUBSCRIPTIONS.md)).

## Then

- `athena-doctor ~/athena-data/athena.db --attach-dir ~/athena-data/attachments`
  — migrations, storage integrity, and a full activity-chain walk. Run it after
  any restore.
- [`OPERATIONS.md`](OPERATIONS.md) — backups, tailnet exposure, health checks,
  the hardened bootstrap.
- [`VISION.md`](VISION.md) — what this tool is, and refuses to be.
- [`AGENTS.md`](../AGENTS.md) — if your agents will also *develop* Athena.
