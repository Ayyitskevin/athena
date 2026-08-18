# CodeQL main triage — 2026-08-18

Scope: Athena `main` at `22689716a9230a84684dd21572fe0419815fb36e`.
This is a local triage record, not authorization to dismiss alerts or change
repository security settings.

## Inventory and disposition

GitHub reported 81 open CodeQL alerts on `main`:

| Alerts | Query | Triage disposition |
| --- | --- | --- |
| #85 | `py/command-line-injection` | False positive once the explicit no-shell regression is reviewed: the Buzz CLI is invoked with an argument vector, `shell=False`, and issue-controlled text remains one `--content` argument. The private key is supplied only through the child environment. |
| #84 | `py/clear-text-logging-sensitive-data` | False positive: the reported value is the documented public credential for a synthetic, loopback-only, throwaway demo. It is not a production secret. |
| #81 | `py/cookie-injection` | False positive: the cookie name is constant and the theme value is restricted to `dark`, `light`, or `system` before `set_cookie`; the cookie is HTTP-only and SameSite=Lax. |
| #2–#12 | `py/stack-trace-exposure` | False positives pending independent review: the responses contain bounded validation or domain errors, not Python tracebacks. Returned template values are escaped. |
| 65 of 67 redirect alerts | `py/url-redirection` | False positives: destinations are fixed same-origin paths with typed integer identifiers or percent-encoded query text. |
| #82, #83 | `py/url-redirection` | Keep open. `_safe_next` rejects absolute and scheme-relative destinations, but browser-level review remains required for unusual slash/backslash encodings before dismissal or behavior changes. |

No GitHub alerts were dismissed during this triage.

## Redirect measurement

A real Athena `TestClient` session with login and CSRF exercised 14 `next`
values through the theme redirect. Ordinary local paths were preserved.
Absolute HTTP(S), scheme-relative, `javascript:`, `data:`, bare-host, empty,
and leading-backslash values fell back to `/`. Raw or percent-encoded mixed
separators produced path-form `Location` values rather than an external URL.

This supports the current same-origin boundary but is not sufficient to close
#82/#83. The required external research lane was unavailable because DeepAPI
credentials were not configured, and no real-browser verification was run.

## Next gates

1. Obtain an independent review of this classification and the no-shell test.
2. Configure DeepAPI and complete browser/external-research review for #82/#83.
3. Ask the repository owner separately before dismissing alerts or enabling
   GitHub secret scanning, push protection, Dependabot security updates, or
   repository rulesets.
