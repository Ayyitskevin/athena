- **A token can be minted on the host (H17).** `athena-token <database>
  <email> --name <label> --scope <scope>...` mints a token for an existing,
  unpaused account through the audited command and prints it once, alone on
  standard output. The `minted_token` event's detail ends `via athena-token
  on the host`. Before, the first token could only be minted through the
  browser form.
