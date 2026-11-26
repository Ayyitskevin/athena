- **A conductor hears once that an outcome's slices are done.** When the last
  open child of an issue closes, its creator and assignee get one
  `children_done` notification on the parent; it closes nothing. Consuming an
  approval stays in the trail but no longer notifies anyone: the act it gated
  notifies as before.
