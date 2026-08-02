# Draft release notes — v0.1.0a1

Prepared for issue #324 section B. **Not published.** Tagging and publishing are
the release owner's acts; this is the text to paste, and the checks that back it.

The first line states the supported shape, per the checklist's own instruction.

---

## Athena v0.1.0a1 — local/tailnet alpha

**Supported shape: one process, one SQLite file, loopback or tailnet, Python
3.12 only.** Public-internet and proxy-terminated deployment are unsupported and
undetectable from inside the process — see
[`RELEASE_READINESS.md`](docs/RELEASE_READINESS.md) before exposing this
anywhere.

Athena is mission control for a one-person AI fleet: a self-hosted workspace
where one operator directs agents, gives them durable context, watches the work,
intervenes, and can reconstruct what happened afterwards. Markdown docs, issue
tracking, cross-links, and an append-only activity trail in one app.

### For the agents

- **The desk** — `GET /desk`, MCP `my_desk()`. One bounded read answers who you
  are, what is asked of you, what you hold, and what changed since you last
  looked. A durable per-reader cursor makes "since I last looked" a real
  question; it moves forward only, and records no activity event.
- **Playbooks** — a page labelled `playbook` turns its checklist into one parent
  issue plus a child per unchecked step, each citing the page. Docs start work;
  embeds already showed work in docs; learnings already wrote back to docs.
- **Workspace search** — `GET /search/workspace` answers across issues, pages,
  and comments in one call, and the work query grammar passes through
  (`is:open label:infra zebra` filters the work *and* finds the page). Grouped by
  kind, never globally ranked, and every group says when its bound cut the list.
- **Space subscriptions** — watch a space and hear every page event inside it, so
  a fleet can share memory without polling a page tree.
- **The Field Guide** — `athena-field-guide <db>` seeds nine pages addressed to
  agents, as ordinary pages in an ordinary space.

### For the operator

- **A trail that can prove itself** — every activity row is hash-chained in the
  same transaction that records it. Bounded, resumable verification over REST,
  MCP, `athena-doctor`, and the security page.
- **Answerability** — per agent, every recorded ask beside its recorded answer.
  Facts per lane, deliberately never a score.
- **Run controls** — steer, request cancel, or request a fresh-context handoff
  against one live run; the agent acknowledges and settles, and an unanswered
  control reads as expired.
- **Two people can edit one page safely** — a browser save carries the page's
  ETag, and the second save is refused rather than silently winning. Nothing is
  overwritten, nothing is merged, and the loser's text is kept as their draft.

### What this release does not claim

- The chain makes tampering **evident, not impossible**.
- A worker kill is **cooperative**: Athena records an instruction and cannot end
  a process. A silent worker is stale, never terminated.
- Every dispatch outcome is **the executor's claim**; Athena never verifies that
  work happened.
- No production deployment has ever occurred. All evidence is synthetic
  databases and loopback processes.

### Install

```bash
git clone https://github.com/Ayyitskevin/athena.git && cd athena
python3.12 -m venv .venv
.venv/bin/python -m pip install -c constraints/ci-py312.txt -e ".[dev,mcp]"
.venv/bin/athena-demo --db /tmp/athena-review.db     # look at it
```

For the instance you keep, follow [`docs/QUICKSTART.md`](docs/QUICKSTART.md)
path B, then seed the field guide.

Full changes: [`CHANGELOG.md`](CHANGELOG.md).

---

## Backing evidence at the commit this would tag

| Check | Result |
|---|---|
| Full coverage-gated suite | 3,319 passed; line 92.97 / branch 83.46 / combined 90.82; excluded lines exactly 2 |
| `ruff check` / `ruff format --check` / `mypy` (171 modules) | clean |
| `check_import_contracts` / `check_write_ownership` / `check_imported_at_guards` | passed |
| `scripts/smoke_app.py` | passed |
| Composed real-HTTP ecosystem proof | passed, 13 steps, chain verified |
| Hosted CI on `main` | **verify at the exact commit before tagging** — this is checklist item B1 and it is the one that must be re-checked at tag time, not inherited from this document |

---

## Draft risk acceptance — for the release owner to post, in their own words

Issue #324 section C requires the acceptance to be **a comment on that issue, so
the acceptance is itself on a trail**. That signature is the release owner's;
nobody else can give it, and an acceptance signed by an agent would be a forged
governance record. The text below exists only so the owner is not writing it
from scratch — edit it, or discard it and write your own.

> Accepting the residual risks in `RELEASE_READINESS.md` for **v0.1.0a1, scoped
> to the local/tailnet alpha this project claims to be** — not a public
> production release, which stays on HOLD.
>
> Specifically, I accept, having read each one:
>
> 1. **Supply chain** — constraints are version-pinned but not hash-locked;
>    SBOMs and checksums are unsigned assertions; there is no provenance
>    attestation; and the `anomalyco/opencode` composite action pipes an
>    unpinned remote installer into Bash.
> 2. **Deployment shape** — Athena cannot detect that a proxy, tunnel, NAT rule,
>    container publication, or Tailscale Funnel has exposed an otherwise-allowed
>    listener. Public exposure stays unsupported *and undetectable from inside*.
> 3. **Unauthenticated signed-inbound route** — `POST /forge/{source_name}`
>    accepts stranger-controlled bytes, has deliberately no replay window, and
>    stores event-source secrets in plaintext because HMAC needs the shared
>    value.
> 4. **Authorization still in some transports** — mentor page and page-comment,
>    issue-comment, and event-source commands take a bare actor id and trust the
>    route's guards (tracked in `COMMAND_MIGRATION.md`).
> 5. **One executor implementation** — the dispatch contract has exactly one
>    counterparty, written alongside it.
> 6. **Attachment recovery detects but does not repair** divergence; recovery
>    needs a matched database + directory snapshot.
> 7. **No production deployment has ever occurred** — all evidence is synthetic
>    databases and loopback processes.
>
> I am accepting these as stated rather than waiving them: the alpha's public
> claims already match this list, and any of it changing is a new evidence run,
> not an amendment to this comment.

**Two risks added since #324 was written**, from the Final Sprint's own review —
include them or not, but do not let them go unrecorded:

> 8. **The issue edit form has no If-Match.** Two people editing one issue body
>    in browsers can still overwrite each other. Page editing was fixed; issues
>    were not.
> 9. **A deleted page's notification renders only for an admin.** The access
>    model proves a page event's visibility by looking the page up, and a purged
>    row cannot prove it, so the gate fails closed for everyone else.
