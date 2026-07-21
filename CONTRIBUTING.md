# Contributing to Athena

Athena is a local-alpha, self-hosted operator workspace for one human directing
an AI fleet. Contributions are welcome when they strengthen that specific
product rather than broaden it into an enterprise work-management suite.

Before changing code, read [`AGENTS.md`](AGENTS.md),
[`docs/VISION.md`](docs/VISION.md), and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). They are the contribution
contract, product north star, and design of record.

## Development setup

Athena requires Python 3.12 or newer. From a clean checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install \
  -c constraints/ci-py312.txt -e ".[dev,mcp]"
.venv/bin/python -m ruff check .
.venv/bin/python scripts/check_import_contracts.py
scripts/coverage.sh
.venv/bin/python scripts/smoke_app.py
```

The constraints file reproduces the supported Linux/Python 3.12 CI graph. It
is a CI snapshot, not a promise that every supported platform uses identical
wheels.

The line, branch, and combined coverage floors are ratchets: they may hold or
increase as code changes. Never lower a floor to make a branch pass; any
justified recalibration is a separate, explicitly reviewed policy change.

To inspect a populated local instance without inventing data in the web layer:

```bash
athena-demo --db /tmp/athena-review.db
```

The demo binds to loopback, seeds only a new database, prints its disposable
login, and starts Athena. It refuses to overwrite an existing path.

## Change workflow

1. Branch from current `main` using `kevin/<topic>`, `codex/<topic>`,
   `claude/<topic>`, or `grok/<topic>`.
2. Keep one logical change in the branch.
3. Add tests that explain the invariant being protected.
4. Run Ruff, import contracts, the full-source branch-coverage gate, and the
   real-process smoke test.
5. Open a pull request using the repository template. State the user-visible
   outcome, boundaries, verification, and any AI assistance.

Never push directly to `main`. Do not rewrite another contributor's branch or
hide unrelated work in a formatting or dependency update.

## Architecture guardrails

- The database is the sole data owner. The Jinja/HTMX web layer never carries a
  second copy of product data.
- New writes go through one framework-neutral command shared by REST and web;
  MCP reaches the same command through REST.
- A command owns authorization, validation, mutation, derived state, and the
  audit event in one transaction.
- Visibility checks fail closed. Private project, space, and activity facts
  must not leak through nested resources, counts, errors, or search.
- Athena remains one-process and SQLite-first for its stated solo-operator and
  tiny-team scale.
- Agent operations are scoped, attributable, bounded, and explicit about what
  cannot yet be reversed.

The current command migration is documented in
[`docs/COMMAND_MIGRATION.md`](docs/COMMAND_MIGRATION.md). Existing legacy
write pairs are migration debt, not examples for new work.

## AI-assisted contributions

AI assistance is welcome and must be disclosed. Preserve co-author metadata
when appropriate, summarize what the model did, and identify the human-owned
decision and verification boundary. See
[`docs/AI_DEVELOPMENT.md`](docs/AI_DEVELOPMENT.md).

Generated volume is not evidence. A green PR still needs a coherent scope,
reviewable reasoning, real execution, and tests that would fail if the intended
contract regressed.

## Security reports

Do not open a public issue for a suspected vulnerability. Follow
[`SECURITY.md`](SECURITY.md) so credentials, exploit details, and private data
stay out of the public tracker.
