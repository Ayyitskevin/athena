# Python dependency constraints and advisory evidence

`ci-py312.txt` pins the external runtime, development, and MCP dependency set
used by Athena's Linux/Python 3.12 CI job, including the PyPA build frontend.
`pyproject.toml` remains the source of package requirements; the constraints file
limits which versions pip may choose without causing every listed package to be
installed.

These are version pins, not a hash-verified or cross-platform application
lockfile. `bootstrap-py312.txt` separately hash-pins the pip version that
installs the graph. Setuptools is not an application/runtime constraint: its
exact `pyproject.toml` build-system pin is mirrored in the separate
hash-verified evidence-tool lock described below.

To refresh the CI graph, use a clean Python 3.12 virtual environment on Linux:

```bash
constraint_env=$(mktemp -d /tmp/athena-constraints.XXXXXX)
python3.12 -m venv "$constraint_env"
"$constraint_env/bin/python" -I -m pip install \
  --require-hashes -r constraints/bootstrap-py312.txt
"$constraint_env/bin/python" -I -m pip install -e ".[dev,mcp]"
"$constraint_env/bin/python" -I -m pip check
"$constraint_env/bin/python" -I -m pip freeze --exclude-editable
```

Replace `ci-py312.txt` with the final command's output, then repeat CI's
constrained install in a second clean environment and run Ruff, the full test
suite, and `scripts/smoke_app.py`. CI also compares its installed external
package set to this file so a newly declared or transitive runtime, development,
or MCP dependency cannot silently bypass the pins. The pinned build frontend
drives CI's source-distribution-to-wheel gate. Candidate construction uses the
hash-verified setuptools backend from the evidence-tool lock with
`python -m build --no-isolation`.

## Evidence toolchain

`security-tools-py312.txt` is the hash-verified Linux/Python 3.12 tool lock for
`pip-audit` and the sdist-to-wheel build frontend/backend. The historical
filename is retained to avoid a needless lock migration. Its header records the
`uv` version, target platform, and package-index cutoff used to compile it from
`security-tools.in`. Refresh it with the recorded, executable command:

```bash
uv pip compile constraints/security-tools.in \
  --output-file constraints/security-tools-py312.txt \
  --generate-hashes \
  --only-binary :all: \
  --emit-build-options \
  --python-version 3.12 \
  --python-platform x86_64-manylinux_2_28 \
  --exclude-newer 2026-07-31T09:54:22Z \
  --no-sources \
  --default-index https://pypi.org/simple \
  --index-strategy first-index \
  --no-annotate
```

Confirm `uv --version` is `0.11.28`, preserve that fact in the header, review
the complete lock diff, and rerun the tool self-audit, input audit, and
wheel-profile audits in
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

The CI evidence job first scans this tool lock itself. It then audits exactly
61 CI dependency pins, hash-pinned pip, and the pinned setuptools build backend.
[`scripts/check_supply_chain.py`](../scripts/check_supply_chain.py) rejects
unpinned or duplicate inputs, a scanner environment that differs from the
hash-locked tool graph, scanner/service failures, malformed required CycloneDX
identity fields/components, component drift, and any reported vulnerability.
There is no advisory ignore list or soft-pass path.

The resulting 63-component CycloneDX file describes CI, build, and bootstrap
*inputs*. It is not a signed release SBOM, does not claim that the application
constraint graph is hash-locked, and does not attest to a particular wheel.
Advisory status is also time-sensitive: rerunning against PyPI's vulnerability
service may correctly fail when new information is published.

The same hash-locked evidence environment builds the release candidate with
`python -m build --no-isolation`, so the pinned setuptools backend is not
silently fetched into an opaque build environment. Runtime installation remains
constrained to `ci-py312.txt`; those application downloads are version-pinned,
not artifact-hash-locked. Wheel-bound base and MCP SBOMs therefore describe the
exact installed closure observed in that run and do not erase this residual
risk.
