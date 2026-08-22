#!/usr/bin/env python3
"""Fail closed when a running Athena process does not match its checkout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request


_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RUNTIME_KEYS = frozenset({"version", "commit", "tree_state", "source"})


def compare_provenance(
    *, runtime: dict, checkout_commit: str, checkout_tree_state: str
) -> list[str]:
    """Return every identity mismatch instead of hiding later failures."""
    errors: list[str] = []
    version = runtime.get("version")
    commit = runtime.get("commit")
    source = runtime.get("source")
    tree_state = runtime.get("tree_state")

    actual_keys = set(runtime)
    unexpected = sorted(actual_keys - _RUNTIME_KEYS)
    missing = sorted(_RUNTIME_KEYS - actual_keys)
    if unexpected:
        errors.append(f"runtime payload has unexpected keys: {', '.join(unexpected)}")
    if missing:
        errors.append(f"runtime payload is missing keys: {', '.join(missing)}")
    if not isinstance(version, str) or not version:
        errors.append("runtime version is missing")
    if source != "git-checkout":
        errors.append(f"runtime source is {source!r}, expected 'git-checkout'")
    if not isinstance(commit, str) or _FULL_COMMIT.fullmatch(commit) is None:
        errors.append("runtime commit is missing or malformed")
    elif commit != checkout_commit:
        errors.append(
            f"runtime commit {commit} does not match checkout {checkout_commit}"
        )
    if tree_state == "dirty":
        errors.append("runtime startup snapshot was dirty")
    elif tree_state != "clean":
        errors.append(f"runtime startup tree state is {tree_state!r}, expected 'clean'")
    if checkout_tree_state != "clean":
        errors.append("checkout is dirty now")
    return errors


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise RuntimeError(detail)
    return result.stdout.strip()


def _fetch_runtime(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "athena-drift/1"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"{url} returned HTTP {response.status}")
        body = response.read(16_385)
    if len(body) > 16_384:
        raise RuntimeError("runtime provenance response exceeds 16 KiB")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError("runtime provenance response is not an object")
    return payload


def _checkout_snapshot(checkout: Path) -> tuple[str, str]:
    commit_before = _git(checkout, "rev-parse", "--verify", "HEAD")
    status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=normal")
    commit_after = _git(checkout, "rev-parse", "--verify", "HEAD")
    if commit_before != commit_after:
        raise RuntimeError("checkout changed while reading provenance")
    return commit_after, "dirty" if status else "clean"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default="http://127.0.0.1:8300/version", help="Athena /version URL"
    )
    parser.add_argument(
        "--checkout", type=Path, default=Path.cwd(), help="deployed Git checkout"
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checkout = args.checkout.resolve(strict=True)
        before = _checkout_snapshot(checkout)
        runtime = _fetch_runtime(args.url, args.timeout)
        after = _checkout_snapshot(checkout)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"Athena provenance check failed: {exc}", file=sys.stderr)
        return 1

    if before != after:
        print(
            "Athena provenance mismatch: checkout changed during provenance check",
            file=sys.stderr,
        )
        return 1
    checkout_commit, checkout_tree_state = after

    errors = compare_provenance(
        runtime=runtime,
        checkout_commit=checkout_commit,
        checkout_tree_state=checkout_tree_state,
    )
    if errors:
        for error in errors:
            print(f"Athena provenance mismatch: {error}", file=sys.stderr)
        return 1
    print(f"Athena provenance OK: {runtime['version']} {checkout_commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
