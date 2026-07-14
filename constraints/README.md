# CI dependency constraints

`ci-py312.txt` pins the external runtime, development, and MCP dependency set
used by Athena's Linux/Python 3.12 CI job. `pyproject.toml` remains the source of
package requirements; the constraints file limits which versions pip may choose
without causing every listed package to be installed.

These are version pins, not a hash-verified or cross-platform lockfile. They do
not pin the Python patch release, pip itself, or the isolated setuptools build
environment. To refresh them, use a clean Python 3.12 virtual environment on
Linux:

```bash
constraint_env=$(mktemp -d /tmp/athena-constraints.XXXXXX)
python3.12 -m venv "$constraint_env"
"$constraint_env/bin/python" -m pip install -e ".[dev,mcp]"
"$constraint_env/bin/python" -m pip check
"$constraint_env/bin/python" -m pip freeze --exclude-editable
```

Replace `ci-py312.txt` with the final command's output, then repeat CI's
constrained install in a second clean environment and run Ruff, the full test
suite, and `scripts/smoke_app.py`. CI also compares its installed external
package set to this file so a newly declared or transitive runtime, development,
or MCP dependency cannot silently bypass the pins.
