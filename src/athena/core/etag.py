"""Strong resource validators and strict ``If-Match`` parsing.

Athena emits content-derived strong ETags and accepts the HTTP ``If-Match``
list syntax at mutation boundaries.  Parsing lives here rather than in a
framework dependency so REST commands can evaluate a parsed condition while
holding the same database write lock as the mutation it protects.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


MAX_IF_MATCH_BYTES = 8192
MAX_IF_MATCH_TAGS = 64


class InvalidIfMatch(ValueError):
    """An ``If-Match`` field value is not valid HTTP entity-tag syntax."""


class IfMatchTooLarge(ValueError):
    """An ``If-Match`` condition exceeds Athena's bounded parsing limits."""


@dataclass(frozen=True, slots=True)
class IfMatch:
    """A parsed ``If-Match`` condition evaluated with strong comparison.

    Supplying ``current_etag`` means a current representation exists, so the
    wildcard condition matches.  Weak request tags are intentionally absent
    from ``_strong_tags``: HTTP requires strong comparison for ``If-Match``.
    """

    wildcard: bool
    _strong_tags: frozenset[str]

    def matches(self, current_etag: str) -> bool:
        """Return whether this condition strongly matches ``current_etag``."""
        return self.wildcard or current_etag in self._strong_tags


def strong_etag(namespace: str, representation: object) -> str:
    """Return a deterministic, quoted strong ETag for a JSON representation.

    Namespace and payload are length-framed before hashing so distinct pairs
    cannot collide through concatenation ambiguity.  The version marker lets a
    future canonicalization change invalidate old validators deliberately.
    """
    if not isinstance(namespace, str):
        raise TypeError("namespace must be a string")

    canonical = json.dumps(
        representation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    namespace_bytes = namespace.encode("utf-8")

    digest = hashlib.sha256()
    digest.update(b"athena-strong-etag-v1\0")
    digest.update(len(namespace_bytes).to_bytes(8, "big"))
    digest.update(namespace_bytes)
    digest.update(len(canonical).to_bytes(8, "big"))
    digest.update(canonical)
    return f'"sha256-{digest.hexdigest()}"'


def parse_if_match(values: list[str]) -> IfMatch:
    """Parse repeated ``If-Match`` field values into a bounded condition.

    Repeated field lines are combined with commas, as HTTP defines for list
    fields. Empty list members are ignored as RFC 9110 requires; malformed
    syntax is rejected while commas inside quoted opaque tags remain valid.
    """
    if not values:
        raise InvalidIfMatch("If-Match must not be empty")

    total_bytes = 0
    for position, value in enumerate(values):
        if not isinstance(value, str):
            raise InvalidIfMatch("If-Match values must be strings")
        try:
            # Repeated list fields are combined with an inserted comma. Count
            # those separators too so many empty field lines cannot evade the
            # bound and allocate an unbounded combined value below.
            if position:
                total_bytes += 1
            total_bytes += len(value.encode("latin-1"))
        except UnicodeEncodeError as exc:
            raise InvalidIfMatch(
                "If-Match contains a character outside the HTTP field-value range"
            ) from exc
        if total_bytes > MAX_IF_MATCH_BYTES:
            raise IfMatchTooLarge(f"If-Match exceeds {MAX_IF_MATCH_BYTES} bytes")

    combined = ",".join(values)
    length = len(combined)
    index = _skip_ows(combined, 0)
    if index == length:
        return IfMatch(wildcard=False, _strong_tags=frozenset())

    if combined[index] == "*":
        index = _skip_ows(combined, index + 1)
        if index != length:
            raise InvalidIfMatch("If-Match wildcard must be the sole value")
        return IfMatch(wildcard=True, _strong_tags=frozenset())

    strong_tags: set[str] = set()
    tag_count = 0
    while True:
        index = _skip_ows(combined, index)
        if index == length:
            return IfMatch(
                wildcard=False,
                _strong_tags=frozenset(strong_tags),
            )
        if combined[index] == ",":
            tag_count += 1
            if tag_count > MAX_IF_MATCH_TAGS:
                raise IfMatchTooLarge(
                    f"If-Match exceeds {MAX_IF_MATCH_TAGS} list members"
                )
            index += 1
            continue
        weak = combined.startswith("W/", index)
        if weak:
            index += 2
        if index >= length or combined[index] != '"':
            raise InvalidIfMatch("If-Match entity tags must be quoted")

        tag_start = index
        index += 1
        while index < length and combined[index] != '"':
            if not _is_etag_character(combined[index]):
                raise InvalidIfMatch(
                    "If-Match contains an invalid entity-tag character"
                )
            index += 1
        if index >= length:
            raise InvalidIfMatch("If-Match contains an unterminated entity tag")

        index += 1
        raw_tag = combined[tag_start:index]
        tag_count += 1
        if tag_count > MAX_IF_MATCH_TAGS:
            raise IfMatchTooLarge(f"If-Match exceeds {MAX_IF_MATCH_TAGS} entity tags")
        if not weak:
            strong_tags.add(raw_tag)

        index = _skip_ows(combined, index)
        if index == length:
            return IfMatch(
                wildcard=False,
                _strong_tags=frozenset(strong_tags),
            )
        if combined[index] != ",":
            raise InvalidIfMatch("If-Match entity tags must be comma-separated")
        index += 1


def _skip_ows(value: str, index: int) -> int:
    while index < len(value) and value[index] in " \t":
        index += 1
    return index


def _is_etag_character(character: str) -> bool:
    codepoint = ord(character)
    # RFC 9110 etagc: %x21 / %x23-7E / obs-text. DQUOTE, whitespace, and
    # controls are excluded; commas and backslashes are ordinary opaque bytes.
    return codepoint == 0x21 or 0x23 <= codepoint <= 0x7E or 0x80 <= codepoint <= 0xFF
