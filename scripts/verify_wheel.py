"""Verify that Athena's wheel carries its exact runtime-file manifest."""

from __future__ import annotations

import argparse
from collections import Counter
import configparser
from pathlib import Path, PurePosixPath
import sys
from zipfile import BadZipFile, ZipFile


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "athena"
RUNTIME_DIRS = ("core/migrations", "field_guide", "static", "templates")
REQUIRED_CONSOLE_SCRIPTS = {"athena-serve": "athena.ops:serve_main"}


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def source_runtime_files(package_root: Path = PACKAGE_ROOT) -> set[str]:
    """Return every source file Athena needs at runtime, relative to the package."""
    return {
        path.relative_to(package_root).as_posix()
        for directory in RUNTIME_DIRS
        for path in (package_root / directory).rglob("*")
        if path.is_file()
    }


def wheel_runtime_files(wheel_path: Path) -> list[str]:
    """Return runtime members from a wheel using the package-relative shape."""
    runtime_roots = tuple(PurePosixPath(directory) for directory in RUNTIME_DIRS)
    files: list[str] = []
    with ZipFile(wheel_path) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            try:
                relative = PurePosixPath(name).relative_to("athena")
            except ValueError:
                continue
            if any(relative.is_relative_to(root) for root in runtime_roots):
                files.append(relative.as_posix())
    return files


def verify_runtime_manifest(
    wheel_path: Path, *, package_root: Path = PACKAGE_ROOT
) -> dict[str, int]:
    """Require the wheel's runtime files to match the source tree exactly."""
    source_files = source_runtime_files(package_root)
    if not source_files:
        raise RuntimeError("source runtime manifest is empty")

    wheel_members = wheel_runtime_files(wheel_path)
    wheel_counts = Counter(wheel_members)
    wheel_files = set(wheel_members)
    missing = sorted(source_files - wheel_files)
    unexpected = sorted(wheel_files - source_files)
    duplicates = sorted(path for path, count in wheel_counts.items() if count != 1)
    if missing or unexpected or duplicates:
        details = ["wheel runtime manifest differs from source"]
        if missing:
            details.append("missing:\n  " + "\n  ".join(missing))
        if unexpected:
            details.append("unexpected:\n  " + "\n  ".join(unexpected))
        if duplicates:
            details.append("duplicates:\n  " + "\n  ".join(duplicates))
        raise RuntimeError("\n".join(details))

    return {
        directory: sum(
            PurePosixPath(path).is_relative_to(PurePosixPath(directory))
            for path in source_files
        )
        for directory in RUNTIME_DIRS
    }


def verify_console_scripts(
    wheel_path: Path,
    *,
    required: dict[str, str] = REQUIRED_CONSOLE_SCRIPTS,
) -> None:
    """Require one unambiguous wheel console-script mapping for each safe entrypoint."""
    with ZipFile(wheel_path) as archive:
        candidates = [
            name
            for name in archive.namelist()
            if len(PurePosixPath(name).parts) == 2
            and PurePosixPath(name).parts[0].endswith(".dist-info")
            and PurePosixPath(name).name == "entry_points.txt"
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                "wheel must contain exactly one .dist-info/entry_points.txt"
            )
        try:
            payload = archive.read(candidates[0]).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("wheel entry_points.txt is not valid UTF-8") from exc

    parser = _CaseSensitiveConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(payload)
    except configparser.Error as exc:
        raise RuntimeError(f"wheel entry_points.txt is invalid: {exc}") from exc
    if not parser.has_section("console_scripts"):
        raise RuntimeError("wheel entry_points.txt has no [console_scripts] section")
    scripts = dict(parser.items("console_scripts"))
    for name, expected in required.items():
        actual = scripts.get(name)
        if actual != expected:
            rendered = "missing" if actual is None else repr(actual)
            raise RuntimeError(
                f"wheel console script {name!r} must target {expected!r}; got {rendered}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="verify Athena wheel templates, static assets, and migrations"
    )
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args(argv)

    try:
        counts = verify_runtime_manifest(args.wheel)
        verify_console_scripts(args.wheel)
    except (BadZipFile, OSError, RuntimeError) as exc:
        print(f"Athena wheel runtime manifest failed: {exc}", file=sys.stderr)
        return 1

    print(
        "Athena wheel runtime manifest passed: "
        f"{counts['core/migrations']} migrations, "
        f"{counts['static']} static assets, "
        f"{counts['templates']} templates, "
        "and required console scripts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
