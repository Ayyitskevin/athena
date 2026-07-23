# Release readiness evidence

This page records evidence for the `sol/athena-production-readiness` review. It
is a checklist, not a declaration that Athena is production-ready. The supported
deployment remains one Python 3.12 process/worker on a trusted local machine or
tailnet; direct public-internet, hostile multi-tenant, multi-process, and HA use
remain outside the security claim.

## Decision

**Status: `HOLD` for a public production release; `PASS` for draft-PR review of
the bounded local/tailnet readiness uplift.**

All required repository gates must pass locally and on hosted GitHub Actions at
the exact PR head before this branch can advance. The remaining blockers below
are not waived by green tests.

## Baseline (2026-07-23)

Base commit: `b8a929d96d1ba4cef941b1819629370d43de236e` (`origin/main` at task start).
Environment: Ubuntu, CPython 3.12.3, exact `constraints/ci-py312.txt` graph.

| Check | Baseline evidence | Status |
|---|---|---|
| Dependency metadata/freeze | `pip check`; exact freeze diff | PASS |
| Ruff lint | `ruff check .` | PASS |
| Ruff formatting | `ruff format --check .` reported 259 files needing format | FAIL |
| Import contracts | 115 runtime modules, no forbidden dependencies | PASS |
| Whole-runtime typing | 243 mypy errors in 49 files across 115 runtime modules | FAIL |
| Required pytest suite | 1,918 passed in 180.53s; no skips reported by `-ra` | PASS |
| Coverage | line 92.37%; branch 81.15%; combined 89.81%; 2 excluded lines | OBSERVED, not enforced |
| Distribution path | one sdist and wheel; 58 migrations, 4 static assets, 45 templates; installed-wheel boot outside checkout | PASS |
| Hosted evidence for this branch | Branch did not yet exist remotely | NOT RUN |

## Final local gate

The commands below are the required, reproducible local release gate. Coverage
evidence is written to a fresh directory outside the checkout; packaging builds a
wheel from the extracted sdist and boots that installed wheel from `/tmp`.

```bash
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze --exclude-editable \
  | diff -u constraints/ci-py312.txt -
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/athena
.venv/bin/python scripts/check_import_contracts.py
scripts/coverage.sh /tmp/athena-final-coverage
# Then run the sdist-derived wheel recipe in CONTRIBUTING.md.
```

| Check | Final evidence | Status |
|---|---|---|
| Dependency metadata/freeze | `pip check` reported no broken requirements; exact constrained freeze diff was empty | PASS |
| Ruff lint/format | `ruff check .` passed; all 307 files already formatted | PASS |
| Import contracts | 116 runtime modules; no forbidden dependencies | PASS |
| Whole-runtime typing | mypy reported no issues in all 116 runtime source files | PASS |
| Required pytest suite | 2,022 passed in 246.90s | PASS |
| Coverage | line 13,132/14,201 = 92.47236%; branch 3,406/4,180 = 81.48325%; combined 16,538/18,381 = 89.97334%; 2 excluded lines | PASS |
| Distribution manifests | exactly one sdist and one sdist-derived wheel; checkout and extracted-source verification each found 58 migrations, 4 static assets, and 45 templates | PASS |
| Installed-wheel external boot | wheel installed with no dependency breakage; import resolved inside site-packages and process smoke passed from `/tmp`; editable install then restored with an exact freeze | PASS |
| Required-test skips | `pytest -ra` reported no skips; static skip/importorskip scan found none | PASS |
| Hosted GitHub Actions, exact PR head | Pending draft PR and exact-head Actions run | NOT RUN |

Final local evidence time: `2026-07-23T14:36:27Z`.
Final artifact SHA-256 values:

- sdist: `13c42ec336b84aa76656cc540099b3597958378db8e727abf0b4871086dc1026`
- sdist-derived wheel: `b627ab80400f4379b1ebb0ef37b49bf6f5706c65edb357ed23aee0c5cef2d48a`

## Operational and security evidence

| Area | Evidence | Status |
|---|---|---|
| Configuration | Strict booleans, finite/ranged numerics, known log levels, and all-or-none OIDC settings; malformed configuration aborts startup | PASS |
| Schema/readiness | Contiguous packaged migrations, exact applied prefix and SHA-256 ledger checks; `/readyz` fails closed without leaking detail | PASS |
| Restore | Candidate `quick_check`, private stages, file/directory sync, atomic replacement, sidecar-cleanup rollback, retained recovery on double failure | PASS |
| Attachments | Private atomic publication, metadata+audit transaction, no-follow descriptor download, observable attempt-all cleanup, deterministic reconciliation | PASS |
| Exports | Private unique JSON stage, file/directory sync, atomic replace, bounded portability snapshot | PASS |
| Cache policy | Cookie/session/authenticated/download/mutation responses use private/no-store policy while preserving `Vary` | PASS |
| Lifecycle | Both background tasks are cancelled and awaited; non-cancellation failures surface | PASS |
| Workflow permissions | Top-level CI `contents: read`; OpenCode job permissions scoped; checkout credentials disabled | PASS |
| Direct action references | Every repository workflow `uses:` reference is pinned to a full commit SHA; actionlint passes | PASS |
| Dependency/security scan | No reproducible scanner is pinned in the repository contract | NOT RUN |
| GitHub security settings | Private vulnerability reporting, Dependabot security updates, secret scanning, and push protection were disabled when inspected; settings changes are explicitly out of scope | NOT CONFIGURED |
| Guard screening | No separate input/output guard artifact accompanied the upstream task | NOT VERIFIED |

## Remaining blockers and residual risk

- The pinned `anomalyco/opencode` composite action resolves the latest OpenCode
  release at runtime, invokes `actions/cache@v4` by a mutable tag, and pipes an
  unpinned remote installer into Bash. The repository reference itself is
  immutable, but its transitive execution is not reproducible.
- Public-internet exposure, hostile multi-tenancy, multiple workers/processes,
  leader election, and HA recovery are unsupported. Rate limits are in-process.
- Complete attachment recovery still requires an operator-created matched
  database-plus-directory snapshot. Reconciliation detects but does not repair
  missing, tampered, or orphaned blobs.
- Some command-boundary migration debt remains (documented in
  [`COMMAND_MIGRATION.md`](COMMAND_MIGRATION.md)); the idempotency receipt and
  domain mutation also remain separate transactions with explicit indeterminate
  outcomes.
- Legacy databases created before migration checksums are introduced must trust
  the installed package when their ledger is first backfilled. Preserve a trusted
  package/archive and a matched pre-upgrade recovery pair.
- No SBOM, artifact signature, provenance attestation, tagged release, or GitHub
  release exists. Repository vulnerability/security automation is not enabled.
- This review used synthetic temporary databases only. It did not deploy Athena,
  change repository settings, exercise real operator data, or test disaster
  recovery on a second host.

## Promotion checklist

- [x] Baseline recorded before implementation.
- [x] Required gates encoded in CI without lowering observed coverage.
- [x] Focused regressions cover each operational behavior change.
- [x] Documentation separates implemented guarantees from unsupported modes.
- [x] Final local evidence placeholders above replaced with observed results.
- [ ] Draft PR opened without merging.
- [ ] Hosted CI succeeds at the exact final PR head.
- [ ] Remaining public-production and supply-chain blockers are resolved or
      explicitly accepted by a human release owner.
