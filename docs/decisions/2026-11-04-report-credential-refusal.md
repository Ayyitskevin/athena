# Refuse recognized credentials in new report writes

Date: 2026-11-04 (decision date).
Status: Accepted implementation direction, direct maintainer request.
Superseded-by: None.

## Context

Completion reports and yield evidence become durable records. Accidentally
including a credential in that context cannot be repaired by quietly editing
the actor's evidence or rewriting the activity chain.

## Decision

Refuse rather than redact high-confidence credential shapes in new completion
report text and new yield evidence. A small pure function recognizes PEM private
key headers; known GitHub, AWS, Slack, OpenAI, Anthropic and native token formats;
password-bearing URL userinfo; and Authorization values with at least 32 token
characters and at least one digit in the value. Provider rules use explicit
prefixes, alphabets and lengths rather than entropy. Ordinary prose,
revision/digest hex, UUIDs and password-free URLs
remain valid evidence.

The credential refusal from the shared commands returns the existing invalid
error with only the controlled field path and a fixed credential-kind label. The
credential refusal itself never repeats the text. A request that fails a
different validation first, such as an over-long or wrongly typed field, can
have the submitted value repeated back to the caller in that error. That refused
input is neither stored nor logged. The shared commands do not redact, write a
report/handoff, append an event, or release/renew/sweep a lease on a credential
hit. An actor can correct the field and retry under the ordinary possession rules.

Scanning belongs to the accepting write commands, never the reusable grammar
normalizers or read projections. Existing history, including credential-shaped
text already stored, remains readable and is neither rewritten nor hidden.

## Consequences

This is an accident guard, not a security boundary or proof that text is safe.
It does not validate a credential with its provider, decode/normalize obfuscated
credentials, inspect files or fetched references, guess arbitrary passwords,
scan entropy, or cover every historical/future provider format. For example,
only the enumerated Slack token families/segment lengths and OpenAI watermark
formats are recognized. Unknown formats and intentionally split/encoded text
can pass. Keep all credentials and private payloads out of reports regardless.

The Authorization rule's digit requirement lets instructional word placeholders
pass without a general entropy heuristic. A digit-free credential not recognized
by another pattern can also pass; the other provider patterns are unchanged.

The scope excludes other endpoints and handoff fields such as attempted work,
notes, blocking questions and resume instructions. It does not change
authentication, authority, approval, status, history retention or lease policy.
The absence of a refusal is not a verification verdict.

## Sources

Direct maintainer implementation request (2026-11-04): refuse, never
redact; known shapes only; write path only; preserve historical reads and exact
lease state on refusal. The same-date maintainer clarification requires a digit
in the Authorization value, leaves other transport validation unchanged, and
documents its possible caller-only value echo. This supplements, without
superseding, the
[completion-report decision](2026-11-04-completion-reports.md).

The implemented contract is [CLOSEOUT_RECIPE.md](../CLOSEOUT_RECIPE.md),
[lease_commands.py](../../src/athena/aegis/lease_commands.py), and the pure
[shape detector](../../src/athena/aegis/report_credentials.py). Native token
shape is read from [tokens.py](../../src/athena/core/tokens.py); no authentication
code is changed.
