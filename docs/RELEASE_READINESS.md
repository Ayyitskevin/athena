# Release readiness evidence

This page records the evidence behind the `0.1.0a1` milestone. It is a checklist,
not a declaration that Athena is production-ready. The supported deployment
remains one Python 3.12 process/worker on a trusted local machine or tailnet;
direct public-internet, hostile multi-tenant, multi-process, and HA use remain
outside the security claim.

## Decision

**Status: `HOLD` for a public production release; `PASS` for the local/tailnet
alpha the project actually claims to be.**

Every required repository gate passes locally and on hosted GitHub Actions at the
exact PR head. The remaining blockers below are **not waived by green tests** —
they are supply-chain and repository-settings items, and accepting them is a
human release owner's decision, not something a test can make.

## Evidence (2026-07-26)

Commit: `091528f4d7438cc359c288b7324dace88960f528` (`origin/main` at Stage L start;
this branch adds only the packaging fix and this document).
Environment: Linux, CPython 3.12, exact `constraints/ci-py312.txt` graph.
Local evidence time: `2026-07-26T19:42:51Z`.

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
scripts/coverage.sh /tmp/athena-final-coverage
# Then the sdist-derived wheel recipe in CONTRIBUTING.md.
```

| Check | Observed evidence | Status |
|---|---|---|
| Dependency metadata | `pip check`: no broken requirements | PASS |
| Dependency freeze | exact constrained freeze diff empty | PASS |
| Ruff lint | `ruff check .` passed | PASS |
| Ruff formatting | 350 files already formatted | PASS |
| Whole-runtime typing | mypy: no issues in 135 source files | PASS |
| Import contracts | 135 runtime modules, no forbidden dependencies | PASS |
| Required suite | 2,300 passed, 0 skipped (`-ra` reports none) — 2,299 at the base commit plus the sdist-completeness guard added below | PASS |
| Coverage (enforced) | line 15,489/16,682 = 92.84858% (floor 92.60); branch 4,018/4,856 = 82.74300% (floor 82.30); combined 19,507/21,538 = 90.57016% (floor 90.30); 2 excluded lines | PASS |
| Process smoke | fresh database, no-data metrics/active-work contracts, packaged assets, bounded stop | PASS |
| Field exercise | 18 steps over real loopback HTTP against a real reference executor, from the checkout **and** from the extracted sdist | PASS |
| Test warnings | 1 warning, accounted for below | ACCOUNTED |

### Distribution

| Check | Observed evidence | Status |
|---|---|---|
| Artifact counts | exactly one sdist and one sdist-derived wheel | PASS |
| Wheel runtime manifest | verified from the checkout **and** from the extracted sdist: 68 migrations, 4 static assets, 46 templates | PASS |
| Import contracts from sdist | 135 modules, no forbidden dependencies | PASS |
| sdist helper completeness | `scripts/` **and** `examples/` present; the field exercise runs from the extracted tree | PASS (after the fix below) |
| Installed-wheel external boot | wheel installed with no dependency breakage; `athena.__file__` resolved inside `site-packages`; process smoke passed from `/tmp`; constrained editable install restored with an exact freeze | PASS |

Artifact SHA-256 (post-fix build):

- sdist: `957924c149a6ba5624938c0e0e2ebe6886dde1394cdc40b0ff6e1fd1e3f94580`
- sdist-derived wheel: `f289b5c30e5f39076ec5e70fc4f8c62ba38bb9c9696b2acc739f244ab2a223e6`

Artifact hashes are inputs to a build, not a supply-chain guarantee: nothing here
is signed or attested (see blockers).

### What this gate caught

The gate is not ceremony — running it found a defect that every prior stage's
green suite had missed. `MANIFEST.in` shipped `scripts/` but not `examples/`, so
the **source distribution carried `scripts/field_exercise.py` while omitting the
`examples/icarus_executor.py` it spawns**: the release gate was unrunnable from a
source distribution. Fixed on this branch, and pinned by a test that asserts
`MANIFEST.in` ships whatever directory the exercise spawns from. The exercise now
passes from an extracted sdist, which is the evidence recorded above.

### Accounted-for test warning

One warning, from a pinned third-party interaction, not from Athena:

```
pydantic 2.13.4 / _generate_schema.py: UnsupportedFieldAttributeWarning
  The 'alias' attribute with value 'authorization' was provided to the
  `Field()` function, which has no effect in the context it was used.
```

FastAPI 0.139.0 declares `authorization: str | None = Header(default=None)` on
Athena's actor-resolution dependencies; pydantic warns because the alias reaches
its schema builder attached to a union member, where pydantic itself would ignore
it. **FastAPI, not pydantic, applies that alias**, and the behavior is verified
rather than assumed: bearer credentials resolve their agent and an invalid bearer
is refused with 401, across the whole suite. The warning is recorded here instead
of being silenced with a `filterwarnings` entry, because suppressing it would hide
the same class of warning if it ever appeared on a field Athena does own.

## Operational and security evidence

| Area | Evidence | Status |
|---|---|---|
| Configuration | Strict booleans, finite/ranged numerics, known log levels, all-or-none OIDC; malformed configuration aborts startup | PASS |
| Schema/readiness | 68 contiguous packaged migrations, exact applied prefix and SHA-256 ledger checks; `/readyz` fails closed without leaking detail | PASS |
| Restore | Candidate `quick_check`, private stages, file/directory sync, atomic replacement, sidecar-cleanup rollback, retained recovery on double failure | PASS |
| Attachments | Private atomic publication, metadata+audit transaction, no-follow descriptor download, observable attempt-all cleanup, deterministic reconciliation | PASS |
| Exports | Private unique JSON stage, file/directory sync, atomic replace, bounded portability snapshot | PASS |
| Cache policy | Cookie/session/authenticated/download/mutation responses use private/no-store policy while preserving `Vary` | PASS |
| Lifecycle | Both background tasks are cancelled and awaited; non-cancellation failures surface | PASS |
| Egress | SSRF guard (scheme, address class, DNS pin, no redirects) on every outbound POST; private-address egress is opt-in per exact hostname via `ATHENA_EGRESS_PRIVATE_HOSTS`, empty by default | PASS |
| Command ownership | Every durable write in the Aegis project surface has a command owner; remaining debt is enumerated in [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md) | PASS |
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
- **Deployment shape.** Public-internet exposure, hostile multi-tenancy, multiple
  workers/processes, leader election, and HA recovery are unsupported. Rate limits
  are in-process.
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
