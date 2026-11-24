- **The ready rule reads open work only, and the desk asks it once.** The rule
  behind the desk's ready lane, `GET /desk/ready`, `POST /issues/claim-next` and
  the `is:ready` query atom read every issue that was not archived, closed ones
  included, and pickup did so holding the writer lock; the desk asked it four
  times per read. It now starts from the issues that may be open, decided per
  project, and the desk takes its ready lane and its not-offered list from one
  evaluation. Which issues are ready, and in what order, is unchanged. Two
  conditions remain: an issue closed under a project's own closed status (such
  as `shipped`) is still read once, by its index entry, because the same name
  is open where its project does not list it; and a project that makes a closed
  name open has its issues under that name read, as they are open there. The
  not-offered list now refuses a limit below 1, as the ready lane always has.
