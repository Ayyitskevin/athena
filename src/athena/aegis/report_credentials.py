"""Narrow accident guard for new reports, not a general secret/security boundary."""

import re

_START = r"(?<![A-Za-z0-9_-])"
_END = r"(?![A-Za-z0-9_-])"
_TOKENS = (
    (
        "GitHub token",
        r"(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59})",
    ),
    ("AWS access key ID", r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    (
        "Slack token",
        r"(?:xoxb-[0-9]{10,13}-[0-9]{10,13}-[A-Za-z0-9]{24}"
        r"|xoxp-(?:[0-9]{10,13}-){3}[a-f0-9]{32}"
        r"|xapp-1-A[A-Z0-9]{10}-[0-9]{10,13}-[a-f0-9]{64}"
        r"|xoxe\.xox[bp]-1-[A-Za-z0-9]{163,166})",
    ),
    (
        "OpenAI API key",
        r"(?:sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
        r"|sk-(?:proj|svcacct|admin)-(?:"
        r"[A-Za-z0-9_-]{58}T3BlbkFJ[A-Za-z0-9_-]{58}"
        r"|[A-Za-z0-9_-]{74}T3BlbkFJ[A-Za-z0-9_-]{74}))",
    ),
    ("Anthropic API key", r"sk-ant-(?:api03|admin01)-[A-Za-z0-9_-]{93}AA"),
    # core.tokens mints TOKEN_PREFIX + secrets.token_urlsafe(32): 43 unpadded chars.
    ("Athena token", r"ath_[A-Za-z0-9_-]{43}"),
)
_PATTERNS = (
    (
        "PEM private key",
        re.compile(
            r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY-----"
        ),
    ),
    *((kind, re.compile(_START + pattern + _END)) for kind, pattern in _TOKENS),
    (
        "URL password",
        re.compile(r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/?#@:]*:[^\s/?#@]+@"),
    ),
    (
        "Authorization token",
        re.compile(
            r"(?i:\bauthorization)[ \t]*:[ \t]*"
            r"(?:[A-Za-z][A-Za-z0-9_-]*[ \t]+)?"
            r"[A-Za-z0-9._~+/-]{32,}={0,2}(?![A-Za-z0-9._~+/=-])"
        ),
    ),
)


def credential_kind(text: str) -> str | None:
    """Return only a fixed kind for a known shape; never return matched text.

    No I/O, validation against a provider, decoding, entropy guesses, or redaction.
    Unknown/changed formats and arbitrary passwords can pass this accident guard.
    """
    return next((kind for kind, pattern in _PATTERNS if pattern.search(text)), None)
