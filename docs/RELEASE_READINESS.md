# Release readiness evidence

This page records the evidence behind the `0.1.0a1` milestone. It is a checklist,
not a declaration that Athena is production-ready. The supported deployment
remains one Python 3.12 process/worker on a trusted local machine or tailnet;
direct public-internet, proxy-terminated, hostile multi-tenant, multi-process,
and HA use remain outside the security claim. The forge delivery route
(`POST /forge/{source_name}`) is hardened for untrusted signed input, but Athena
does not yet ship a supported public edge that makes it reachable from a
public forge; see [FORGE.md](FORGE.md).

## Decision

**Status: `HOLD` for a public production release; `PASS` for the local/tailnet
alpha the project actually claims to be.**

Re-affirmed 2026-07-30 after the Stage M–P expansion, the adversarial review of
the merged tree (grade 7/10 — see OPUS_REMEDIATION_GUIDE_ATHENA.md), and the
H-0/H-1/H-2 remediation waves that followed it. Every required repository gate
passes locally at the named commit. The remaining blockers below are **not
waived by green tests** — they are supply-chain, deployment-shape, and
repository-settings items, and accepting them is a human release owner's
decision, not something a test can make.

### 2026-07-31 deployment-hardening candidate

The unmerged `codex/deployment-preflight-hardening` candidate narrows the
supported runtime to an executable contract rather than a runbook convention:
`athena-serve` permits only direct numeric loopback or explicit Tailscale binds,
preflights exact `Host` authorities and durable administrator recovery, refuses
legacy actor-header trust and bootstrap credentials during normal startup, fixes
Uvicorn to one worker with proxy-header trust disabled, and installs the same
socket/authority boundary outside all application work. It rejects oversized
requests before cookie-controlled SQLite work and requires the live logical
schema to equal the packaged migration result. Bootstrap is loopback-only,
rehearses migration on an in-memory copy before writing the real file, and
requires the first administrator to set a bounded password before the normal
launcher can succeed.

Implementation commit:
`1596270e408692760f01828fbd089bcb8fe1b78c`
(`codex/deployment-preflight-hardening`).
Environment: Linux, CPython 3.12.3, exact
`constraints/ci-py312.txt` graph in fresh venv
`/tmp/athena-deployment-gate.K9CbwR/venv`.
Local evidence time: `2026-07-31T08:26Z`.

The exact required gate passed: dependency check and freeze diff, Ruff check and
format check (374 files), mypy (149 source files), all three architecture
checkers, and 2,833 tests. Coverage was 92.76827% line
(17,446/18,806), 83.03030% branch (4,658/5,610), and 90.53080%
combined (22,104/24,416), above every configured floor. The one previously
documented FastAPI/Pydantic alias warning remains.

The sdist-derived wheel was verified from both the checkout and extracted source:
69 migrations, 4 static assets, 49 templates, the required console script, and
149 import-contract modules. After installation outside the checkout, it passed
the real two-lifecycle bootstrap/restart smoke and the 22-step HTTP field
exercise. Artifact SHA-256:

- sdist: `d622cfe47df1e6ef231dd2c2b33b37e0eec0130978f23f3efcec6ceab6f61d07`
- wheel: `a946bb1733b635ac44d17806e38b3545550b690fa5dcc2e05b1f25a9058793d5`

