# Local documentary reader and scoped projection — prototype

These are LOCAL prototypes in `scripts/documentary_review/`, outside the installed
Athena package, server and MCP. No endpoint, database, dispatch, retrieval or new
Athena schema is introduced. Any later product integration must follow the
API/MCP-first contract and separate review/admission. No adoption is implied.

`reader.read_manifest(raw_bytes, inputs, bindings)` consumes the existing
`docs/evidence-convention/evidence-manifest-v1.schema.json`, pinned by SHA256. The
fixed schema is the sole import-time file read; remote schema retrieval is refused.
After import it accepts only an explicit finite `(root, path) -> Artifact(bytes,
git_ref)` map. It never opens a record path (including symlinks), lists directories,
substitutes latest, executes a command, fetches a URL or writes Athena. Supplying the
permitted originals and corroborating Git provenance remain the caller's job.

It checks schema/version/unknown fields, 64-KiB and collection limits, duplicate
JSON keys/subject/check IDs, verdict references, line bounds, byte sizes/SHA256 and
caller Git-ref bindings. Missing originals are PARTIAL, changed bindings FAIL,
unsupported versions/fields UNSUPPORTED. Original raw bytes must be retained by the
caller: the receipt includes their digest, not a normalized replacement. Schema
validity and matching bytes are not truth, independence or approval.

The existing v1 `checks` fields have no structured runner/subject/result links.
Therefore the local caller supplies an **additional invocation index**, not fields
silently added to v1. Its digest is separately recorded, and every referenced ID
must exist among the manifest subjects:

```json
{"exact check name": {
  "command": "exact retained command, for inspection only",
  "cwd": "exact retained cwd",
  "runner_id": "runner-subject-id",
  "subject_ids": ["tested-subject-id"],
  "result_id": "retained-result-subject-id"
}}
```

The reader binds command/cwd to the retained check and runner/result/subjects to
supplied exact bytes. Missing binding remains PARTIAL; wrong cwd or changed runner
cannot inherit PASS. The caller must source that index from actual retained
observations; it is not authenticated invocation provenance. Shell/binary/version
and full transitive suite coverage remain limits unless explicitly supplied.
Commands are returned as inert strings for copy/inspection, never transformed into
an executable shell script. No current behavioral test result is produced:
`behavior=UNRUN` always, even if integrity is PASS and a historical check says PASS.
Checks and their historical counts stay separate; overlapping suites are not summed.

`projection.project(manifest_bytes_list, inputs, bindings_by_manifest)` invokes the
reader itself before interpreting anything; a forged cached validation is not an
input. Each `(subject ID, exact scope)` is independent. Only explicit
`predecessors` edges with `supersedes_only`, matching exact predecessor/successor
record identities and exact proposition/scope can advance that scope. Authorship
of edges is human/analyst responsibility: no language model, timestamp or filename
chooses them. Branches, cycles, missing/disconnected/contradictory edges remain
PARTIAL. Unrelated edges do not override another scope. A unique linear terminal
record is a **current documentary label**, not a candidate verdict or permission.
History, old failed checks, limitations and exact artifacts remain accessible.
The projection is recomputed, never a second editable status store.

Attach the six [continuation answers](CONTINUATION.md) and their source handoffs to
the local report. The prototype deliberately always returns `actionable=false`
and `authentic_trigger=null`; it cannot authenticate a new grant. An old report's
hold is not silently cleared by a new date. No trigger means no active next step.

Proof: frozen succession and three-state fixtures plus positive/negative tests in
`tests/test_documentary_reader.py`. The real fleet pilot is retained outside the
public repository under the authorized report folder (no private fleet artifacts
copied into this repo). Benefits in reads/time saved remain UNMEASURED.
