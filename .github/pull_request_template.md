## Outcome

<!-- What user/operator outcome does this change produce? -->

## Why

<!-- State the problem, invariant, or evidence that motivated the change. -->

## Boundaries

<!-- What deliberately did not change? Note migrations, auth, schema, API, or deployment effects. -->

## Verification

- [ ] `ruff check .`
- [ ] `python scripts/check_import_contracts.py`
- [ ] `scripts/coverage.sh` with no skipped tests presented as passing
- [ ] `python scripts/smoke_app.py`
- [ ] Relevant artifact, security, migration, or UI checks

## Risk and rollback

<!-- Identify the highest-risk assumption and how to detect/reverse a bad result. -->

## AI assistance

<!-- Name material AI assistance and the human-owned decision/verification boundary. -->
