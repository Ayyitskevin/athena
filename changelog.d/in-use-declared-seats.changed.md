- **Declared fleet seats are data, managed from the cockpit and over REST
  (U4).** The Fleet roster and "Assign to a seat" knew only the eight seats
  written into `core/fleet_roster.py`, so a new instance opened on a "Roster
  drift" alert about seats it never had and listed every agent onboarded
  through the cockpit as drift. Seats now live in the `fleet_seats` table
  (migration 0093, decision D27): an instance that already had users keeps its
  eight, and a new one starts with none and shows no drift alert. An admin
  declares, edits and removes a seat on `/admin/fleet` (including a "Declare a
  seat" form on each agent row no seat declares) or over `GET/POST /fleet/seats`
  and `PATCH/DELETE /fleet/seats/{slug}`, each recorded on the trail and
  accepting an `Idempotency-Key`. A taken slug or email is refused `409
  conflict`. `seat_slug` in the desk, office and work-context packets is read
  from the table. There is no MCP tool for seats.
