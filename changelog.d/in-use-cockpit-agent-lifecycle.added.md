- **A token for an existing agent, a restore, and a playbook start, in the
  browser (U5).** An admin mints a token for an agent that already exists:
  `POST /users/{user_id}/tokens`, and **Mint a token** in the agent's section
  of `/admin/agents`, which shows the raw token and an MCP config once. The
  trail records the admin as the minter. A removed agent is restored from a
  **Removed agents** panel on the same page, and a playbook page shows
  **Start this playbook** with how many steps it will make.
