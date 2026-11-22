- **MCP configs name a profile, and `athena-mcp` refuses a rejected token.**
  The config printed by `athena-demo`, `POST /users/onboard_agent` and the
  cockpit onboarding page now sets `ATHENA_MCP_PROFILE` (`seat`, or
  `seat+knowledge` for a token with `docs:write`) and the absolute path of
  `athena-mcp`. An unset profile still means `full`. `athena-mcp` exits with a
  message when Athena answers its token with 401 or 403; it still starts when
  Athena cannot be reached.
