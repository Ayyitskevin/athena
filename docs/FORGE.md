# Forge integration

Your commits, branches, and pull requests are visible from the work item that
caused them.

**Athena integrates with a forge. It never becomes one, and it never calls one.**

- [Inbound only, and why](#inbound-only-and-why)
- [Registering a source](#registering-a-source)
- [What arrives, and what happens to it](#what-arrives-and-what-happens-to-it)
- [Why imported is the whole design](#why-imported-is-the-whole-design)
- [Refusals](#refusals)
- [Limits, stated](#limits-stated)

## Inbound only, and why

Athena accepts signed events **from** a forge. It has no outbound integration: no
polling, no API calls, no stored GitHub token. Two consequences, both deliberate:

- **No new egress surface.** The SSRF-hardened webhook sender is still the only
  thing in Athena that makes outbound requests.
- **No third-party credential in the database to leak.** The secret stored
  against a source is *Athena's* — the value an inbound request must prove
  knowledge of. Compromising it lets someone write foreign history into your
  workspace; it does not let them touch your forge.

The cost is real and worth naming: Athena cannot backfill. It knows only what was
delivered to it while a source was registered and enabled, and it cannot go and
ask. That is the trade for holding no key to your code host.

## Registering a source

Admin only, and audited like a webhook registration.

```
POST /event-sources   {"name": "gh", "kind": "github", "host": "github.com"}
→ 201 {..., "secret": "evtsec_…"}
```

The secret is returned **once** and is never readable again — the same
one-time-display contract as an API token, but not the same storage. An API
token is persisted only as its SHA-256 hash; an event-source secret is stored
**plaintext**, necessarily — HMAC verification needs the shared value itself,
not a hash of it. A database leak that exposes token hashes costlessly also
exposes live source secrets, so the row deserves the same care as the
credential it is.

For a forge that can already reach Athena inside the same supported local or
tailnet boundary, point its webhook at:

```
POST https://your-athena/forge/gh
```

with GitHub's standard `X-Hub-Signature-256` and `X-GitHub-Event` headers.

Reachability is where this feature pulls against the rest of Athena's deployment
story. A public forge such as `github.com` cannot dial a private tailnet, while
the supported `athena-serve` contract has no public or proxy-terminated mode.
Athena also cannot detect that a tunnel, reverse proxy, NAT rule, container
publication, or Tailscale Funnel has exposed an otherwise permitted listener.
Consequently, receiving events from a public forge is **not a supported release
shape today**.

A release owner may eventually design a separately reviewed edge exception that
forwards only the exact forge route, preserves the preflighted `Host`, enforces
its own connection/body/rate limits, and exposes no other Athena surface. The
route hardening below is necessary for that design, but it is not approval to
deploy one. A self-hosted forge on the same local/tailnet boundary needs no such
exception.

`PUT /event-sources/{id}/enabled` pauses acceptance **without rotating the
secret**, so silencing a noisy forge does not mean re-registering the webhook on
the far side. `DELETE` revokes it — and **keeps the history it already landed**,
because those events happened and were authentic when recorded. Revoking a
credential is not a reason to rewrite the trail.

## What arrives, and what happens to it

A **closed vocabulary**. Three event kinds, nothing else:

| GitHub event | Becomes | Landed when it names an issue in |
|---|---|---|
| `push` | `forge_commit` | a commit message, or the branch name |
| `pull_request` (opened / closed / merged / reopened) | `forge_pull_request` | the PR title, or its head branch |
| `create` (`ref_type: branch`) | `forge_branch` | the branch name |

Anything else — a label change, a review request, a tag, a workflow run — is
accepted with **202 and no effect**. Authentic and uninteresting is not an error,
and must not look like one. A tracker that absorbed every event shape a forge
invents would become a second, worse copy of the forge.

**A merged pull request is distinguishable from a closed one.** GitHub has no
"merged" action; a close carries a `merged` flag, and that difference is the
entire point of the event.

### Key matching

An event lands on an issue when it names that issue's key (`ATH-12`). Keys are
found in commit messages, branch names, and PR titles — the branch matters
because teams that name branches `ATH-12-thing` routinely write commit messages
that never repeat the key.

**Extraction proposes; the database disposes.** `UTF-8`, `SHA-256`, and
`ISO-8601` are all shaped exactly like issue keys. The parser returns them as
candidates, and they are discarded unless the prefix is a **real project key**
and the number a **real issue** in it. A commit message mentioning a character
encoding cannot land on somebody's work.

An event that matches nothing is **counted, not stored**: `unmatched_count` on
the source climbs. Landing it nowhere would be silent; storing it somewhere would
be noise on a trail that belongs to work. A climbing number means something real
— the forge is busy on work this workspace does not track.

## Why imported is the whole design

Every landed event is written as **imported history** (`imported_at` set,
migration 0041). That single decision is what makes the feature safe, because
every native-only mechanism *already* excludes imported rows:

| Mechanism | Guard |
|---|---|
| Undo | refuses with `undo_imported_event` |
| Lifecycle facts (0055) | `imported_at IS NULL` |
| Claim handoffs (0058) | `imported_at IS NULL` |
| Assignee facts (0068) | `imported_at IS NULL` |
| Fleet metrics | `imported_at IS NULL` |
| Attention rollup, security refusal counters | `imported_at IS NULL` — **added after the adversarial review** (Wave H-0); this table claimed the guard before it existed. A back-dated import could otherwise plant fake refusals on `/admin/security` and inflate the attention card |
| Automation scan | `native_only=True` → `imported_at IS NULL` — **added after the adversarial review** (Wave H-0); a wildcard rule firing on an imported event is how the review moved an issue's status |

This table is not maintained by hand: `tests/test_imported_at_guards.py` pins
its rows to `scripts/check_imported_at_guards.py`, which fails the build when a
listed guard disappears from the code — and when any *new* reader of the
activity table appears that is neither guarded nor explicitly exempted.

So a forge **cannot move an issue's status**, cannot shift a completion-cycle
median, cannot be undone into a native write, and cannot appear as agent
throughput. A merged PR saying "closes ATH-12" is recorded as *the source said
so* — it does not close anything. Athena takes evidence from a forge, never
instructions.

Imported rows also carry **no run coordinates**. A forge event belongs to no
Athena run, so it can never be spliced into one's replay. That neutralization
lives in `activity.record` itself, not in the caller, so forgetting it is not
possible.

**Attribution.** The actor is the operator who *registered the source*. A forge
event has no Athena identity behind it — whoever pushed may have no account here
— and inventing a synthetic user would put a fictional name on the trail. The
honest reading is: *you allowed this channel, and this came through it.*

## Refusals

- **Bad signature, missing signature, or unknown source → identical 401.**
  Answering "no such source" would make the endpoint a directory of your
  integrations for anyone who can reach it.
- **The signature is verified before the payload is parsed.** The handler takes
  the raw request and declares no body model, because a declared model would put
  FastAPI's JSON parsing and validation *in front of* authentication on a
  publicly reachable route. Bound the size, read the bytes, verify, then parse.
- **Paused source → 403.** The credential is good; the answer is "not right now".
- **Payload contradicting its own header → 422** (a `push` with no `commits`).
- **Oversized body → 413**, refused on `Content-Length` where possible and on
  actual length always.

## Limits, stated

- **No backfill, ever.** Only what was delivered while the source was enabled.
- **Rate limited before credential or body work.** Each delivery attempt is
  charged against the shared per-IP limiter
  (`ATHENA_ANON_RATE_LIMIT_PER_MINUTE`, off by default) **before** the source is
  looked up or the body is read — so unsigned bursts, including against unknown
  or paused source names, get a 429 instead of free HMAC work and database
  lookups. `athena-serve` requires this limit to be positive in tailnet mode; any
  future public edge exception would need both a positive Athena limit and an
  independently bounded edge. This setting also covers optional-identity REST
  reads; it is not a global browser-request ceiling.
- **512 KB** per delivery, **20 commits** examined per push, **10 issues** landed
  per delivery. Overflow counts as unmatched, so the operator still sees that
  something arrived and did not land.
- **One forge dialect** (`github`). The vocabulary is closed at registration, so
  a request can never arrive for a parser that does not exist.
- **Links render only for registered hosts.** A URL in a forge detail becomes a
  clickable link only when its host belongs to a registered source — otherwise
  anyone holding a source secret could plant an arbitrary outbound link on an
  issue's trail. Links carry `rel="noopener noreferrer nofollow"`.
- **The issue detail page renders forge links.** Other activity surfaces
  (dashboard, run lineage, space trails) show the URL as inert text: they do not
  pass the registered-host set, and the renderer degrades to plain text rather
  than guessing. That is a deliberate default — a surface opts in.
- **No signature replay window.** A replayed delivery with a valid signature will
  land again. GitHub's shape has no nonce Athena could key on, and inventing a
  dedupe key from the payload would silently drop legitimate repeats (a force-push
  re-reporting the same commit). Duplicates on the trail are visible and harmless;
  silent loss would not be.
