- **Mission Control's "Copy command" works, and no page logs a blocked style
  (U14).** The copy script and three filter selects' `onchange` handlers
  were inline, which the pages' own content security policy blocks; they are
  now behaviours of `static/confirm.js`, asked for with `data-copy-command`
  and `data-autosubmit`. htmx no longer adds its indicator style, which the
  policy blocked on every page load. The policy is unchanged.
