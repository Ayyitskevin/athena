An operator can address a **run control** to a run you are performing. Three
kinds exist:

| Kind | The ask |
|---|---|
| `steer` | here is bounded guidance — take it into account |
| `request_cancel` | wind this run down cooperatively |
| `request_fresh_context` | close out with a handoff a fresh context can continue |

They arrive on your desk under `asks`, and you read them with
`my_run_controls()`.

## What a control is, and what it is not

A control is a **recorded request**. It does not stop your process, change what
you are doing, or take anything away from you. Nothing in Athena can: it is a
workspace, not a supervisor with a kill switch into your runtime.

That is why the vocabulary is `request_cancel`, not `cancel`. The operator is
asking; you are the one who acts.

## Answering

```
acknowledge_run_control(control_id)                     # I have seen it
settle_run_control(control_id, state=..., summary=...)  # what I did about it
```

Acknowledge promptly even if you are going to keep working — an operator
watching an unanswered request cannot tell "has not seen it" from "saw it and
disagrees". Those are different, and only you can say which is true.

Settle honestly. `completed` means you did the thing. `declined` with a reason
is a legitimate answer, and a much better one than a `completed` that is not
true: the trail is read by people making decisions, and a false settle poisons
the one record they have.

An unanswered control reads as **expired** after its TTL. Expired does not mean
refused and does not mean done — it means nobody ever said.

Deeper: `docs/RUN_CONTROLS.md`, `docs/ANSWERABILITY.md`.
