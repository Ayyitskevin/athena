"""Recomputed, exact-scope documentary projection; no writable status store."""

from collections import defaultdict
import json

from .reader import read_manifest, record_key


def project(manifests: list[bytes], inputs: dict, bindings_by_manifest: dict) -> dict:
    result = {
        "integrity": "PASS",
        "behavior": "UNRUN",
        "actionable": False,
        "authentic_trigger": None,
        "current": [],
        "history": [],
        "retained_checks": [],
        "limitations": [],
        "errors": [],
        "read_receipts": [],
    }
    if not 1 <= len(manifests) <= 20:
        result.update(integrity="FAIL", errors=["supply 1..20 explicit manifests"])
        return result
    # Parse IDs only to route an inert binding map. The reader independently
    # reparses, checks duplicate keys, schema and every byte binding below.
    for raw in manifests:
        try:
            parsed = json.loads(raw) if len(raw) <= 65536 else None
            name = parsed.get("id") if isinstance(parsed, dict) else None
        except (ValueError, UnicodeError, RecursionError):
            name = None
        result["read_receipts"].append(
            read_manifest(
                raw,
                inputs,
                bindings_by_manifest.get(name, {}) if isinstance(name, str) else {},
            )
        )
    if any(r["integrity"] != "PASS" for r in result["read_receipts"]):
        result.update(
            integrity="PARTIAL",
            errors=["unvalidated inputs: no current verdicts interpreted"],
        )
        return result
    docs = [r["document"] for r in result["read_receipts"]]
    if len({d["id"] for d in docs}) != len(docs):
        result.update(integrity="FAIL", errors=["duplicate manifest ID"])
        return result
    groups = defaultdict(dict)
    conflicts = set()
    for receipt in result["read_receipts"]:
        doc = receipt["document"]
        subjects = {s["id"]: s["record"] for s in doc["subjects"]}
        result["limitations"].extend(doc["limitations"])
        result["retained_checks"].extend(
            {**c, "manifest_id": doc["id"]} for c in receipt["checks"]
        )
        for verdict in doc["verdicts"]:
            for subject in verdict["subject_ids"]:
                group = (subject, verdict["scope"])
                node = {
                    "subject_id": subject,
                    "scope": verdict["scope"],
                    "subject_record": subjects[subject],
                    "record": verdict["record"],
                    "reported_verdict": verdict["reported_verdict"],
                }
                key = record_key(verdict["record"])
                prior = groups[group].get(key)
                if prior is not None and prior != node:
                    conflicts.add(group)
                groups[group][key] = node
                result["history"].append({**node, "manifest_id": doc["id"]})
    edges = [
        edge
        for doc in docs
        for edge in doc["predecessors"]
        if edge["disposition"] == "supersedes_only"
    ]
    used = set()
    for group, nodes in sorted(groups.items()):
        outgoing = {key: set() for key in nodes}
        incoming = {key: set() for key in nodes}
        for index, edge in enumerate(edges):
            old, new = record_key(edge["record"]), record_key(edge["successor"])
            if edge["proposition"] == group[1] and old in nodes and new in nodes:
                outgoing[old].add(new)
                incoming[new].add(old)
                used.add(index)
        roots = [key for key in nodes if not incoming[key]]
        valid = (
            group not in conflicts
            and len(roots) == 1
            and all(len(e) <= 1 for e in (*outgoing.values(), *incoming.values()))
        )
        walked = set()
        key = roots[0] if valid else None
        while key is not None and key not in walked:
            walked.add(key)
            following = outgoing[key]
            if not following:
                break
            key = next(iter(following))
        if not valid or len(walked) != len(nodes) or outgoing.get(key):
            result["errors"].append(
                f"ambiguous/missing/contradictory succession: {group}"
            )
        else:
            result["current"].append(nodes[key])
    for index, edge in enumerate(edges):
        if index not in used:
            result["errors"].append(f"unresolved explicit edge: {edge['proposition']}")
    result["limitations"] = sorted(set(result["limitations"]))
    result["history"].sort(
        key=lambda row: (row["subject_id"], row["scope"], row["manifest_id"])
    )
    result["current"].sort(key=lambda row: (row["subject_id"], row["scope"]))
    if result["errors"]:
        result["integrity"] = "PARTIAL"
    # Even a unique current label cannot grant an action. Human-authored six
    # continuation answers remain attached to exact handoffs, never dispatched.
    return result
