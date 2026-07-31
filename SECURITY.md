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

The supported shape is Python 3.12, one Athena process and uvicorn worker, one
SQLite database and attachment directory on local storage, and clients on a
trusted local machine or tailnet. Remote browser access should normally pass
through an HTTPS reverse proxy. Public internet exposure, hostile multi-tenancy,
multi-process/HA deployment, and enterprise isolation are not current security
claims.

Before a long-running deployment, follow
[`docs/OPERATIONS.md`](docs/OPERATIONS.md), including secure cookies, request
limits, attachment ownership and integrity, a single webhook/automation runner,
matched database-plus-attachment backups, and `athena-doctor`.

On a fresh database, HTTP creation of the first administrator is disabled until
the operator configures a generated `ATHENA_BOOTSTRAP_TOKEN` and presents it in
the dedicated request header. Complete that step over loopback before enabling
shared ingress, then remove the token and restart. A reverse proxy's loopback
connection is not evidence that its originating client is local. Duplicate
bootstrap headers fail closed. `Idempotency-Key` is deliberately unsupported
before an actor exists, because no durable principal can own the receipt; Athena
rejects it on otherwise valid requests with the normal anonymous `401` without
consulting bootstrap state.

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

A useful report includes:

- the affected current-`main` commit hash;
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
