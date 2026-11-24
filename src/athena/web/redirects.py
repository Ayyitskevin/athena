"""Where a browser may be sent back to: a plain local path, or nowhere.

One rule for every ``next``-style target a form carries (today the theme and
rail toggles), so an open redirect cannot reappear in one of them while the
others stay closed. The sign-in page takes no target: it always lands on
``LANDING``.
"""

from __future__ import annotations

#: Where sign-in lands when no safe ``next`` was asked for: the dashboard, whose
#: first panel is the fleet-attention card and its ranked queue (U6).
LANDING = "/aegis/dashboard"


def safe_local_path(target: str | None) -> str | None:
    """``target`` if it is an absolute path inside this app, else ``None``.

    Anything that is not a plain local path is refused: ``//host`` is
    protocol-relative, ``https://…`` and ``javascript:`` are absolute, and a
    backslash is an authority separator in WHATWG URL parsing. Control
    characters are refused as well, instead of relying on response-header
    serialization to make an unsafe value inert.
    """
    if not target:
        return None
    has_control = any(ord(char) < 0x20 or ord(char) == 0x7F for char in target)
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and not has_control
    ):
        return target
    return None
