- **The cockpit's JSON refusals name registered codes (G4).** The command
  palette, `/admin/fleet.json`, a run's `replay.json` and a project's
  `floor.json` answered refusals with a detail and no `code`, and the palette
  sent the command's internal kind (`invalid`) where the registered code
  (`validation_error`) belongs. Each now carries the code a REST route would,
  and `scripts/check_refusal_codes.py` holds `web/` to its JSON-body rules.
