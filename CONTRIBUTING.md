# Contributing to Athena

Athena is a local-alpha, self-hosted operator workspace for one human directing
an AI fleet. Contributions are welcome when they strengthen that specific
product rather than broaden it into an enterprise work-management suite.

Before changing code, read [`AGENTS.md`](AGENTS.md),
[`docs/VISION.md`](docs/VISION.md), and
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). They are the contribution
contract, product north star, and design of record.
Read the [glossary and decision index](docs/GLOSSARY.md) for shared terms and the
sources behind accepted rules.

## Local gate profiles

After setup, `scripts/gate.sh` defaults to the `product` profile, including the
pinned dependency set, types, and full-suite, full-source coverage. Explicit
`scripts/gate.sh --profile product` selects the same checks.
`scripts/gate.sh --profile quick` opts into fast, **partial** feedback without
the dependency, type, or coverage stages; `--profile changed`, the inner loop
described below, adds the type check and the tests your change reaches. Every
profile reports the commit, dirty state, Python and import path, stages run and
omitted, and fails at the first failed stage. The script refuses an Athena
import from another checkout.
The product pass covers local product stages, not CI's container job or its
release-candidate supply-chain job; obtain those results separately. CI shards
coverage and combines evidence; `scripts/coverage.sh` runs and checks it locally.
The stage inventory is in `scripts/gate.py`; tests compare it with CI's named
checks and complete command lines after dropping blank and comment-only lines.
Unnamed steps with commands are rejected. Action version bumps do not change that
inventory; removed, replaced, or added commands in mapped steps require an
inventory review.
The wrapper requires this checkout's executable `.venv/bin/python`, as created
below, and refuses with a setup pointer if it is missing or broken. It does not
require activating the environment.

`--json <path>` also writes the receipt as JSON (a path relative to the
checkout root, or absolute; standard output keeps the human receipt). Schema
`athena-gate-receipt/1` carries the profile, commit, `dirty`, Python and import
path, each stage's `result` (`passed`, `failed`, `unavailable` or `not_run`)
and `seconds`, the `stages_run`, `stages_passed` and `not_run` lists, `tests`
(pytest's closing counts) and `coverage` (line, branch and combined
covered/total/percent/floor) when a test stage ran, `peak_memory`, for the
changed profile its `selection`, `partial`, `passed`, `exit_code`, the
`verdict` line, and `receipt`, the printed closing lines verbatim. The pull
request template asks for this receipt, or its closing lines, as the PR's
verification evidence.

### The inner loop: `--profile changed`

`scripts/gate.sh --profile changed [--base <ref>]` runs the quick stages, the
type check, and the tests selected from your change, with the same
`ATHENA_COVERAGE_WORKERS` pytest-xdist workers as the suite. The change is every
path that differs between the merge base with `--base` (default `origin/main`)
and the working tree, uncommitted and untracked files included. If `origin/main`
is missing it falls back to local `main` and says so; with neither, or with no
merge base (a shallow clone), it runs the full suite. A `--base` that is not a
commit fails the gate. The receipt is **partial**: it ends in
`CHANGED PROFILE PASS (PARTIAL; ...)`, never `PRODUCT GATE PASS`, the selected
tests run without the coverage floors (a partial run cannot meet them), and the
dependency and coverage stages are listed as not run. It names the base and
merge-base commit, the changed paths, how many test files and tests were
selected out of the total, which rule selected them, and every path that forced
the full suite. Use it while you work; the product profile is still the
condition of done.

`scripts/select_tests.py` makes the selection statically (it parses files with
`ast` and imports nothing), in a few seconds:

- **Import graph.** Python modules under `src/`, `tests/` and `scripts/` are
  nodes; imports anywhere in a file (relative ones resolved), the parent
  packages an import runs, and strings in tests that name an `athena` module
  (monkeypatch targets) are edges. A changed module selects every test that
  reaches it; a changed test selects itself; a changed test helper selects the
  tests that import it. The graph is **not** followed through the composition
  roots `athena.main`, `athena.web` and `athena.web.router`: nearly every test
  builds the app, so following them would select the full suite for any change.
