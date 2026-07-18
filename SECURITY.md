# Security policy

Athena handles authentication credentials, private operator notes, agent
tokens, outbound webhooks, and an append-only activity trail. Security reports
are treated as product issues, not ordinary feature requests.

## Supported versions

Athena is local-alpha software. Security fixes target current `main` and the
most recent tagged alpha. Older commits, untagged forks, and modified
deployments are not maintained as separate support lines.

## Deployment boundary

The supported shape is one process on a trusted local machine or tailnet,
normally behind an HTTPS reverse proxy when accessed remotely. Public internet
exposure, hostile multi-tenancy, and enterprise isolation are not current
security claims.

Before a long-running deployment, follow
[`docs/OPERATIONS.md`](docs/OPERATIONS.md), including secure cookies, request
limits, attachment ownership, a single webhook/automation runner, backups, and
`athena-doctor`.

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** flow when GitHub
private vulnerability reporting is available. If that entry point is not
available, contact the repository owner through GitHub before transmitting
details. Do not include a live token, password, private database, journal text,
or customer information in an issue, discussion, pull request, or log paste.

A useful report includes:

- the affected commit or tagged version;
- the deployment assumptions required to reproduce it;
- the smallest safe reproduction;
- expected versus observed authorization or data behavior;
- impact and whether credentials or private facts were exposed; and
- suggested mitigation, if known.

Please allow the maintainer time to reproduce and coordinate a fix before
public disclosure. Athena does not currently promise a formal response SLA.

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
