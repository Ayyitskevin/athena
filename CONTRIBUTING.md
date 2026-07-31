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
.venv/bin/python -I -m pip install \
  --only-binary :all: \
  --require-hashes -r constraints/bootstrap-py312.txt
.venv/bin/python -I -m pip install \
  -c constraints/ci-py312.txt -e ".[dev,mcp]"
.venv/bin/python -I -m pip check
.venv/bin/python -I -m pip freeze --exclude-editable \
  | diff -u constraints/ci-py312.txt -
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m mypy src/athena
.venv/bin/python scripts/check_import_contracts.py
.venv/bin/python scripts/check_write_ownership.py
.venv/bin/python scripts/check_imported_at_guards.py
scripts/coverage.sh
```

The constraints file is the exact verified Linux/Python 3.12 CI graph. The
freeze diff rejects dependency drift; `pip check` rejects incompatible
installed metadata. The three `check_*` scripts fail the build on doctrinal
drift the tests cannot see: `check_import_contracts.py` keeps the layer
direction (`web → aegis|mentor → core`), `check_write_ownership.py` keeps
transports from writing except through command modules and the designated
writers named in it, and `check_imported_at_guards.py` pins every native-only
activity reader to its `imported_at IS NULL` guard (and FORGE.md's guard
table to the checker, via `tests/test_imported_at_guards.py`).
`scripts/coverage.sh` runs the complete test suite with
full-source branch coverage, writes evidence outside the checkout, and enforces
the floors configured in `pyproject.toml`.

### Release-candidate supply-chain evidence

CI performs this gate only after the required test job passes. It uses a
separate hash-verified evidence/build environment, builds one sdist and one
wheel derived from that sdist, and installs the wheel into fresh base and MCP
runtime environments. Set `ATHENA_CANDIDATE_REPOSITORY` to the exact
`owner/repository` whose checkout you are testing, then run the same gate from
the repository root with Bash:

```bash
set -euo pipefail
: "${ATHENA_CANDIDATE_REPOSITORY:?set the exact owner/repository identity}"
if [[ -n "$(git status --porcelain)" ]]; then
  echo "candidate evidence requires a clean working tree" >&2
  exit 1
fi
repo_root="$(pwd -P)"
evidence_env="$(mktemp -d "${TMPDIR:-/tmp}/athena-evidence-venv.XXXXXX")"
evidence_root="$(mktemp -d "${TMPDIR:-/tmp}/athena-evidence.XXXXXX")"
dist_root="$(mktemp -d "${TMPDIR:-/tmp}/athena-dist.XXXXXX")"
runtime_root="$(mktemp -d "${TMPDIR:-/tmp}/athena-runtime.XXXXXX")"
evidence_python="$evidence_env/bin/python"
input_sbom="$evidence_root/athena-ci-python.cdx.json"
base_raw="$evidence_root/athena-runtime-base-pip-audit.cdx.json"
base_sbom="$evidence_root/athena-runtime-base-python312.cdx.json"
mcp_raw="$evidence_root/athena-runtime-mcp-pip-audit.cdx.json"
mcp_sbom="$evidence_root/athena-runtime-mcp-python312.cdx.json"
candidate_bundle="$evidence_root/athena-candidate"

python3.12 -I -m venv "$evidence_env"
"$evidence_python" -I -m pip install \
  --only-binary :all: \
  --require-hashes -r constraints/bootstrap-py312.txt
"$evidence_python" -I -m pip install \
  --only-binary :all: \
  --require-hashes -r constraints/security-tools-py312.txt
"$evidence_python" -I -m pip check
"$evidence_python" -I scripts/check_supply_chain.py verify-environment
"$evidence_python" -I -m pip_audit \
  --strict \
  --no-deps \
  --disable-pip \
  --progress-spinner off \
  --timeout 30 \
  --vulnerability-service pypi \
  --require-hashes \
  --requirement constraints/security-tools-py312.txt
"$evidence_python" -I scripts/check_supply_chain.py run \
  --auditor-python "$evidence_python" \
  --output "$input_sbom"
"$evidence_python" -I scripts/check_supply_chain.py verify \
  --sbom "$input_sbom"

"$evidence_python" -I -m build \
  --no-isolation \
  --sdist \
  --outdir "$dist_root/built" \
  "$repo_root"
shopt -s nullglob
built_sdists=("$dist_root"/built/*.tar.gz)
if (( ${#built_sdists[@]} != 1 )); then
  echo "expected exactly one source distribution" >&2
  exit 1
fi
mkdir -p "$dist_root/snapshot"
sdist="$dist_root/snapshot/${built_sdists[0]##*/}"
install -m 0444 -- "${built_sdists[0]}" "$sdist"
sdist_sha256="$(sha256sum "$sdist")"
sdist_sha256="${sdist_sha256%% *}"
"$evidence_python" -I scripts/check_wheel_evidence.py \
  inspect-sdist \
  --sdist "$sdist" \
  --expected-sha256 "$sdist_sha256"
mkdir -p "$dist_root/source"
"$evidence_python" -I -c \
  'import pathlib, sys, tarfile; tarfile.open(sys.argv[1]).extractall(pathlib.Path(sys.argv[2]), filter="data")' \
  "$sdist" "$dist_root/source"
