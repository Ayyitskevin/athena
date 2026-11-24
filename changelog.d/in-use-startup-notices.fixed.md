- **What a start says about its configuration is true.** Every start logged
  "set `ATHENA_COOKIE_SECURE=1` for any deployment served over HTTPS", a
  setting `athena-serve` refuses. The line is now logged when serving starts,
  not whenever the app is built: an `athena-serve` start says its cookies carry
  no Secure flag because it serves direct HTTP and refuses the setting (only
  `athena-serve` says this; `athena-demo` does not), and another start with
  the setting off keeps its warning, now saying the setting matters only
  behind a TLS-terminating proxy. `athena-serve` and `athena-doctor` also name,
  in one warning once the settings have loaded, every `ATHENA_` variable in
  their environment that no setting declares, such as a misspelled
  `ATHENA_ANONYMOUS_READ`, without printing its value; a variable the MCP
  server or the seat doctor reads is declared and never reported, and neither
  are the client-side names the documents use (`ATHENA_URL`,
  `ATHENA_ADMIN_TOKEN`, `ATHENA_AGENT_TOKEN`, `ATHENA_ADMIN_PASSWORD`,
  `ATHENA_ISSUE_ETAG`).
