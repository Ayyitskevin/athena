# Security policy

Athena handles authentication credentials, private operator notes, agent
tokens, outbound webhooks, and an append-only activity trail. Security reports
are treated as product issues, not ordinary feature requests.

## Supported versions

Athena is local-alpha software. As verified on 2026-07-23, the repository has no
tags and no GitHub releases; package version markers in the source and changelog
are not published release lines. Security fixes target current `main` only. Older
commits, forks, and modified deployments are not maintained as separate support
lines.

## Deployment boundary

The supported shape is Python 3.12, one `athena-serve` process and uvicorn worker,
one SQLite database and attachment directory on local storage, and direct clients
on a trusted local machine or explicitly configured tailnet. The launcher has no
public mode: it accepts only exact loopback/Tailscale numeric binds, validates an
exact Host-authority allowlist before body/session/database work, disables proxy
headers, and preflights durable recovery state before Athena/Uvicorn accepts
traffic.
Public internet exposure, reverse proxies/tunnels, hostile multi-tenancy,
multi-process/HA deployment, and enterprise isolation are not current security
claims.

The socket guard does not detect reachability. A proxy, tunnel, NAT rule,
container publication, or Tailscale Funnel can publish an allowed loopback
listener; a Tailscale-range address does not prove ACL policy. Operators must
verify those external controls independently.

Before a long-running deployment, follow
[`docs/OPERATIONS.md`](docs/OPERATIONS.md), including cookie transport posture,
request limits, attachment ownership and integrity, a single webhook/automation runner,
matched database-plus-attachment backups, and `athena-doctor`. The current
supported launcher is direct HTTP and refuses HTTPS-only cookies; Athena does not
yet claim a supported TLS or proxy-termination shape.

On a fresh database, HTTP creation of the first administrator is disabled until
the operator configures a generated `ATHENA_BOOTSTRAP_TOKEN`, starts
`athena-serve --bootstrap` on loopback, and presents it in the dedicated request
header. The supported bootstrap also requires the first administrator to set a
password, preventing a credentialless account from stranding the normal restart.
Stop, remove the token, and restart normally immediately after creation.
Duplicate bootstrap headers fail closed. `Idempotency-Key` is deliberately
unsupported before an actor exists, because no durable principal can own the
receipt; Athena rejects it on otherwise valid requests with the normal anonymous
`401` without consulting bootstrap state.

## Reporting a vulnerability

GitHub private vulnerability reporting is disabled for this repository (verified
2026-07-23). First contact the repository owner using the contact information on
their GitHub profile, without vulnerability details, affected endpoints, exploit
steps, or secrets, and ask to arrange a private channel. If no private contact
method is available, any public contact must remain a detail-free request to speak
privately. Do not transmit the report until both sides have agreed on that channel.

Never include a live token, password, private database, attachment, operator note,
journal text, customer information, or other secret in an issue, discussion, pull
request, public message, or log paste.

## Automated analysis

The repository's CodeQL workflow analyzes Python, first-party browser
JavaScript, and GitHub Actions on pull requests to `main`, pushes to `main`, a
weekly schedule, and explicit operator dispatch. Python scope includes sources
under `src/`, `scripts/`, and `examples/`; JavaScript scope includes first-party
browser code under `src/`; and workflow scope includes `.github/`. Tests and the
vendored `src/athena/static/htmx.min.js` library are excluded. Athena does not
claim language-specific CodeQL coverage for shell, Jinja template semantics,
standalone SQL migration contents, or CSS. The workflow uses GitHub's
`security-extended` query suite, so triage may include lower-confidence results
that the default suite would omit.

The scanner references no repository-configured secret and receives no
content-write, identity-token, issue, or pull-request authority. Its only write
capability is the `security-events` permission required to publish SARIF results
to GitHub code scanning. Every external action reference is pinned to a full
commit SHA and checked by the test suite, including references in local actions
reached from a workflow. A clean scan is point-in-time evidence, not proof that
Athena is vulnerability-free and not authority to expose the unsupported public
deployment shape.

A useful report includes:

- the affected current-`main` commit hash;
- the deployment assumptions required to reproduce it;
- the smallest safe reproduction;
- expected versus observed authorization or data behavior;
- impact and whether credentials or private facts were exposed; and
- suggested mitigation, if known.

Please allow the maintainer time to reproduce and coordinate a fix before
public disclosure. Athena does not currently promise a formal response SLA.

## Login throttling, and the trade it makes

`POST /login` is credential-free at the door, so two fixed-window throttles guard it,
both checked **before** the password hash (so a flood is bounded by the limiter rather
than by PBKDF2's per-guess CPU cost):

| Limit | Keyed by | Default | Bounds |
|---|---|---|---|
| `ATHENA_LOGIN_RATE_LIMIT_PER_MINUTE` | direct peer IP | 10/min | one host hammering |
| `ATHENA_LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE` | submitted email | 5/min | one account, from anywhere |

The per-account limit exists because credential stuffing is distributed by
construction: a thousand hosts guessing ten passwords each at one address stays under
every per-IP ceiling while making ten thousand attempts on that account.

**The trade, stated plainly:** any per-account throttle is also a lever for denying
service to a known address — an attacker who knows your email can spend five requests
a minute keeping you at a 429. Three things bound that, deliberately:

- The window is **one minute**, not a durable lockout. There is no state an attacker
  can push you into that outlasts their own effort, and no administrative unlock to
  wait for. Sixty seconds after they stop, you log in.
- The limiter is **in-process** (`core/rate_limits.py`) and resets on restart, like
  every other limit here.
- It is **keyed by the submitted email, not the resolved account**, so it cannot be
  used as a membership oracle. Throttling by user id would mean a real address returns
  429 while an unknown one returns 401 — a free existence test, undoing the
  opacity that `users.verify_credentials`' dummy PBKDF2 verify and the background-task
  audit write exist to provide. A 429 tells the caller only that this address was
  hammered recently, which the caller doing the hammering already knows.

Set `ATHENA_LOGIN_ACCOUNT_RATE_LIMIT_PER_MINUTE=0` to disable it if that trade is
wrong for your deployment; the per-IP limit is unaffected. A throttled attempt against
a real account is recorded on the activity trail as a `login_throttled` security event
and appears on **Admin → Security** with the other refusals.

## High-value review areas

Reports are especially useful around:

- authorization or visibility bypasses;
- bearer-token scope, revocation, or idempotency failures;
- CSRF, session, OIDC, or password-reset behavior;
- webhook SSRF, redirect, DNS-rebinding, or signing weaknesses;
- attachment path traversal or unauthorized disclosure;
- audit events that can diverge from the mutation they describe;
- import/export integrity or cross-scope data leakage; and
- agent actions that escape their declared actor, run, rate, or token boundary.

## Non-security reports

Crashes without a confidentiality, integrity, or authorization impact belong in
the normal issue tracker. Never attach a real Athena database to either channel;
reduce the problem to synthetic data first.
