- **The operator can resume a claim handoff from the command palette.** The
  palette claimed an issue and yielded it with a handoff but could not resume
  one, so an operator who took over an issue a seat had handed off could not
  acknowledge it in the browser, and their own yield with a handoff was refused
  until it was resumed over the API. The palette now offers "Resume handoff"
  beside yield and complete, to the current leaseholder only and while a handoff
  is open, through the same command the API uses.