source_trees=("$dist_root"/source/athena-*)
if (( ${#source_trees[@]} != 1 )); then
  echo "expected exactly one extracted source tree" >&2
  exit 1
fi
"$evidence_python" -I -m build \
  --no-isolation \
  --wheel \
  --outdir "$dist_root/artifacts" \
  "${source_trees[0]}"
wheels=("$dist_root"/artifacts/*.whl)
if (( ${#wheels[@]} != 1 )); then
  echo "expected exactly one wheel built from the source distribution" >&2
  exit 1
fi
wheel="${wheels[0]}"

"$evidence_python" -I scripts/verify_wheel.py "$wheel"
"$evidence_python" -I \
  "${source_trees[0]}/scripts/verify_wheel.py" \
  "$wheel"
"$evidence_python" -I \
  "${source_trees[0]}/scripts/check_import_contracts.py"
"$evidence_python" -I scripts/check_wheel_evidence.py \
  inspect-wheel --wheel "$wheel"
"$evidence_python" -I scripts/check_wheel_evidence.py \
  inspect-sdist \
  --sdist "$sdist" \
  --wheel "$wheel" \
  --tool-lock constraints/security-tools-py312.txt \
  --expected-sha256 "$sdist_sha256"

python3.12 -I -m venv "$runtime_root/base"
python3.12 -I -m venv "$runtime_root/mcp"
base_python="$runtime_root/base/bin/python"
mcp_python="$runtime_root/mcp/bin/python"
for candidate_python in "$base_python" "$mcp_python"; do
  "$candidate_python" -I -m pip install \
    --only-binary :all: \
    --require-hashes -r constraints/bootstrap-py312.txt
done
"$base_python" -I -m pip install \
  --only-binary :all: \
  --constraint constraints/ci-py312.txt \
  "$wheel"
"$mcp_python" -I -m pip install \
  --only-binary :all: \
  --constraint constraints/ci-py312.txt \
  "${wheel}[mcp]"
"$base_python" -I -m pip check
"$mcp_python" -I -m pip check
(
  cd /tmp
  "$base_python" -I -c \
    'from pathlib import Path; import athena, sys; assert Path(athena.__file__).resolve().is_relative_to(Path(sys.prefix).resolve()), athena.__file__'
  "$base_python" -I "${source_trees[0]}/scripts/smoke_app.py"
)

source_revision="$(git rev-parse --verify HEAD)"
source_tree="$(git rev-parse --verify 'HEAD^{tree}')"
"$evidence_python" -I scripts/check_wheel_evidence.py audit-profile \
  --auditor-python "$evidence_python" \
  --candidate-python "$base_python" \
  --wheel "$wheel" \
  --profile base \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --raw-output "$base_raw" \
  --sbom-output "$base_sbom" \
  --source-revision "$source_revision"
"$evidence_python" -I scripts/check_wheel_evidence.py verify-profile \
  --candidate-python "$base_python" \
  --wheel "$wheel" \
  --profile base \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --sbom "$base_sbom"
"$evidence_python" -I scripts/check_wheel_evidence.py audit-profile \
  --auditor-python "$evidence_python" \
  --candidate-python "$mcp_python" \
  --wheel "$wheel" \
  --profile mcp \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --raw-output "$mcp_raw" \
  --sbom-output "$mcp_sbom" \
  --source-revision "$source_revision"
"$evidence_python" -I scripts/check_wheel_evidence.py verify-profile \
  --candidate-python "$mcp_python" \
  --wheel "$wheel" \
  --profile mcp \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --sbom "$mcp_sbom"

"$evidence_python" -I scripts/check_wheel_evidence.py create-bundle \
  --output "$candidate_bundle" \
  --sdist "$sdist" \
  --sdist-sha256 "$sdist_sha256" \
  --wheel "$wheel" \
  --input-sbom "$input_sbom" \
  --base-sbom "$base_sbom" \
  --mcp-sbom "$mcp_sbom" \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --tool-lock constraints/security-tools-py312.txt \
  --base-python "$base_python" \
  --mcp-python "$mcp_python" \
  --repository "$ATHENA_CANDIDATE_REPOSITORY" \
  --event local \
  --run-id "$(date -u +%Y%m%d%H%M%S)" \
  --run-attempt 1 \
  --checkout-commit "$source_revision" \
  --checkout-tree "$source_tree"
candidate_wheels=("$candidate_bundle"/*.whl)
if (( ${#candidate_wheels[@]} != 1 )); then
  echo "candidate bundle does not contain exactly one wheel" >&2
  exit 1
fi
for candidate_python in "$base_python" "$mcp_python"; do
  "$candidate_python" -I -m pip install \
    --no-index \
    --no-deps \
    --force-reinstall \
    "${candidate_wheels[0]}"
done
"$base_python" -I -m pip check
"$mcp_python" -I -m pip check
"$evidence_python" -I scripts/check_wheel_evidence.py verify-bundle \
  --bundle "$candidate_bundle" \
  --constraints constraints/ci-py312.txt \
  --bootstrap constraints/bootstrap-py312.txt \
  --tool-lock constraints/security-tools-py312.txt \
  --base-python "$base_python" \
  --mcp-python "$mcp_python"
```

The final directory contains exactly seven regular files: the sdist, wheel,
63-subject input SBOM, base and MCP wheel-bound runtime SBOMs, candidate
manifest, and `SHA256SUMS`. The runtime SBOMs cover the exact third-party
name/version closures observed in this Linux/CPython 3.12 run. Each SBOM retains
the producer's exact Python patch and platform as provenance; standalone
verification accepts another canonical CPython patch allowed by the wheel on
the same platform, with both candidate environments required to match that
verifier exactly. The SBOMs do not claim that `pip-audit` scanned Athena's code.
Runtime downloads remain
version-constrained rather than hash-locked. The bundle is an unsigned,
unattested candidate—not a tag, publication, or release—and its temporary
directories remain outside the checkout for inspection.

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
