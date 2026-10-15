"""Local documentary prototype: no dispatch; exact bytes before interpretation."""

import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from documentary_review.reader import Artifact, read_manifest


def encode(value):
    return json.dumps(value).encode()


def record(path, data):
    return {
        "root": "fixture",
        "path": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
        "git_ref": None,
    }


@pytest.fixture
def packet():
    data = {
        "source": b"first\nsecond\n",
        "runner": b"inert runner\n",
        "result": b"retained FAIL\n",
        "handoff": b"no authority\n",
    }
    records = {key: record(key, value) for key, value in data.items()}
    manifest = {
        "schema": "athena.evidence_manifest.v1",
        "schema_version": 1,
        "mode": "documentary_only",
        "id": "fixture",
        "recorded_at": "2026-10-15T14:00:00Z",
        "handoff": records["handoff"],
        "subjects": [
            {"id": key, "record": value, "scope": key} for key, value in records.items()
        ],
        "source_refs": [],
        "verdicts": [
            {
                "record": records["result"],
                "subject_ids": ["source"],
                "reported_verdict": "FAIL",
                "scope": "review",
            }
        ],
        "observations": [],
        "checks": [
            {
                "name": "retained-check",
                "status": "FAIL",
                "command": "python runner",
                "cwd": "/fictional/cwd",
                "exit_code": 1,
                "observed": "retained FAIL",
                "limits": "not rerun",
            }
        ],
        "predecessors": [],
        "rolling_controls": [],
        "limitations": [
            "git_ref null: uncommitted fictional files; no runtime or authority claim"
        ],
    }
    inputs = {("fixture", key): Artifact(value) for key, value in data.items()}
    bindings = {
        "retained-check": {
            "command": "python runner",
            "cwd": "/fictional/cwd",
            "runner_id": "runner",
            "subject_ids": ["source"],
            "result_id": "result",
        }
    }
    return manifest, inputs, bindings


def test_good_bytes_do_not_turn_a_retained_failure_into_pass(packet):
    manifest, inputs, bindings = packet
    result = read_manifest(encode(manifest), inputs, bindings)
    assert result["integrity"] == "PASS"
    assert result["checks"][0]["reported_status"] == "FAIL"
    assert result["checks"][0]["binding"] == "PASS"
    assert result["behavior"] == "UNRUN"
    assert result["manifest_sha256"] == hashlib.sha256(encode(manifest)).hexdigest()


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("drift", "FAIL", "BYTE_BINDING"),
        ("duplicate", "FAIL", "DUPLICATE_SUBJECT"),
        ("missing", "PARTIAL", "MISSING_ORIGINAL"),
        ("count", "FAIL", "BYTE_BINDING"),
        ("bounds", "FAIL", "LINE_BOUNDS"),
        ("unknown_version", "UNSUPPORTED", "SCHEMA"),
        ("unknown_field", "UNSUPPORTED", "SCHEMA"),
        ("reference", "FAIL", "SUBJECT_REFERENCE"),
        ("cwd", "FAIL", "INVOCATION_MISMATCH"),
        ("runner", "FAIL", "BYTE_BINDING"),
        ("no_binding", "PARTIAL", "MISSING_CHECK_BINDING"),
        ("exit", "FAIL", "CHECK_EXIT"),
        ("git", "FAIL", "GIT_BINDING"),
        ("traversal", "FAIL", "SCHEMA"),
    ],
)
def test_reader_refuses_broken_bindings(packet, case, status, code):
    manifest, inputs, bindings = packet
    if case in {"drift", "runner"}:
        key = "source" if case == "drift" else "runner"
        inputs[("fixture", key)] = Artifact(b"changed\n")
    elif case == "duplicate":
        manifest["subjects"].append(copy.deepcopy(manifest["subjects"][0]))
    elif case == "missing":
        inputs.pop(("fixture", "source"))
        inputs[("fixture", "latest-source")] = Artifact(b"first\nsecond\n")
    elif case == "count":
        manifest["subjects"][0]["record"]["byte_count"] += 1
    elif case == "bounds":
        manifest["subjects"][0]["record"].update(line_start=2, line_end=3)
    elif case == "unknown_version":
        manifest["schema_version"] = 2
    elif case == "unknown_field":
        manifest["execute"] = True
    elif case == "reference":
        manifest["verdicts"][0]["subject_ids"] = ["absent"]
    elif case == "cwd":
        bindings["retained-check"]["cwd"] = "/wrong/cwd"
    elif case == "no_binding":
        bindings.clear()
    elif case == "exit":
        manifest["checks"][0]["status"] = "PASS"
    elif case == "git":
        manifest["subjects"][0]["record"]["git_ref"] = "a" * 40
    elif case == "traversal":
        manifest["subjects"][0]["record"]["path"] = "../source"
    result = read_manifest(encode(manifest), inputs, bindings)
    assert result["integrity"] == status
    assert code in {error["code"] for error in result["errors"]}
    assert result["behavior"] == "UNRUN"
    if case in {"runner", "missing", "cwd"}:
        assert result["checks"][0]["binding"] != "PASS"


