- **Minting and revoking a token act as the live account.** `mint_token` and
  `revoke_token` take the resolved account and re-read it under their own lock, so
  an account paused or removed after the caller looked it up gets no fresh
  credential (a paused one may still revoke). The host `athena-token` command, the
  demo and the MCP launch smoke act as the account they always recorded. Two
  commands leave the bare-actor-id debt; its ceiling is 35.
