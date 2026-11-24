- **A conductor can find the account it means to assign or delegate to.**
  `GET /issues/{id}/eligible-accounts` (the `list_eligible_accounts` tool, on
  the `conductor` profile) lists, for an issue the caller can see and a caller
  with a write role, the accounts that can see and work that issue: `id`,
  `name` and `is_agent`, paged in the vocabulary every new read uses (`cursor`
  in, `next_cursor` and `has_more` out). Assignment takes every listed account and
  delegation every listed agent. A hidden issue reads as missing.
  `delegate_issue` now answers an id that does not exist, was removed or is not
  an agent alike (`no agent account with that id`). `POST /users/onboard_agent`
  and the cockpit's onboarding form take an optional `profile` (`seat`,
  `seat+knowledge` or `conductor`) that the printed `mcp_config` names; it
  grants nothing, the default is unchanged, and an explicitly requested profile
  the token's scopes cannot fully use is refused with 422 before anything is
  created.

  **Behaviour change:** a removed account can no longer be assigned, delegated
  to or added as a contributor, by a request or by a composed `create_issue`.
  Each answers exactly as for an id that does not exist, so a write no longer
  finds an account that removal hid from every list, picker and lookup. An
  automation rule that names a removed account now fails as one that names a
  missing account does.
