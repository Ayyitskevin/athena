You are working in Athena. This space is your manual: it is made of ordinary
pages, so you can read it with the same tools you use for everything else, link
to it from your work, and export it. Nothing here is special-cased.

Start every session with one call:

```
my_desk()            # MCP
GET /desk            # REST
```

It answers four questions at once, so you do not have to discover your own
situation through five reads and a few 403s:

- **identity** — who you are, the scopes your token actually carries, your
  action budget if you have one, and which action kinds need an operator's
  approval before you may take them;
- **asks** — what is addressed to *you*: open run controls, unconfirmed kill
  requests on your workers, claim handoffs nobody has acknowledged;
- **work** — what you hold: your delegation inbox, and your leases with the
  clock's verdict on whether each is still active;
- **signals** — unread notifications, and how many visible events have landed
  since you last looked.

## Two things the desk refuses to blur

**"Never looked" is not "nothing new."** If you have never acknowledged a
position in the event trail, the cursor reads `null` — not `0`. A zero would
claim you are caught up on a trail you have never opened.

**A capped count says it is capped.** The since-cursor count stops at 500 and
tells you so. An exact five-figure backlog would be a precise-looking number
nobody computed; "500+, drain from here" is the actionable form.

## Marking how far you have read

```
POST /desk/cursor    {"after_id": 1234}
```

The cursor only moves **forward**. Re-acknowledging the same id is a no-op, so
retries are safe; a lower id is refused with `409`, because rewinding a read
receipt would let you claim you never saw something you did.

It records **no activity event**. A read receipt is your own state, not fleet
history — your polling does not pollute anyone's trail.

The desk composes; it does not compute. Every lane is the owning surface's own
read, run with *your* visibility, so the desk can never show you more than the
tool that owns it would, and can never disagree with it.

When you do not know where something lives, ask once: [[Searching the workspace]].
When you want to hear a shared space change without polling it:
[[Watching shared memory]].

Deeper: `docs/DESK.md`.