- **The map.** `TEST_MAP` in the same file names the tests for what is reached
  without an import: `*_api.py` routers and `web/` modules over HTTP, MCP
  `tools_*.py`, templates, static assets and field-guide pages. Most entries
  match tests by file name. It applies to the changed path and to every module
  the graph reaches. `tests/test_select_tests.py` fails when an entry stops
  matching real files or a router, web module or tool has no mapped test.
- **References.** A test whose strings name the changed path, its file name or
  a directory containing it (a path built with `/` or `Path(...)`) is selected.
  That covers documents, fixtures and scripts tests read. A document no test
  names selects no tests, and the receipt says so.
- **Full suite.** `pyproject.toml`, `constraints/`, any `conftest.py`,
  `core/db.py`, migrations, the composition roots and the gate scripts select
  the full suite. So does a path no rule maps, and a change that reaches a
  module only a composition root imports and the map does not name (today
  `athena.middleware`, which every request runs). The receipt names the path
  and the reason.

The blind spot: a test that reaches changed code only through the app (it
imports `athena.main` and nothing that imports the changed module) is selected
only if the map names it, and the map goes by test file names. A route used as
setup by tests named for something else is the typical miss. The product
profile, which runs everything, is what catches it.

## Development setup

Athena supports Python 3.12 (`>=3.12,<3.13`). It is the only Python version
verified in CI. `scripts/bootstrap.sh` prepares a fresh checkout in one step: it
fetches full history (a shallow clone has no merge base for `--profile changed`)
and builds `.venv` with exactly the first five commands below, skipping the
install while `pyproject.toml`, the two constraints files and the script itself
are unchanged since it last succeeded. A Claude Code cloud session runs it at
start (`.claude/hooks/session-start.sh`); every other lane runs it by hand.
`tests/test_bootstrap.py` holds the script to this block.

