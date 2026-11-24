- **Stopping an agent names its leases and may release them (C8).** Pausing an
  account, revoking its tokens, offboarding it and removing it now answer with
  its `active_leases` (issue, generation, expiry, whether released), over REST
  and through `pause_agent`, `revoke_agent_tokens` and `offboard_agent`. With
  the explicit `release_leases` option the same act releases each one, recorded
  per lease as `lease_released` naming the holder; without it nothing is
  released. An admin also settles another account's expired lease with
  `complete_claim` and its generation, without claiming it first: the lapse is
  recorded once, then the release.
