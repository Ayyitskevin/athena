# Run learnings: closing the loop back into Mentor

`VISION.md`'s fifth step — Trust/**Learn** — promises that corrections "feed back
into Mentor as durable context the agents read next time". Mentor pages were
already read by agents (the work-context packet surfaces linked pages), but
nothing ever wrote back. Every run started from exactly the knowledge the last one
did, and whatever an agent figured out died with its session.

A **learning** is one note appended to an issue's **runbook**: a single Mentor
page, bound to that issue, holding what people and agents found out while working
on it.

## The loop closes through machinery that already existed

The appended entry references the issue as `[[issue:N]]`. That is all it takes:

1. Saving the page syncs the link index (`core/links`), inside the same
   transaction.
2. The issue's **backlinks** now include the runbook.
3. The next agent's **work-context packet** includes those backlinks.

Nobody wired step 3 to step 1. The feedback loop is the existing knowledge graph,
finally being written to from the work side.

## Three deliberate constraints

**Promotion is explicit.** Nothing is promoted automatically — not on completion,
not on yield, not on any event. `ACTIVE_WORK.md` classes handoff and yield text as
*untrusted advisory input*, and the operator decides what earns a place in the
knowledge base. There is no "auto-summarize the run" path, and adding one would
mean Athena writing a narrative it cannot verify.

**Promoted text is quoted, not merged.** The summary is stored as a blockquote
under an attribution header Athena writes:

```markdown
## Learning from run `sol-1`

Recorded by **Sol** (agent) while working on [[issue:42]].

> The flaky test was clock skew, not a race. Check the container clock first.
```

A summary containing its own `## Learning` heading therefore renders *inside* the
quote instead of forging a second attribution beside the real one. Untrusted text
must not be able to impersonate the provenance around it. Nothing in a learning is
ever executed or read as an instruction — it is somebody's report, rendered as
one, and Mentor sanitizes on render as it does for any page.

**Provenance is verified, not accepted.** The actor comes from the credential, the
timestamp from the server clock, and a named `run_id` must be a run that actually
exists and that this actor can see — the same rule `activity._validated_lineage`
applies to run ancestry. An unverifiable claim is **refused**, not recorded:
invented attribution in a knowledge base is worse than none.

## The runbook binding

Migration 0066 stores `(issue_id → page_id)` explicitly. Finding the page by title
would have worked until somebody renamed it, at which point the next promotion
would silently start a second runbook and split the memory in two — exactly the
quiet divergence a knowledge base must not have.

One runbook per issue. The page is an ordinary Mentor page: it can be edited,
moved, archived, or deleted like any other, and a promotion that finds a vanished
page starts a new runbook rather than failing.

## Surfaces

```text
POST /issues/{issue_id}/learnings   {"summary": "...", "run_id": "...", "space_id": 3}
GET  /issues/{issue_id}/runbook     # null when there is none yet
record_run_learning(issue_id, summary, run_id=None, space_id=None)
get_issue_runbook(issue_id)
```

The browser affordance lives on the **run lineage view** (`/aegis/activity/runs/
{run_id}/lineage`), listing the issues that run touched — looking at what a run
actually did is precisely the moment an operator knows what the next one should be
told. A browser promotion is attributed to the run being viewed.

`space_id` is required only for the first promotion on an issue: it says where the
runbook should live. Athena has no default space and will not invent one — refusing
is better than publishing somebody's notes into a space nobody chose.

## Authorization

- The Mentor **write scope** (`docs:write`), because this writes a page.
- **Visibility of the issue**, or the same 404 a missing issue gives.
- **Visibility of the space** when starting a runbook, and of the runbook page when
  appending — a runbook that has since moved into a private space is refused
  rather than appended to blindly.

Every promotion records a `page_learning_recorded` event atomically with the page
write, and the page write itself is the ordinary page create/edit command, so it
carries a version, an audit event, and a budget charge like any other page write.

## Limitations

- One runbook per issue; there is no project-level or space-level digest.
- Learnings are append-only in practice: editing or removing one means editing the
  page, which is an ordinary page edit with its own history.
- Nothing summarizes, deduplicates, or ranks entries. A long-running issue's
  runbook grows until a human curates it.
- The next agent reads the runbook because it appears in backlinks and the
  work-context packet — Athena does not force it to, and cannot verify that it did.
- `MAX_SUMMARY_CHARS` (8000) bounds one entry, not the page.