Independent architecture and adversarial reviews both returned `PASS` with no
blocking finding. The adversarial authority matrix found no parser acceptance
among 69,156 libc legacy-IPv4 forms and no mismatch across 18,294 accepted DNS
names checked against Node's WHATWG URL parser. Exact-head Actions run
[`30616489205`](https://github.com/Ayyitskevin/athena/actions/runs/30616489205)
then passed every required step on evidence commit
`ef1b91e82bf2b21bb2d9b47636dee4b2f32ff869` in 17m14s, including coverage
evidence upload, the sdist-derived wheel build/install, and the external wheel
boot. That historical run emitted one non-failing maintenance annotation because
the then-pinned `actions/upload-artifact` v4.6.2 revision declared a Node 20
runtime and GitHub forced it onto Node 24. This candidate replaces it with the
official, signature-verified v6.0.0 commit
`b7c566a772e6b6bfb58ed0dc250532a479d7789f`, whose immutable `action.yml`
declares `node24`; the existing artifact name, path, missing-file policy, hidden
file inclusion, retention, and workflow permissions are unchanged. Exact
PR-head CI remains the behavioral proof for both the uploaded archive and the
absence of that annotation. Neither local nor hosted evidence authorizes merge
or release. The public-release decision remains `HOLD`: Athena cannot observe
whether a proxy, tunnel, NAT rule, container publication, or Tailscale Funnel
exposes an otherwise allowed listener.

## Evidence (2026-07-30, refreshed)

This run supersedes the Stage L evidence (2026-07-26, quoted below for the
record) — the tree has since gained Stages M–P (query grammar, live embeds,
knowledge graph, forge integration), the adversarial-review remediation waves
H-0/H-1, and wave H-2's `POST /labels` duplicate-race fix.

Commit: `2ed40b29485758acb5b0e48c4db0d000aacc966b` (`kimi/wave-h2-docs-reconciliation`;
the branch adds the H-2 documentation reconciliation and the `POST /labels`
409-on-race fix in the working tree).
Environment: Linux, CPython 3.12.13, exact `constraints/ci-py312.txt` graph in a
fresh venv (`/tmp/athena-gate-venv`).
Local evidence time: `2026-07-30T15:47Z`.

### Required gate

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze --exclude-editable \
  | diff -u constraints/ci-py312.txt -
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/athena
.venv/bin/python scripts/check_import_contracts.py
.venv/bin/python scripts/smoke_app.py
.venv/bin/python scripts/field_exercise.py
ATHENA_PYTHON=.venv/bin/python scripts/coverage.sh /tmp/athena-h2-coverage
# Then the sdist-derived wheel recipe in CONTRIBUTING.md.
```

| Check | Observed evidence | Status |
|---|---|---|
| Dependency metadata | `pip check`: no broken requirements | PASS |
| Dependency freeze | exact constrained freeze diff empty | PASS |
| Ruff lint | `ruff check .` passed | PASS |
| Ruff formatting | 368 files already formatted | PASS |
| Whole-runtime typing | mypy: no issues in 148 source files | PASS |
| Import contracts | 148 runtime modules, no forbidden dependencies | PASS |
| Required suite | 2,633 passed, 0 skipped (`-ra` reports none) — the Stage L suite plus Stages M–P, the H-0/H-1 regressions, and the `POST /labels` duplicate-race regression added in this wave | PASS |
| Coverage (enforced) | line 16,730/18,022 = 92.83098% (floor 92.60); branch 4,401/5,306 = 82.94384% (floor 82.30); combined 21,131/23,328 = 90.58213% (floor 90.30); 2 excluded lines | PASS |
| Process smoke | fresh database, no-data metrics/active-work contracts, packaged assets, bounded stop | PASS |
| Field exercise | 22 steps over real loopback HTTP against a real reference executor, from the checkout **and** from the extracted sdist | PASS |
| Test warnings | 1 warning, accounted for below | ACCOUNTED |

### Distribution

| Check | Observed evidence | Status |
|---|---|---|
| Artifact counts | exactly one sdist and one sdist-derived wheel | PASS |
| Wheel runtime manifest | verified from the checkout **and** from the extracted sdist: 69 migrations, 4 static assets, 49 templates | PASS |
| Import contracts from sdist | 148 modules, no forbidden dependencies | PASS |
| sdist helper completeness | `scripts/` **and** `examples/` present; the field exercise runs from the extracted tree | PASS |
| Installed-wheel external boot | wheel installed with no dependency breakage; `athena.__file__` resolved inside `site-packages`; process smoke passed from `/tmp`; constrained editable install restored with an exact freeze | PASS |

Artifact SHA-256 (this run):

- sdist: `636fd30c3984f058ceddc6cea73ebee2b9d50330728a0d92e5dc7efee8e2b947`
- sdist-derived wheel: `39277d80031f8395162b6ec00b19e4ce8114d28782023fd913ca2f5efb0dc877`

Artifact hashes are inputs to a build, not a supply-chain guarantee: nothing here
is signed or attested (see blockers).

One environment note, for honesty rather than alarm: the long-lived `.venv312`
development venv has **drifted** from the constrained graph (it carries extra
`mcp-types` and `opentelemetry-api` installs, and `uv pip freeze` normalizes
distribution names differently from `pip freeze`). The gate above was therefore
run in a fresh venv built exactly as CI builds it, where the freeze diff is
empty. Recreate stale development venvs rather than trusting them for evidence.

## Evidence (2026-07-26, Stage L — superseded)

Commit: `091528f4d7438cc359c288b7324dace88960f528` (`origin/main` at Stage L start;
this branch adds only the packaging fix and this document).
Environment: Linux, CPython 3.12, exact `constraints/ci-py312.txt` graph.
Local evidence time: `2026-07-26T19:42:51Z`.

### What this gate caught

The gate is not ceremony — running it found a defect that every prior stage's
green suite had missed. `MANIFEST.in` shipped `scripts/` but not `examples/`, so
the **source distribution carried `scripts/field_exercise.py` while omitting the
`examples/icarus_executor.py` it spawns**: the release gate was unrunnable from a
source distribution. Fixed on this branch, and pinned by a test that asserts
`MANIFEST.in` ships whatever directory the exercise spawns from. The exercise now
passes from an extracted sdist, which is the evidence recorded above.

The 2026-07-30 re-run caught no new defect — it exists because four stages of
code landed after the first run, not because the gate found something. The
defects found *between* the runs were found by the adversarial review and its
remediation waves (H-0/H-1, and H-2's `POST /labels` duplicate-race 500), each
with its regression test, not by re-running this gate.

### Accounted-for test warning

One warning, from a pinned third-party interaction, not from Athena. Observed in
the 2026-07-30 run as:

```
tests/test_agent_budgets.py::test_concurrent_writes_cannot_both_spend_the_last_unit
pydantic 2.13.4 / _generate_schema.py: UnsupportedFieldAttributeWarning
  The 'alias' attribute with value 'X-Athena-Actor' was provided to the
  `Field()` function, which has no effect in the context it was used.
```

FastAPI 0.139.0 declares `authorization: str | None = Header(default=None)` and
`x_athena_actor: str | None = Header(default=None, alias="X-Athena-Actor")` on
Athena's actor-resolution dependencies (`core/identity.py`); pydantic warns
because the alias reaches its schema builder attached to a union member, where
pydantic itself would ignore it. (Which of the two Header aliases the warning
names has varied between runs — at Stage L it was `authorization`; the mechanism
is the same.) **FastAPI, not pydantic, applies that alias**, and the behavior
is verified rather than assumed: bearer credentials resolve their agent, the
actor header resolves only when explicitly trusted, and an invalid bearer is
refused with 401, across the whole suite. The warning is recorded here instead
of being silenced with a `filterwarnings` entry, because suppressing it would
hide the same class of warning if it ever appeared on a field Athena does own.

## Operational and security evidence

| Area | Evidence | Status |
|---|---|---|
| Configuration | Strict booleans, finite/ranged numerics, known log levels, all-or-none OIDC; malformed configuration aborts startup | PASS |
| Supported deployment | Candidate `athena-serve` contract: direct local/tailnet bind policy, exact accepted-socket and `Host` boundary, fixed single-worker server settings, durable-admin recovery, installed two-process bootstrap/restart smoke, independent architecture/adversarial review, and exact-head Actions `30616489205` | PASS |
| Schema/readiness | 69 contiguous packaged migrations, exact applied prefix and SHA-256 ledger checks; deployment doctor/launcher additionally require exact logical-schema equality; bootstrap dry-runs migration before the real write; `/readyz` fails closed without leaking detail | PASS |
| Restore | Candidate `quick_check`, private stages, file/directory sync, atomic replacement, sidecar-cleanup rollback, retained recovery on double failure | PASS |
| Attachments | Private atomic publication, metadata+audit transaction, no-follow descriptor download, observable attempt-all cleanup, deterministic reconciliation | PASS |
| Exports | Private unique JSON stage, file/directory sync, atomic replace, bounded portability snapshot | PASS |
| Cache policy | Cookie/session/authenticated/download/mutation responses use private/no-store policy while preserving `Vary` | PASS |
| Lifecycle | Both background tasks are cancelled and awaited; non-cancellation failures surface | PASS |
| Egress | SSRF guard (scheme, address class, DNS pin, no redirects) on every outbound POST; private-address egress is opt-in per exact hostname via `ATHENA_EGRESS_PRIVATE_HOSTS`, empty by default | PASS |
| Forge inbound | Signature verified before the payload is parsed on a route that declares no body model; bad/missing signature and unknown source collapse to one 401 (unknown sources pay the same HMAC work); the anonymous per-IP limiter is charged before any source lookup or body read; a closed three-event vocabulary; landed events are imported history, excluded from undo, lifecycle facts, handoffs, assignee facts, fleet metrics, the attention rollup, security counters, and the automation event scan | PASS |
| Command ownership | Every durable write in the Aegis project surface has a command owner; remaining debt — including transport-side authorization on the mentor page/comment, issue-comment, and event-source commands — is enumerated in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md) | PASS |
| Workflow permissions | Top-level CI `contents: read`; job permissions scoped; checkout credentials disabled | PASS |
| Direct action references | Every workflow `uses:` reference pinned to a full commit SHA; actionlint passes | PASS |
| Dependency/security scan | No reproducible scanner is pinned in the repository contract | NOT RUN |
| GitHub security settings | Private vulnerability reporting, Dependabot security updates, secret scanning, and push protection were disabled when last inspected; settings changes are out of scope for a code change | NOT CONFIGURED |
| SBOM / signing / provenance | None produced | NOT RUN |

## Remaining blockers and residual risk

These are the reasons the decision above is `HOLD` for public production. None is
resolved by a green test run.

- **Supply chain.** No SBOM, artifact signature, provenance attestation, or
  published release exists. The pinned `anomalyco/opencode` composite action
  resolves the latest OpenCode release at runtime, invokes `actions/cache@v4` by a
  mutable tag, and pipes an unpinned remote installer into Bash — the repository
  reference is immutable, its transitive execution is not reproducible.
- **Repository security automation is off.** Private vulnerability reporting,
  Dependabot, secret scanning, and push protection were disabled when inspected.
- **Deployment shape.** The supported launcher now fails closed to direct
  loopback or explicit tailnet listeners, but it cannot infer external
  publication. Public-internet exposure, proxy termination, hostile
  multi-tenancy, multiple workers/processes, leader election, and HA recovery
  remain unsupported. Rate limits are in-process.
- **The project has an unauthenticated signed-inbound route** (Stage P).
  `POST /forge/{source_name}` is engineered to receive stranger-controlled bytes,
  but the repository does not yet provide a supported public edge for it. If a
  release owner creates an external exception, HMAC verification becomes the
  route's gate and the assumptions are substantial: there is deliberately no
  signature replay window (a valid redelivery lands again), and event-source
  secrets are stored **plaintext** because HMAC needs the shared value — a
  database leak that would expose only token hashes elsewhere exposes live source
  secrets here. The adversarial review already broke two documented guarantees
  by execution (an enumeration oracle; automation firing on imported history);
  both are fixed in wave H-0 with regression tests, but public operation has not
  had a supported deployment review.
- **Authorization still lives in some transports.** The mentor page and
  page-comment commands, the issue-comment commands, and the event-source
  commands take a bare actor id and trust the route's guards; the command owns
  the write and its audit, not the gate (enumerated in
  [COMMAND_MIGRATION.md](COMMAND_MIGRATION.md)). Every shipped transport applies
  the checks — the debt is that a *future* caller of those commands inherits no
  enforcement, and the migration rules say the gate belongs inside.
- **Stages M–P landed after the original gate run.** The query grammar, live
  embeds, the knowledge graph, and the forge integration were built after the
  2026-07-26 evidence below was recorded, and the 7/10 adversarial review is the
  only hostile pass they have had. The review's verified defects are fixed
  (waves H-0/H-1, each with its regression test) and the documentation drift it
  named is reconciled (wave H-2, including this page), but "fixed once, reviewed
  once" is not "hardened".
- **Attachment recovery** still requires an operator-created matched
  database-plus-directory snapshot. Reconciliation detects but does not repair
  missing, tampered, or orphaned blobs.
- **Idempotency receipts and domain mutations remain separate transactions** with
  explicit indeterminate outcomes.
- **Legacy databases** created before migration checksums must trust the installed
  package when their ledger is first backfilled. Preserve a trusted
  package/archive and a matched pre-upgrade recovery pair.
- **The executor contract has exactly one implementation.** Dispatch is proven
  against `examples/icarus_executor.py`, written alongside it. A contract with one
  counterparty is usually still wrong in ways only a second one reveals.
- **No production deployment has occurred.** All evidence here comes from
  synthetic temporary databases and loopback processes. No real operator data, no
  second host, no disaster-recovery rehearsal.

## Promotion checklist

- [x] Evidence recorded against a named commit and environment.
- [x] Required gates encoded in CI without lowering observed coverage.
- [x] Focused regressions cover each operational behavior change.
- [x] Documentation separates implemented guarantees from unsupported modes.
- [x] Final local evidence replaced with observed results.
- [x] Distribution artifacts built, verified from both trees, and booted outside
      the checkout.
- [x] Draft PR opened without merging.
- [ ] Hosted CI succeeds at the exact final PR head.
- [ ] A human release owner explicitly accepts the residual risk above and applies
      the tag:

      ```bash
      git tag -a v0.1.0a1 -m "Athena 0.1.0a1 — local alpha" <merge commit>
      git push origin v0.1.0a1
      ```

      Until that tag exists, `0.1.0a1` is an untagged milestone and the CHANGELOG
      says so.