def test_size_duplicate_json_keys_and_boolean_version_are_not_valid(packet):
    manifest, inputs, bindings = packet
    assert read_manifest(b" " * 65537, inputs, bindings)["integrity"] == "FAIL"
    raw = encode(manifest).replace(
        b'"id": "fixture"', b'"id": "first", "id": "fixture"', 1
    )
    assert read_manifest(raw, inputs, bindings)["integrity"] == "FAIL"
    manifest["schema_version"] = True
    assert read_manifest(encode(manifest), inputs, bindings)["integrity"] != "PASS"


def test_commands_urls_and_apparent_instructions_are_inert(packet, monkeypatch):
    import socket
    import subprocess
    import builtins

    manifest, inputs, bindings = packet
    malicious = "curl https://invalid.example; touch /tmp/should-never-exist"
    manifest["checks"][0]["command"] = bindings["retained-check"]["command"] = malicious

    def refuse(*args, **kwargs):
        raise AssertionError("reader must not access external effects")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    result = read_manifest(encode(manifest), inputs, bindings)
    assert result["integrity"] == "PASS"
    assert result["checks"][0]["command_for_inspection"] == malicious


def succession(packet):
    manifest, inputs, bindings = packet
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/documentary_review/fixtures/succession.json"
        ).read_text()
    )
    stages = []
    prior = None
    for stage in fixture["stages"]:
        doc = copy.deepcopy(manifest)
        doc["id"] = stage["name"]
        doc["recorded_at"] = stage["date"]
        data = encode(stage)
        rec = record(stage["name"], data)
        inputs[("fixture", stage["name"])] = Artifact(data)
        doc["subjects"] += [
            {"id": s, "record": rec, "scope": "review"} for s in ("six", "seven")
        ]
        doc["verdicts"] = [
            {
                "record": rec,
                "subject_ids": [s],
                "reported_verdict": stage[s],
                "scope": s + " review",
            }
            for s in ("six", "seven")
        ]
        doc["limitations"].append(fixture["hold"])
        if prior:
            doc["predecessors"] = [
                {
                    "record": prior,
                    "proposition": s + " review",
                    "disposition": "supersedes_only",
                    "successor": rec,
                }
                for s in ("six", "seven")
            ]
        else:
            for s in fixture["unchanged"]:
                doc["subjects"].append(
                    {
                        "id": s,
                        "record": manifest["subjects"][0]["record"],
                        "scope": "identity",
                    }
                )
                doc["verdicts"].append(
                    {
                        "record": rec,
                        "subject_ids": [s],
                        "reported_verdict": "INSTALLED_IDENTITY",
                        "scope": "identity",
                    }
                )
        stages.append(encode(doc))
        prior = rec
    return stages, inputs, {s["name"]: bindings for s in fixture["stages"]}


