- **A held lease renews on its generation alone, and a heartbeat reports on
  the session's run.** The holder's renewal with its current generation no
  longer needs `If-Match` (an `If-Match` it sends is still checked); a first
  claim still answers 428 without one, a stale or wrong generation and any
  account but the holder are refused as before, and a renewal that sends
  `paths` or `coordination_root` still needs the issue ETag. The 428 that names
  the generation says so. A claim's and a renewal's answer now carries the
  issue's current `issue_etag` beside the lease, so a holder that only renews
  sees when the issue changed. `heartbeat_agent_run` and
  `PUT /agent-runs/heartbeat` take `run_id` as optional: omitted, the run the
  request is tagged with (`X-Athena-Run`; the MCP client's run) is reported,
  and a `run_id` that is not that run is refused with the new code
  `run_id_mismatch` (422). A run header the server would drop or reshape (a
  reserved namespace, over 200 characters, not a printable run id) is refused
  with the new code `run_header_invalid` (422) instead of being read as absent.
