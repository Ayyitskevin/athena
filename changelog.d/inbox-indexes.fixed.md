- **The inbox list and the priority inbox no longer read a user's whole
  history.** The newest-first inbox sorted all of a user's notifications before
  taking its page, and the priority inbox read the whole visible inbox before
  applying its limit. Migration `inbox_indexes` adds an index over unread rows
  and one over each user's rows in order (replacing 0023's), and the priority
  inbox reads a batch at a time and stops once its page is full: 765 ms to
  under 1 ms for an admin at 50,000 issues. An acknowledgement by id finds each
  named row by its key and asks the visibility gate of that row's own event,
  where it could build the set of every visible event first, or walk every
  notification the user has. Every inbox read returns the same rows, order and
  counts as before. Not faster: the priority summary, which reports on every
  visible row; a priority read whose `min_priority` few rows pass, which reads
  back until its page is full; and the unread count for a non-admin, which
  checks visibility on every unread row.
