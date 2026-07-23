# Contributing to Athena

Athena is a local-alpha, self-hosted operator workspace for one human directing
an AI fleet. Contributions are welcome when they strengthen that specific
product rather than broaden it into an enterprise work-management suite.

Before changing code, read [`AGENTS.md`](AGENTS.md),
[`docs/VISION.md`](docs/VISION.md), and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). They are the contribution
contract, product north star, and design of record.

## Development setup

Athena supports Python 3.12 (`>=3.12,<3.13`). It is the only Python version
verified in CI. From a clean checkout, run the required gate:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install \
  -c constraints/ci-py312.txt -e ".[dev,mcp]"
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze --exclude-editable \
  | diff -u constraints/ci-py312.txt -
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/athena
.venv/bin/python scripts/check_import_contracts.py
scripts/coverage.sh
```

The constraints file is the exact verified Linux/Python 3.12 CI graph. The
freeze diff rejects dependency drift; `pip check` rejects incompatible
installed metadata. `scripts/coverage.sh` runs the complete test suite with
full-source branch coverage, writes evidence outside the checkout, and enforces
the floors configured in `pyproject.toml`.

### Packaging and outside-checkout smoke

CI's final gate validates distribution artifacts instead of importing Athena
from the checkout. Run this Bash recipe from the repository root after the gate
above:

```bash
set -euo pipefail
repo_root="$(pwd -P)"
dist_root="$(mktemp -d "${TMPDIR:-/tmp}/athena-dist.XXXXXX")"

.venv/bin/python -m build --sdist --outdir "$dist_root/artifacts" .

shopt -s nullglob
sdists=("$dist_root"/artifacts/*.tar.gz)
if (( ${#sdists[@]} != 1 )); then
  echo "expected exactly one source distribution" >&2
  exit 1
fi
mkdir -p "$dist_root/source"
.venv/bin/python -c \
  'import pathlib, sys, tarfile; tarfile.open(sys.argv[1]).extractall(pathlib.Path(sys.argv[2]), filter="data")' \
  "${sdists[0]}" "$dist_root/source"
source_trees=("$dist_root"/source/athena-*)
if (( ${#source_trees[@]} != 1 )); then
  echo "expected exactly one extracted source tree" >&2
  exit 1
fi
.venv/bin/python -m build --wheel \
  --outdir "$dist_root/artifacts" \
  "${source_trees[0]}"
wheels=("$dist_root"/artifacts/*.whl)
if (( ${#wheels[@]} != 1 )); then
  echo "expected exactly one wheel built from the source distribution" >&2
  exit 1
fi
if [[ ! -f "${source_trees[0]}/scripts/verify_wheel.py" ]] \
  || [[ ! -f "${source_trees[0]}/scripts/smoke_app.py" ]] \
  || [[ ! -f "${source_trees[0]}/scripts/check_import_contracts.py" ]]; then
  echo "source distribution is missing its verification helpers" >&2
  exit 1
fi

.venv/bin/python scripts/verify_wheel.py "${wheels[0]}"
.venv/bin/python \
  "${source_trees[0]}/scripts/verify_wheel.py" \
  "${wheels[0]}"
.venv/bin/python \
  "${source_trees[0]}/scripts/check_import_contracts.py"
sha256sum "${sdists[0]}" "${wheels[0]}"

.venv/bin/python -m pip uninstall --yes athena
.venv/bin/python -m pip install --no-deps "${wheels[0]}"
.venv/bin/python -m pip check
(
  cd /tmp
  "$repo_root/.venv/bin/python" -c \
    'from pathlib import Path; import athena, sys; assert Path(athena.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), athena.__file__'
  "$repo_root/.venv/bin/python" \
    "${source_trees[0]}/scripts/smoke_app.py"
)
```

This intentionally replaces the editable Athena install in `.venv` with the
built wheel. Re-run the constrained editable install before continuing normal
development. The temporary distribution directory remains outside the checkout
as inspectable evidence.

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
4. Run the complete required local gate above.
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
