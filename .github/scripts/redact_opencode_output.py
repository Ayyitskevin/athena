#!/usr/bin/env python3
"""Stream OpenCode logs while masking dynamic credentials GitHub cannot know."""

from __future__ import annotations

import re
import sys
from typing import BinaryIO


REDACTED = b"***REDACTED***"
AUTHORIZATION = re.compile(
    rb"(authorization:\s*(?:basic|bearer)\s+)[A-Za-z0-9._~+/=-]+",
    re.IGNORECASE,
)
GITHUB_TOKEN = re.compile(
    rb"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
)


def redact(data: bytes) -> bytes:
    """Mask credential-shaped values without changing unrelated output."""
    data = AUTHORIZATION.sub(lambda match: match.group(1) + REDACTED, data)
    return GITHUB_TOKEN.sub(REDACTED, data)


def redact_stream(source: BinaryIO, destination: BinaryIO) -> None:
    """Redact complete log lines and flush each one for live Actions output."""
    for line in source:
        destination.write(redact(line))
        destination.flush()


def main() -> int:
    redact_stream(sys.stdin.buffer, sys.stdout.buffer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
