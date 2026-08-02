This page carries the `playbook` label, so it can be turned into real work:

```
start_playbook(<this page's id>)
```

Try it. You will get one parent issue and one child per unchecked step below,
each citing this page — so when you come back here, the backlinks show the work
this page started.

- [ ] Read the change and write down what it is supposed to do
- [ ] Run the full test suite and record the numbers, not the vibe
- [ ] Check the change against the contract in AGENTS.md
- [ ] Prove the new surface over real HTTP, not only in tests
- [x] Confirm the branch is the one you were told to use

The last step is already ticked, so it is **counted and skipped** — you should
get four children, not five, and `checked_skipped: 1` in the response.

## What to notice

Run it twice. You get two independent sets of issues, because instantiating
snapshots this page rather than syncing with it. Then edit this page and run it
again: the issues that already exist do not change.

That is the contract, not a limitation to work around. A template that quietly
rewrote work already in flight would be worse than one that does not try.

Deeper: [[Playbooks: docs that start work]].
