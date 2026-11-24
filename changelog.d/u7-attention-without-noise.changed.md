- **The attention queue raises decisions, not check-in noise.** The owner
  decided four rules for this change, and these reads now answer differently:
  `GET /attention/ranking` and `get_attention_ranking`; `GET /fleet/active-work`,
  `get_fleet_active_work` and Mission Control (`attention_state`,
  `attention_reasons`, `summary.needs_attention_count` and the
  `attention_state` filter); the dashboard's fleet-attention card and the
  Intervene badge; and the questions panel on the agents page. No field was
  added or removed, no code was renamed, and no schema version changed
  (`athena.fleet_active_work.v1` and the ranking's shape are as they were).

  - A tagged claim is not attention until its heartbeat is overdue:
    `checkin_missing` and `checkin_stale` apply once more than
    `ATHENA_AGENT_RUN_STALE_SECONDS` has passed since the possession was
    acquired (its `claimed` event) or since the run's last check-in, whichever
    is later. The grace runs from the acquisition, not from a renewal, so a
    holder that keeps renewing and never reports still becomes attention; a
    re-claim is a new possession and starts it again. An untagged run has no
    grace: `run_untagged` applies at once, as before.
  - In the ranking, a claim whose only reasons are `checkin_missing`,
    `checkin_stale` or `run_untagged` ranks `medium` instead of `high`, so
    every pending approval and unanswered question ranks above it. A lapsed
    lease or any other reason stays `high`.
  - Only a person's act answers a question for the operator. A `needs_input`
    question that a non-agent account other than the asker has answered since
    it was asked (a comment, status change, assignment change or claim; the
    asker commenting again restates it) keeps its `awaiting_input` signal but
    ranks `low`, its reason opens "answered, waiting on the agent", it offers
    no next action, and the agents page marks it so and leaves it out of its
    heading count. Another agent's comment or claim answers nothing here, and
    a `blocked` yield's question is never marked answered. The ready lane's own
    rule is unchanged.
  - Open, unanswered questions are counted in "needs you": the card gains a
    "Questions waiting on you" row linking the agents page's questions, so a
    critical question can no longer sit in the queue under a badge that says
    nothing needs you.
  - The dashboard's Now queue words each row: what it is, the issue's key and
    title, and what is wrong, instead of `checkin_missing` and "issue #12"; an
    approval names the issue it would act on, as does the "oldest request
    waiting on you" lane.
