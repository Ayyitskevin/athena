# Security and governance baseline — 2026-08-22

Scope: GitHub `main` and the deployed checkout commit
`84bccad9dbf60a50b0386a81c2f014e295bad5c7`. This report records evidence and
candidate code; it does not authorize alert dismissal, repository-setting changes,
or production deployment.

## Live inventory

The GitHub REST API reported 80 open CodeQL alerts:

| Count | Query | Current disposition |
| ---: | --- | --- |
| 1 | `py/clear-text-logging-sensitive-data` | #84 is the randomly generated password printed by the loopback-only synthetic demo. It is not a production credential, but it is intentionally a working credential; keep open until the demo UX receives an explicit product/security decision. |
| 1 | `py/cookie-injection` | #81 is a fixed-name, HTTP-only, SameSite=Lax preference cookie. The submitted theme must be one of the closed set `dark`, `light`, or `system` before the write. Existing request tests pin invalid-value refusal. Candidate for independent-review dismissal, not silent suppression. |
| 11 | `py/stack-trace-exposure` | #2, #4–#12, and #94 do not return Python tracebacks. Spot checks trace responses to fixed or escaped validation/domain messages; the two sprint-state responses interpolate fixed library literals without escaping, so the classification must be revisited if those messages ever accept outside text. #94 points at a normal embed resolver return. Keep open until the independent review confirms every source path. |
| 67 | `py/url-redirection` | 65 destinations are fixed same-origin prefixes with typed identifiers or percent-encoded query text. #82/#83 accept a form-carried local return path and received defense-in-depth hardening in this candidate. No alert is dismissed here. |

Commands used for the live count:

```bash
gh api --paginate \
  'repos/Ayyitskevin/athena/code-scanning/alerts?state=open&per_page=100'
gh api --paginate \
  'repos/Ayyitskevin/athena/dependabot/alerts?state=open&per_page=100'
```

## Redirect adversarial result

The form return-path predicate already rejected absolute URLs, scheme-relative
URLs, and non-path schemes. A raw `/\\host` value is authority-like after the
[WHATWG URL parser](https://url.spec.whatwg.org/#concept-basic-url-parser)
normalizes backslashes. In the measured FastAPI/Starlette response, header
serialization percent-encoded that backslash to `/%5C...`, which stayed same-origin;
there was no demonstrated exploit in the actual response.

The candidate now rejects every backslash and ASCII control before constructing the
response. This removes reliance on a downstream serializer retaining that safe
behavior. `tests/test_operator_plumbing.py::test_preference_next_cannot_leave_the_app`
failed before the predicate change and passes afterward together with the theme and
rail cookie tests.

## Dependabot finding

One high Dependabot alert remains open for `cryptography` in `uv.lock`
(`GHSA-g6cj-pr64-35w5`, vulnerable `<50.0.0`, patched in `50.0.0`). It does not
describe Athena's current supported dependency graph:

- `uv.lock` is absent from the tracked tree;
- `constraints/ci-py312.txt` pins `cryptography==50.0.0`; and
- `.github/dependabot.yml` explicitly declares the supported setuptools/constraints
  workflow and documents GitHub's earlier incorrect uv ecosystem inference.

This is stale deleted-manifest evidence, not proof of a vulnerable deployed package.
Closing the GitHub alert is a separate human-approved dashboard mutation. The normal
freeze diff and advisory audit remain the executable dependency gates.

## Repository controls

Read-only GitHub API checks on 2026-08-22 found:

- private vulnerability reporting: enabled;
- Dependabot security updates: enabled;
- secret scanning and push protection: enabled;
- required `main` checks: `test` and `container` only;
- required approving reviews: zero;
- administrator enforcement: disabled; and
- repository rulesets: none.

The security features are materially stronger than the stale release document said,
so `SECURITY.md` and `RELEASE_READINESS.md` are corrected in this candidate. Merge
governance is still incomplete: CodeQL is not required. GitHub documents ruleset
merge protection for code scanning in its
[code-scanning merge-protection guidance](https://docs.github.com/en/code-security/code-scanning/managing-your-code-scanning-configuration/set-code-scanning-merge-protection).
Enabling it is intentionally outside this code-only slice.

## Runtime provenance evidence

`GET /version` returns a process-start snapshot with exactly `version`, `commit`,
`tree_state`, and `source`, without opening SQLite. The standalone deployment checker
compares that snapshot with the checkout's current commit and cleanliness and fails
closed on every mismatch. Twenty consecutive real loopback HTTP requests returned
the same full commit and dirty startup snapshot during candidate development; the
checker then correctly refused both the dirty startup and the currently dirty tree.
A clean-checkout success measurement remains an exit gate after the candidate commit.

This is deployment provenance, not signed supply-chain provenance. SLSA's
[provenance model](https://slsa.dev/spec/v1.2/provenance) binds an artifact to its
build definition and resolved dependencies; Athena's local runtime snapshot does not
make that stronger attestation claim.

## Gates before shipping

1. Run the focused and full local gates, including a clean-checkout real HTTP check.
2. Obtain a non-author review of the endpoint, redirect boundary, checker, templates,
   and alert classifications.
3. Obtain repository-owner approval before merging or deploying this security-
   sensitive candidate.
4. Treat alert dismissals, CodeQL merge protection, and anonymous-read policy as
   separate decisions with their own evidence and rollback plans.