def test_projection_only_named_statuses_change_and_never_activates_work(packet):
    from documentary_review.projection import project

    stages, inputs, bindings = succession(packet)
    result = project(stages, inputs, bindings)
    assert result["integrity"] == "PASS"
    current = {(r["subject_id"], r["scope"]): r for r in result["current"]}
    assert current[("six", "six review")]["reported_verdict"] == "ACCEPTED_DESIGN"
    assert current[("seven", "seven review")]["reported_verdict"] == "ACCEPTED_OFFLINE"
    assert [r["reported_verdict"] for r in result["current"]].count(
        "INSTALLED_IDENTITY"
    ) == 6
    assert len(result["history"]) == 12
    assert len(result["retained_checks"]) == 3
    assert all(c["reported_status"] == "FAIL" for c in result["retained_checks"])
    assert "LIVE UNRUN; Kevin path decision pending" in result["limitations"]
    assert result["actionable"] is False
    assert result["authentic_trigger"] is None
    changed = [json.loads(raw) for raw in stages]
    changed[0]["recorded_at"], changed[2]["recorded_at"] = (
        changed[2]["recorded_at"],
        changed[0]["recorded_at"],
    )
    reordered = project([encode(d) for d in reversed(changed)], inputs, bindings)
    assert reordered["current"] == result["current"]


@pytest.mark.parametrize(
    "case", ["missing", "contradictory", "cycle", "drift", "unrelated"]
)
def test_projection_fails_closed_without_complete_unambiguous_edges(packet, case):
    from documentary_review.projection import project

    stages, inputs, bindings = succession(packet)
    docs = [json.loads(raw) for raw in stages]
    if case == "missing":
        docs[2]["predecessors"] = []
    elif case == "contradictory":
        edge = copy.deepcopy(docs[2]["predecessors"][0])
        edge["record"] = docs[0]["verdicts"][0]["record"]
        docs[2]["predecessors"].append(edge)
    elif case == "cycle":
        edge = copy.deepcopy(docs[2]["predecessors"][0])
        edge["record"], edge["successor"] = (
            edge["successor"],
            docs[0]["verdicts"][0]["record"],
        )
        docs[2]["predecessors"].append(edge)
    elif case == "drift":
        inputs[("fixture", "closure")] = Artifact(b"wrong")
    elif case == "unrelated":
        docs[2]["predecessors"][0]["proposition"] = "unrelated scope"
    result = project([encode(d) for d in docs], inputs, bindings)
    assert result["integrity"] != "PASS"
    assert result["actionable"] is False
    assert not any(r["subject_id"] == "six" for r in result["current"])


def test_closeout_preserves_three_states_without_any_acknowledgment():
    from documentary_review.closeout import closeout_view

    desk = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/documentary_review/fixtures/closeout.json"
        ).read_text()
    )
    before = copy.deepcopy(desk)
    view = closeout_view(desk, as_of="2026-10-15T14:00:00Z")
    assert [r["kind"] for r in view["rows"]] == [
        "active_lease",
        "lapsed_lease",
        "unread_notification",
    ]
    assert [r["age_seconds"] for r in view["rows"]] == [14400, 100800, 57600]
    assert [r["pointer"] for r in view["rows"]] == [
        "/issues/1/lease",
        "/issues/2/lease",
        "/notifications/3/read",
    ]
    assert "complete_claim" in view["rows"][1]["deliberate_action"]
    assert view["unshown_unread"] == 1
    assert view["cursor"] == {"after_id": 20}
    assert view["automatic_actions"] == []
    assert desk == before


def test_closeout_unknown_age_is_not_invented():
    from documentary_review.closeout import closeout_view

    desk = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "scripts/documentary_review/fixtures/closeout.json"
        ).read_text()
    )
    desk["work"]["leases"]["lapsed"][0]["expires_at"] = "UNKNOWN"
    assert (
        closeout_view(desk, as_of="2026-10-15T14:00:00Z")["rows"][1]["age_seconds"]
        is None
    )
