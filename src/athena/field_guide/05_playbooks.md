A page can start work. Label a page `playbook`, write its steps as a markdown
checklist, and instantiate it. Over MCP that is the full profile's tool; no seat
profile carries it, so a seat with a profile asks its operator or uses REST:

```
start_playbook(page_id, project_id=None, title=None)
POST /pages/{page_id}/start-playbook
```

You get one parent issue and one child per **unchecked** step — and
**indentation is structure**: an indented step nests under the issue its
enclosing step became, so a checklist with sub-steps instantiates as the same
issue tree you would build by hand.

```markdown
- [ ] Freeze the release branch
  - [ ] Announce the freeze window
- [ ] Rehearse the migration on a copy
- [x] Tell the operator it is starting
```

The fenced checklist above is an example. To run it, copy the four checklist
lines into a labeled playbook outside the fence. Starting that page creates
a parent issue and three steps. “Announce the freeze window” nests under
“Freeze the release branch”. The ticked step contributes one to
`checked_skipped`. A ticked step's unchecked sub-steps are still real work: they attach
to the nearest ancestor that became work, top level if none did.

## A template is not a live mirror

Instantiating **snapshots** the page. Editing the playbook afterwards changes
nothing that already exists: no reconciliation, no sync-back, no drift warnings.
Running it again makes a **second, independent** set of issues.

If you meant to fix a mistake in work already started, fix the issues. If you
meant to improve the playbook for next time, edit the page. Athena will not
pretend those are the same act.

## Why this costs no new machinery

Every issue it creates cites the page with an ordinary `[[page:N]]` wikilink. So
the existing indexer builds the backlinks, the page shows the work it started,
and a `rollup` embed on that same page counts the children's progress — and
nothing in the playbook command knows what a link, a backlink, or an embed is.

Retrying is safe: pass the same `Idempotency-Key` and you get the same parent
back, not a second set.

There is a worked example in this space:
[[Example playbook: shipping a change]]. It carries the label, so you can run it
and watch the loop close.

Deeper: `docs/PLAYBOOKS.md`, `docs/EMBEDS.md`.
