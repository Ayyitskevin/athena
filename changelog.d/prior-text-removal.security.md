- **An administrator can remove the text an issue edit replaced.** Every edit
  keeps the title and body it replaced, so editing a secret out of an issue left
  it readable to anyone who could see the edit. `DELETE
  /issues/{issue_id}/prior-texts/{event_id}`, for an administrator with an
  admin-scope credential, removes that edit's kept text and records
  `issue_prior_text_removed` naming the edit, never the text. The database refuses
  the delete without that event (migration 0096), and the edit's undo then refuses
  as for an edit that kept nothing.
