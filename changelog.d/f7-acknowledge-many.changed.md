- **One call acknowledges many notifications, and a lease renewal no longer
  notifies anyone.** `POST /notifications/read_all` takes an optional body:
  `{"ids": [...]}` acknowledges those of yours and lists any other id (another
  account's, missing, or hidden from you now) under `not_found`, alike and
  unchanged; `{"through_id": N}` acknowledges your unread notifications up to
  and including `N` and nothing newer. With no body it still acknowledges
  everything. The `mark_notification_read` tool takes exactly one of
  `notification_id`, `ids` or `through_id`; no tool was added (D18). A
  `lease_renewed` event stays in the trail and the event feed but creates no
  notification. In the fleet trial the 39 single acknowledgements became 4
  calls and one outcome takes 90 calls, where it took 142.
