A page can start work. Label a page `playbook`, write its steps as a markdown
checklist, and instantiate it:

```
start_playbook(page_id, project_id=None, title=None)
POST /pages/{page_id}/start-playbook
```

You get one parent issue and one child per **unchecked** step.

```markdown
- [ ] Freeze the release branch
- [ ] Rehearse the migration on a copy
- [x] Tell the operator it is starting
```

That page makes two issues, not three. A ticked box is the author saying the
step is already done, and creating an issue for it would be the tool arguing
with its author — so ticked steps are counted and reported back to you as
`checked_skipped`, never silently dropped and never silently created.

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
