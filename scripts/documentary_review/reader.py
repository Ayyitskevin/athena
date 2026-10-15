"""Finite supplied-byte reader of the existing evidence convention; no retrieval.

Only the fixed repository schema is read at import. The reader itself takes bytes,
never opens a record path, and never executes commands or resolves URLs. Git refs
are compared with caller-supplied provenance, not authenticated by this prototype.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import NoSuchResource


SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/evidence-convention/evidence-manifest-v1.schema.json"
)
SCHEMA_BYTES = SCHEMA_PATH.read_bytes()
SCHEMA_SHA256 = "232ce9c717a7d76137259bb1c00ebdbc9624c5bbd7e7e327ddd44efd90c67285"
if sha256(SCHEMA_BYTES).hexdigest() != SCHEMA_SHA256:
    raise RuntimeError("existing evidence schema changed; review this local consumer")


def _no_retrieval(uri):
    raise NoSuchResource(ref=uri)


VALIDATOR = Draft202012Validator(
    json.loads(SCHEMA_BYTES),
    format_checker=FormatChecker(),
    registry=Registry(retrieve=_no_retrieval),
)


@dataclass(frozen=True)
class Artifact:
    data: bytes
    git_ref: str | None = None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _records(value):
    if isinstance(value, dict):
        if {"root", "path", "sha256", "byte_count", "git_ref"} <= value.keys():
            yield value
        else:
            for child in value.values():
                yield from _records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _records(child)


def record_key(record):
    """Exact whole-file identity, independent of optional passage navigation."""
    return tuple(
        record[key] for key in ("root", "path", "sha256", "byte_count", "git_ref")
    )


def read_manifest(
    raw: bytes, inputs: dict[tuple[str, str], Artifact], bindings: dict | None = None
) -> dict:
    """Check an explicit finite map; unknown/missing data never triggers retrieval.

    bindings is a report-local invocation index, NOT an extension to Athena v1:
    check name -> exact command/cwd + runner/result/subject IDs already in subjects.
    Its digest is retained independently. Supplied declarations bind a retained
    observation, never prove that its command ran or transfer it to new inputs.
    """
    result = {
        "integrity": "PASS",
        "behavior": "UNRUN",
        "manifest_sha256": sha256(raw).hexdigest(),
        "schema_sha256": SCHEMA_SHA256,
        "errors": [],
        "records": [],
        "checks": [],
        "document": None,
    }

    def error(code, detail, status="FAIL"):
        result["errors"].append({"code": code, "detail": detail, "status": status})

    def finish():
        statuses = {entry["status"] for entry in result["errors"]}
        result["integrity"] = next(
            (s for s in ("UNSUPPORTED", "FAIL", "PARTIAL") if s in statuses), "PASS"
        )
        return result

    if len(raw) > 65536:
        error("SIZE", "manifest exceeds 64 KiB")
        return finish()
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError):
        error("JSON", "malformed, duplicate-key or excessively nested JSON")
        return finish()
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != "athena.evidence_manifest.v1"
        or type(doc.get("schema_version")) is not int
        or doc["schema_version"] != 1
        or doc.get("mode") != "documentary_only"
    ):
        error("SCHEMA", "unsupported schema/version/mode", "UNSUPPORTED")
        return finish()
    schema_errors = list(VALIDATOR.iter_errors(doc))
    if schema_errors:
        for failure in schema_errors:
            error(
                "SCHEMA",
                f"{list(failure.path)}: {failure.message}",
                "UNSUPPORTED"
                if failure.validator == "additionalProperties"
                else "FAIL",
            )
        return finish()
    result["document"] = doc
    subject_ids = [s["id"] for s in doc["subjects"]]
    if len(subject_ids) != len(set(subject_ids)):
        error("DUPLICATE_SUBJECT", "subject IDs must be unique")
    subjects = {s["id"]: s for s in doc["subjects"]}
    for verdict in doc["verdicts"]:
        ids = verdict["subject_ids"]
        if len(ids) != len(set(ids)) or not set(ids) <= subjects.keys():
            error(
                "SUBJECT_REFERENCE", "verdict references absent or duplicate subjects"
            )
    for edge in doc["predecessors"]:
        if edge["disposition"] == "supersedes_only" and edge["successor"] is None:
            error("MISSING_SUCCESSOR", edge["proposition"], "PARTIAL")
    checked = set()
    for record in _records(doc):
        navigation = (
            record_key(record),
            record.get("line_start"),
            record.get("line_end"),
        )
        if navigation in checked:
            continue
        checked.add(navigation)
        before = len(result["errors"])
        key = (record["root"], record["path"])
        artifact = inputs.get(key)
        if artifact is None:
            error("MISSING_ORIGINAL", f"{key}: exact original not supplied", "PARTIAL")
        else:
            if (
                len(artifact.data) != record["byte_count"]
                or sha256(artifact.data).hexdigest() != record["sha256"]
            ):
                error("BYTE_BINDING", str(key))
            if artifact.git_ref != record["git_ref"]:
                error("GIT_BINDING", f"{key}: caller provenance differs")
            start, end = record.get("line_start"), record.get("line_end")
            if start is not None or end is not None:
                if (
                    start is None
                    or end is None
                    or not 1 <= start <= end <= len(artifact.data.splitlines())
                ):
                    error("LINE_BOUNDS", str(key))
        result["records"].append(
            {
                "record": record,
                "integrity": "PASS"
                if len(result["errors"]) == before
                else result["errors"][-1]["status"],
            }
        )

    bindings = {} if bindings is None else bindings
    result["bindings_sha256"] = sha256(
        json.dumps(
            bindings, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()
    names = [c["name"] for c in doc["checks"]]
    if len(names) != len(set(names)):
        error(
            "DUPLICATE_CHECK", "check names must uniquely address invocation bindings"
        )
    if set(bindings) - set(names):
        error("UNKNOWN_CHECK_BINDING", "binding names an absent check")
    for check in doc["checks"]:
        before = len(result["errors"])
        status, exit_code = check["status"], check["exit_code"]
        if (
            (status == "UNRUN" and exit_code is not None)
            or (status == "PASS" and exit_code != 0)
            or (status == "FAIL" and (exit_code is None or exit_code == 0))
        ):
            error("CHECK_EXIT", check["name"])
        binding = bindings.get(check["name"])
        if binding is None:
            if status != "UNRUN":
                error("MISSING_CHECK_BINDING", check["name"], "PARTIAL")
        elif not isinstance(binding, dict) or set(binding) != {
            "command",
            "cwd",
            "runner_id",
            "subject_ids",
            "result_id",
        }:
            error("INVOCATION_MISMATCH", check["name"])
        else:
            ids = binding["subject_ids"]
            if (
                not isinstance(ids, list)
                or not ids
                or any(not isinstance(i, str) for i in ids)
                or len(ids) != len(set(ids))
                or not set(ids) <= subjects.keys()
                or not isinstance(binding["runner_id"], str)
                or binding["runner_id"] not in subjects
                or not isinstance(binding["result_id"], str)
                or binding["result_id"] not in subjects
            ):
                error("SUBJECT_REFERENCE", f"invocation {check['name']}")
            if (
                binding["command"] != check["command"]
                or binding["cwd"] != check["cwd"]
                or check["command"] is None
                or check["cwd"] is None
            ):
                error("INVOCATION_MISMATCH", check["name"])
            if not result["errors"][before:]:
                bound = {
                    record_key(subjects[i]["record"])
                    for i in [*ids, binding["runner_id"], binding["result_id"]]
                }
                failures = [
                    r["integrity"]
                    for r in result["records"]
                    if record_key(r["record"]) in bound and r["integrity"] != "PASS"
                ]
                if failures:
                    error(
                        "CHECK_ARTIFACT",
                        check["name"],
                        "FAIL" if "FAIL" in failures else "PARTIAL",
                    )
        result["checks"].append(
            {
                "name": check["name"],
                "reported_status": status,
                "command_for_inspection": check["command"],
                "cwd_for_inspection": check["cwd"],
                "exit_code": exit_code,
                "observed": check["observed"],
                "limits": check["limits"],
                "invocation": binding,
                "binding": "PASS"
                if len(result["errors"]) == before
                else result["errors"][-1]["status"],
            }
        )
    return finish()
