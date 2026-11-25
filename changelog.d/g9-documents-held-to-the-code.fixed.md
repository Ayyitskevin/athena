- **The documents a seat reads name only tools its profile carries, the desk-loop
  prompt runs, and settings tables are generated (G9).** The Field Guide and
  `docs/DESK.md` sent a seat to `watch`, `unwatch`, `mark_notifications_read`,
  `recent_events`, `search_workspace`, `start_playbook`, `begin_run` and
  `my_office`, which no seat profile carries; each is now named with the profile
  that carries it, or replaced by the seat's own tool (the desk loop drains with
  `wait_for_events(wait=0)`). `tests/test_mcp_docstring_references.py` fails
  when one of these documents names a tool its profile lacks without naming the
  profile that has it.

  `examples/desk_loop.md` now runs, writes included, as the agent
  `docs/RUNTIME_RECIPE.md` onboards. Running it found three faults, fixed: it told
  a `seat+knowledge` agent to call `begin_run`, it named a `delegated` desk lane
  that is `work.delegations`, and its first learning was refused because a new
  instance has no space for a runbook (the recipe now creates one, and the prompt
  says when to pass `space_id`).

  The settings tables in `docs/DISPATCH.md` and `docs/WORKERS.md` are generated
  from the settings registry by `scripts/generate_settings_doc.py`, whose
  `--check` covers them. `docs/AGENT_API.md` and `docs/INDEX.md` name
  `/openapi-agent.json` as the REST surface an agent's token can call, and
  `/openapi.json` as the whole app's document. Two plan guides that quoted the
  July coverage floors as standing figures point at `pyproject.toml`, and a test
  fails when a present-tense document copies a floor. No route, tool, setting or
  behaviour changed.
