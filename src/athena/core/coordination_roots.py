"""Immutable operator evidence catalog for *declared* work, not OS authority.

No path in this module is ever opened or resolved through the filesystem.
Configuration establishes syntax; operators own the truth of its topology.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

MAX_CATALOG_BYTES = 16_384
MAX_ROOTS = 32
MAX_PREFIX_CHARS = 256
MAX_BINDING_BYTES = 2_048
_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


class RootError(ValueError):
    def __init__(self, detail: str, *, kind: str = "invalid"):
        super().__init__(detail)
        self.kind = kind


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise RootError("coordination root text must be valid UTF-8") from exc


def normalize_prefix(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_PREFIX_CHARS:
        raise RootError("coordination root prefix must be a bounded relative string")
    _utf8_size(value)
    if (
        value.startswith("/")
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise RootError("coordination root prefix must be relative POSIX text")
    parts = value.split("/")
    if ".." in parts:
        raise RootError("coordination root prefix may not contain '..'")
    normalized = "/".join(part for part in parts if part not in ("", "."))
    if re.match(r"^[A-Za-z]:", normalized):
        raise RootError("coordination root prefix must not contain a drive prefix")
    return normalized


def _key(value: object) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise RootError("coordination root keys must be bounded lowercase identifiers")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise RootError("coordination root digest must be 64 lowercase hex characters")
    return value


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise RootError("duplicate coordination root JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise RootError("non-finite coordination root JSON number")


def _json(raw: str, limit: int) -> object:
    if _utf8_size(raw) > limit:
        raise RootError("coordination root JSON exceeds its byte bound")
    try:
        return json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise RootError("invalid coordination root JSON") from exc


@dataclass(frozen=True)
class RootSelector:
    catalog_sha256: str
    root_key: str
    relative_prefix: str = ""

    def as_dict(self) -> dict:
        return {
            "catalog_sha256": self.catalog_sha256,
            "root_key": self.root_key,
            "relative_prefix": self.relative_prefix,
        }


@dataclass(frozen=True)
class RootEntry:
    root_key: str
    tree_id: str
    prefix: str
    evidence_sha256: str


@dataclass(frozen=True)
class RootCatalog:
    sha256: str
    roots: tuple[RootEntry, ...]
    storage_namespace: str
    topology_epoch: str

    def resolve(self, selector: RootSelector) -> tuple[str, str]:
        if selector.catalog_sha256 == self.sha256:
            for entry in self.roots:
                if entry.root_key == selector.root_key:
                    prefix = "/".join(
                        p for p in (entry.prefix, selector.relative_prefix) if p
                    )
                    return entry.tree_id, normalize_prefix(prefix)
        raise RootError(
            "coordination root is unresolved for the current catalog", kind="conflict"
        )


def parse_catalog(raw: str) -> RootCatalog | None:
    if raw == "":
        return None
    value = _json(raw, MAX_CATALOG_BYTES)
    if not isinstance(value, dict) or set(value) != {
        "version",
        "qualification",
        "roots",
    }:
        raise RootError(
            "coordination root catalog requires exactly version, qualification and roots"
        )
    if type(value["version"]) is not int or value["version"] != 1:
        raise RootError("unsupported coordination root catalog version")
    qualification = value["qualification"]
    if not isinstance(qualification, dict) or set(qualification) != {
        "storage_namespace",
        "topology_epoch",
        "coverage",
        "path_semantics",
        "alias_closed",
        "families_disjoint",
        "effects",
    }:
        raise RootError("invalid coordination root qualification fields")
    namespace = _key(qualification["storage_namespace"])
    epoch = _key(qualification["topology_epoch"])
    if (
        qualification["coverage"] != "whole-entry"
        or qualification["path_semantics"]
        != "posix-case-sensitive-normalization-preserving-v1"
        or qualification["effects"] != "declared-checkout-files-only"
        or qualification["alias_closed"] is not True
        or qualification["families_disjoint"] is not True
    ):
        raise RootError("unsupported coordination root qualification")
    rows = value["roots"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROOTS:
        raise RootError("coordination root catalog requires 1–32 roots")
    entries = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "root_key",
            "tree_id",
            "prefix",
            "evidence_sha256",
        }:
            raise RootError("invalid coordination root catalog entry fields")
        key = _key(row["root_key"])
        if key in seen:
            raise RootError("duplicate coordination root catalog root_key")
        seen.add(key)
        entries.append(
            RootEntry(
                key,
                _key(row["tree_id"]),
                normalize_prefix(row["prefix"]),
                _digest(row["evidence_sha256"]),
            )
        )
    return RootCatalog(
        hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        tuple(entries),
        namespace,
        epoch,
    )


def parse_selector(raw: object) -> RootSelector:
    if (
        not isinstance(raw, dict)
        or not {"catalog_sha256", "root_key"} <= set(raw)
        or set(raw) - {"catalog_sha256", "root_key", "relative_prefix"}
    ):
        raise RootError("invalid coordination_root fields")
    return RootSelector(
        _digest(raw["catalog_sha256"]),
        _key(raw["root_key"]),
        normalize_prefix(raw.get("relative_prefix", "")),
    )


def resolve(raw: object, catalog: RootCatalog | None) -> tuple[str, str]:
    selector = parse_selector(raw)
    if catalog is None:
        raise RootError("coordination root catalog is not configured", kind="conflict")
    return catalog.resolve(selector)


def encode_binding(selector: dict | None, generation: str) -> str | None:
    if selector is None:
        return None
    return json.dumps(
        {
            "version": 1,
            **parse_selector(selector).as_dict(),
            "possession_generation": generation,
        },
        ensure_ascii=False,
    )


def read_binding(
    raw: object, generation: str, catalog: RootCatalog | None
) -> dict | None:
    if not isinstance(raw, str):
        return None
    try:
        value = _json(raw, MAX_BINDING_BYTES)
        if not isinstance(value, dict) or set(value) != {
            "version",
            "catalog_sha256",
            "root_key",
            "relative_prefix",
            "possession_generation",
        }:
            return None
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["possession_generation"] != generation
            or not _GENERATION.fullmatch(generation)
        ):
            return None
        selector = {
            key: value[key] for key in ("catalog_sha256", "root_key", "relative_prefix")
        }
        resolve(selector, catalog)
        return parse_selector(selector).as_dict()
    except RootError:
        return None
