# The worker registry and the cooperative kill

Athena models **who** an agent is — a user, tokens, scopes — and check-ins
([`RUNS.md`](RUNS.md)) prove a credential reported a **run** recently. Neither
answers the operator's actual question: *which of my agent processes are up, on
what box, and can I tell one to stop?*

A worker is a row that heartbeats. There is no cluster, no scheduler, and no
leader election: Athena stores what a worker reports about itself, plus one
instruction the operator may leave for it.

## Two words this feature refuses to say

**Alive.** A heartbeat proves a worker *reported* at a moment. Presence is derived
from the server clock at read time and is only ever `reporting_recently` or
`stale`. A worker that goes quiet has stopped **reporting**; whether its process
exists is something Athena cannot observe and never claims.

**Killed.** Athena cannot signal a foreign OS process. A kill is a *request* the
worker collects on its next heartbeat and answers for itself. The three facts are
stored in three separate columns because they are three separate facts:

| Column | Whose claim | Means |
|---|---|---|
| `kill_requested_at` | Athena's own | An operator asked this worker to stop |
| `kill_acknowledged_at` | the worker's | It says it received the request |
| `stopped_at` | the worker's | It says it stopped |

Collapsing those into one `killed` flag would be exactly the lie this design
exists to prevent.

## Reading `kill_state`

| State | Means |
|---|---|
| `none` | Nobody asked it to stop |
| `requested` | Asked, and it has not acknowledged |
| `acknowledged` | It heard, and has since gone quiet — **not** proof it stopped |
| `acknowledged_but_reporting` | It heard and is *still reporting*: it is not honoring the request |
| `stopped` | It said it stopped |

`acknowledged_but_reporting` is not an error Athena can resolve. It is the
operator's to act on, and hiding it would undo the point of the feature.

## The loop

```text
worker                                Athena                         operator
  |  PUT /workers/heartbeat             |                                |
  |  {worker_key, node_label, caps} --> |                                |
  |  <-- {kill_requested: false, ...}   |                                |
  |                                     | <-- POST /workers/{id}/kill    |
  |  PUT /workers/heartbeat --------->  |                                |
  |  <-- {kill_requested: TRUE}         |                                |
  |  ...worker decides to comply...     |                                |
  |  heartbeat state="stopping" ----->  |  (records the acknowledgement) |
  |  heartbeat state="stopped" ------>  |  (records the claim)           |
```

The worker learns it was asked to stop by **coming back**. Honoring the request
is the worker's job; Athena's job is to record that it was asked, and what was
said in reply.

## Guarantees

- **A worker registers only itself.** Identity comes from the bearer token and
  every timestamp from the server clock. A browser session or the trusted actor
  header cannot heartbeat — a worker is a process holding a credential, and
  letting a human session impersonate one would put fiction in the registry.
- **A human's token cannot register a worker either.** The registry describes
  agent processes; the command requires an agent account.
- **Credentials are re-resolved inside the write transaction**, so a revoked
  token or an account no longer marked as an agent stops reporting immediately.
- **First registration is audited; refreshes are not.** A new node appearing is a
  fact worth keeping (`worker_registered`); a heartbeat every few seconds is
  operational state, and recording each one would drown the log it informs.
  Kill requests, cancellations, acknowledgements, and stops are all audited.
- **A restart does not cancel an instruction.** A worker that reports running
  after claiming it stopped clears only its own stale claim — the operator's
  request stands, and the result reads as `acknowledged_but_reporting`.
- **Asking twice does not reset the clock.** `kill_requested_at` stays put,
  because how long a worker has been ignoring you is the number you are watching.
- **A request can be withdrawn until it is acknowledged.** After that it cannot:
  the worker may already be shutting down, so "never mind" would be a claim about
  the world Athena cannot make.
- **Workers are per-agent.** Two agents may both call a worker `worker-1` without
  either reaching the other's row. An agent sees only its own workers; an admin
  sees the fleet, and only an admin may leave an instruction.
- **Worker events are admin-only on the trail.** Node labels and kill instructions
  are operator infrastructure, and the event visibility clause is a closed
  whitelist, so an unlisted target kind is admin-only by default.

## Pause interacts with kill, and the order matters

Pause freezes the credential: every authenticated action is refused. A **paused
worker cannot heartbeat, so it cannot learn it was asked to stop.** The request
sits unanswered until the account is resumed, at which point the worker collects
it on its next heartbeat.

If you want a worker to shut down, ask it to stop *first* and wait for the
acknowledgement; pause after, if you also want the credential frozen. Pausing
first is not wrong — it is stricter — but nothing more will happen until you
resume.

## Surfaces

```text
PUT    /workers/heartbeat        # the worker's own: {worker_key, node_label?,
                                 #   capabilities?, state?} → carries kill_requested
GET    /workers                  # the fleet (admin) or your own workers
GET    /workers/{id}
POST   /workers/{id}/kill        # admin: record the instruction
DELETE /workers/{id}/kill        # admin: withdraw it, if unacknowledged
```

MCP: `worker_heartbeat`, `list_workers`, `request_worker_kill`,
`cancel_worker_kill`. The cockpit shows each agent's workers on **Admin →
Agents**, with a **Told to stop** panel for requests still unanswered.

## Configuration

| Variable | Default | Means |
|---|---|---|
| `ATHENA_WORKER_STALE_SECONDS` | 90 | Older than this reports as `stale` |
| `ATHENA_WORKER_MAX_PER_AGENT` | 50 | Registry ceiling per agent |

The ceiling bounds growth, not refreshes: a looping or compromised token may
refresh the rows it has forever and still never add another.

## Limitations

- `node_label` and `capabilities` are **self-declared** and echoed back verbatim.
  Athena never routes, schedules, or authorizes on either — there is no scheduler
  here, and adding one would be a different product.
- There is no process supervision, restart, resource reporting, or log capture.
- Nothing expires a worker row. A retired node stays in the registry as a stale
  entry until the operator's own tooling removes it; there is no delete endpoint
  yet.
- The kill is cooperative in the strongest sense: a worker that ignores it keeps
  running, and the only thing Athena can do about that is show you.