From a clean checkout, run the required gate:

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
.venv/bin/python scripts/check_transaction_commits.py
.venv/bin/python scripts/check_imported_at_guards.py
.venv/bin/python scripts/check_surface_parity.py
scripts/coverage.sh
```

The constraints file is the exact verified Linux/Python 3.12 CI graph. The
freeze diff rejects dependency drift; `pip check` rejects incompatible
installed metadata. The five `check_*` scripts fail the build on doctrinal
drift the tests cannot see: `check_import_contracts.py` keeps the layer
direction (`web → aegis|mentor → core`), `check_write_ownership.py` keeps
transports (every module that defines a router or registers routes or
exception handlers, plus `web/` and `mcp/`) from writing except through
command modules and the designated writers named in it,
`check_transaction_commits.py` fails on a call inside a `db.transaction`
block that commits (a writer whose `commit` is not `False` there, or one that
commits whatever it is passed) and, outside `core/db.py`, on a commit that no
`commit` parameter governs and on a transaction opened or ended by hand
(`execute("BEGIN ...")` and its kin): a writer takes `commit` or goes through
`db.transaction`, and the few that must commit on their own are named, with
the reason, in its `ALLOWED_OWN_TRANSACTIONS` (a stale entry fails), and
`check_imported_at_guards.py` pins every
native-only activity reader to its `imported_at IS NULL` guard (and FORGE.md's
guard table to the checker, via `tests/test_imported_at_guards.py`). Readers are
declared beside their code, in the `imported_at_guards.toml` of their package
(`src/athena/aegis/`, `src/athena/core/`): a new read of the activity table adds
a `[[guarded]]` entry (the pattern its function must carry, and why) or an
`[[exempt]]` entry (why foreign history is safe there) to that file, whose
header describes both. `check_surface_parity.py` fails when a command function
is reached from only one transport, the cockpit (`web/`) or the REST API
(`*_api.py`, `api.py`), and `scripts/surface_parity.toml` does not declare it
with a kind and a reason (C10). A new one-sided command gets its twin or a row
there; a row whose function is gone, or reached from both sides now, fails as
stale. MCP is a client of REST and is not compared.
`scripts/coverage.sh` runs the complete test suite with
full-source branch coverage, writes evidence outside the checkout, and enforces
the floors configured in `pyproject.toml`. The floors are a ratchet:
`scripts/check_coverage.py` also fails when line, branch or combined coverage is
more than half a point above its floor, with "raise the floor to X" where X is
the measurement minus 0.25, rounded down to two decimals. Paste X into
`[tool.athena.coverage]` in the same change; it sits below both a local run and
CI's combined shards (which measure up to about 0.09 points higher), so one
paste passes both. Floors only rise, and the gate receipt carries the failure
line. The changed profile measures no coverage, so neither floors nor ratchet
apply to it. It runs `ATHENA_COVERAGE_WORKERS`
pytest-xdist workers (default 4). At the default the suite needs about
1.5 GiB (1.40 GiB measured on 2026-11-22) of memory;
set fewer workers on a smaller machine. The gate receipt reports the coverage
stage's peak memory and says when a worker was terminated, which usually means
the machine ran out of memory.

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
3. Add tests that explain the invariant being protected. Start them from
   `tests/support.py` (the first user, more accounts, agents, tokens of a given
   scope set, clients that act as an account, the activity trail) rather than
   declaring another `_bootstrap`; `tests/test_suite_conventions.py` fails if the
   count of files that do rises, or if a test waits with `time.sleep`.
4. If the change is notable to a reader of the changelog, add a fragment under
   `changelog.d/` (see [Changelog entries](#changelog-entries)); do not edit
   `CHANGELOG.md` by hand.
5. Run the complete required local gate above.
6. Open a pull request using the repository template. State the user-visible
   outcome, boundaries, verification (the gate receipt), and any AI assistance.

Never push directly to `main`. Do not rewrite another contributor's branch or
hide unrelated work in a formatting or dependency update.

### Changelog entries

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
A pull request does not edit its `[Unreleased]` section, which parallel pull
requests used to conflict on: it adds one file per entry to `changelog.d/`,
named `<slug>.<category>.md`. The slug is the branch's topic in lowercase
letters, digits and hyphens (add `-2` for a second entry); the category is one
of `added`, `changed`, `deprecated`, `removed`, `fixed` or `security`. The file
holds the entry exactly as it will read: one bullet in the house style
(`- **A bold sentence.**` and then the detail), every further line and paragraph
indented by two spaces. `changelog.d/README.md` has an example.
`tests/test_changelog_fragments.py` validates every fragment, so the gate fails
on a malformed one; `.venv/bin/python scripts/changelog_fragments.py check` runs
the same check alone.

The fragments are assembled before a milestone heading is cut, or whenever the
section should be read whole, in a pull request of its own:
`scripts/changelog_fragments.py assemble --dry-run` prints the change, and
`assemble` writes it and deletes the assembled fragments. Each entry goes to the
top of the first `### <Category>` heading under `[Unreleased]`, or under a new
heading at the top of the section; the existing text is never rewritten or
reordered.

### Bumping the version

The version lives in **three** places, and them drifting apart is the classic
release bug — the wheel says one thing and the running app reports another:

| file | what it is |
|---|---|
| `pyproject.toml` → `[project] version` | what gets packaged |
| `src/athena/__init__.py` → `__version__` | what the running app reports |
| `tests/test_smoke.py` | the literal tripwire, so a bump is never accidental |

Change all three in **one commit that does nothing else**. Two guards back this
up: `test_the_packaged_version_and_the_dunder_version_agree` fails the moment the
first two disagree, and the publish workflow refuses to run on a tag that does not
name the version being packaged.

While Athena is pre-1.0 the line is `0.1.0aN`. A CHANGELOG heading is a
**milestone**, not a release — it becomes a release only once a matching git tag
exists. See [`RELEASING.md`](RELEASING.md) for the order, and note that tagging is
the release owner's act, not a step any contributor or agent performs.

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
