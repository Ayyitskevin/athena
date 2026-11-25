- **A refused form renders in the page that posted it, and a comment survives
  a sign-in (U11).** A refused plain form post used to answer with one bare,
  unstyled line and no way back. A validation refusal on the issue page and its
  edit form, a Mentor page and its editor, a space and its edit form, the
  filters page, the fleet roster and an agent's token form now re-renders that
  page with the error at the top and what was typed still in the form. Every
  other refusal (sign-in required, read-only, admin only, not found) is a page
  inside the application with a link back. Status codes and wording are
  unchanged, htmx still receives the fragment it swaps, and a hidden object
  still answers exactly like a missing one.

  The new-comment boxes on an issue and on a Mentor page keep what is typed in
  the browser tab (`sessionStorage`, `static/drafts.js`) until the comment is
  accepted, so a comment posted after the session expired is back in its box
  once the person signs in and returns. The sign-in answer to such a comment
  also shows the text that was not posted. Nothing about drafts is stored on
  the server.
