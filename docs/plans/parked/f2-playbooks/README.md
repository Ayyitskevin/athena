# F-2 Playbooks — built, tested, and PARKED on an architecture decision

The Stage F-2 implementation is complete and was passing **17/17 tests**
(including the end-to-end loop proof: page → issues → backlinks → rollup embed)
when it hit a CI-enforced architectural rule it cannot legally satisfy. Nothing
here is broken; it simply has no legal home under the current module contract.

## The blocker

`scripts/check_import_contracts.py`:

```python
LAYERS = (("web",), ("aegis", "mentor"), ("core",))
# "Containers sharing one entry are independent peers, not mutually importable siblings."
```

A playbook command **must read Mentor** (the page, its label, its body) and
**must write Aegis** (`issue_commands.create_issue` / `set_issue_parent`, so the
writes keep their audit events, budget metering, and authorization). That is a
`mentor → aegis` edge, which the contract forbids — and `aegis → mentor` is
forbidden equally, so moving it does not help. Only `web/` may import both, and
the cardinal rule bars `web/` from owning logic or authorization.

AGENTS.md: *"Don't refactor a neighbor's module to make your change fit — flag
the friction instead."* The sprint guide, rule 10: *"When scope grows, stop and
flag."* Hence this parking bay rather than an unreviewed change to a contract
the build enforces.

## What is here

| File | What it is |
|---|---|
| `playbook_commands.py.txt` | the command module, complete (parser, bounds, refusal kinds, one-transaction instantiation) |
| `test_playbooks.py.txt` | 17 tests, all passing before parking |
| `surfaces.patch.txt` | the REST route (`POST /pages/{id}/start-playbook`), MCP client method, and MCP tool as a diff |

Restoring is a file move plus one `git apply` once the placement is decided.

## The three options for the owner

**A. Add a composition layer** (recommended). Extend the contract to
`(("web",), ("workflows",), ("aegis", "mentor"), ("core",))` and put
cross-module commands in `src/athena/workflows/`. Names the concept honestly —
"a command that composes both modules" — keeps every existing rule intact
(workflows may import both modules; neither module may import workflows), and
gives F-4 (workspace search) and any future cross-module feature the same home.
Cost: one new area in `AGENTS.md`'s ownership table and in the checker.

**B. Dependency inversion.** Keep the module in `mentor/` and have it accept
`create_issue` / `set_parent` callables injected by the transport. The static
import graph stays legal — but the real dependency is now invisible to the very
checker that exists to surface it. This is smuggling, and it is recorded here
only so the option is on the table rather than silently dismissed.

**C. Drop F-2.** The loop stays two-directional (embeds show work; learnings
write back) and docs never start work. This is a real product decision, not a
technical one — it removes the flagship tie-together of the sprint.

My recommendation is **A**, as the smallest change that keeps every existing
invariant true and unblocks the rest of the sprint. It needs the owner's sign-off
because it edits a contract the build enforces.
