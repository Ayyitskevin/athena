#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
athena_python="${ATHENA_PYTHON:-$repo_root/.venv/bin/python}"

if (( $# > 1 )); then
  echo "usage: scripts/coverage.sh [fresh-report-directory]" >&2
  exit 2
fi
if [[ ! -x "$athena_python" ]]; then
  echo "coverage gate requires an executable Python at $athena_python" >&2
  exit 2
fi

if (( $# == 1 )); then
  if [[ -z "$1" ]]; then
    echo "coverage report directory must not be empty" >&2
    exit 2
  fi
  mkdir -p -- "$1"
  report_dir="$(cd -- "$1" && pwd -P)"
else
  report_candidate="$(mktemp -d -- "${TMPDIR:-/tmp}/athena-coverage.XXXXXX")"
  report_dir="$(cd -- "$report_candidate" && pwd -P)"
fi

if [[ "$report_dir" == "$repo_root" || "$report_dir" == "$repo_root/"* ]]; then
  echo "coverage reports must be written outside the checkout: $report_dir" >&2
  exit 2
fi
shopt -s dotglob nullglob
report_entries=("$report_dir"/*)
shopt -u dotglob nullglob
if (( ${#report_entries[@]} > 0 )); then
  echo "coverage report directory must be empty: $report_dir" >&2
  exit 2
fi

cd "$repo_root"
COVERAGE_FILE="$report_dir/.coverage" "$athena_python" -m pytest -q -n 4 -ra --strict-markers \
  --cov \
  --cov-config=pyproject.toml \
  --cov-report=term-missing \
  "--cov-report=json:$report_dir/coverage.json" \
  "--cov-report=xml:$report_dir/coverage.xml"

for artifact in .coverage coverage.json coverage.xml; do
  artifact_path="$report_dir/$artifact"
  if [[ ! -f "$artifact_path" || -L "$artifact_path" || ! -s "$artifact_path" ]]; then
    echo "coverage evidence must be a non-empty regular file: $artifact_path" >&2
    exit 1
  fi
done

"$athena_python" scripts/check_coverage.py "$report_dir/coverage.json"

echo "Coverage evidence: $report_dir"
