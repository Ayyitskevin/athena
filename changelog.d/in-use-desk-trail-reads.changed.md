- **The desk no longer reads the whole trail to find the newest event.**
  `GET /desk` (and the cursor check of `POST /desk/cursor`) asked for the
  newest event a seat can see as one maximum over the four-way visibility
  gate, which read every event; `GET /desk/since` with no cursor sorted every
  visible event to take the oldest 500. Both now ask each visibility arm
  separately through the (kind, id) index, named in the statement so a
  database someone has run ANALYZE on is planned the same way, and return the
  same answers for every kind of caller. One condition remains: a caller whose
  newest events of a kind are all hidden still walks past them, as before.
