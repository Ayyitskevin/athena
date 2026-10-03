# Evidence manifest convention v1

Use this convention when freezing a documentary review subject or handing off
its result. It uses existing attachments, issue comments, runbook references and
handoff evidence pointers; it creates no Athena endpoint, native table, permission
or automatic consumer. The JSON examples are inert retained-record indexes.

## Freeze and read

1. Choose the finite exact subject, excluding rolling controls. Compute SHA256 and
   byte count from each original file. Record source refs separately from where
   the report itself lives. Completion: every subject has a unique id and exact
   byte locator, or a named missing-original limitation and PARTIAL result.
2. Bind each verdict to only its declared subject ids and scope. Record author and
   reviewer observations from their actual retained records. Completion: no
   verdict for A is assigned to changed B, and independence is a sourced
   observation rather than an inferred property of a label/model.
3. Record actual checks, exits and UNRUN limits; retain FAILs and narrow successors.
   Completion: old failures remain accessible, with exactly the proposition a
   successor replaces. A timestamp alone supersedes nothing.
4. Freeze the manifest; hash it separately, without self-hashing. If a later,
   separately authorized upload occurs, use Athena's returned attachment id,
   SHA256 and size for those uploaded bytes. Reference that id and digest from
   the existing issue/comment/handoff. Completion: local originals and uploaded
   bytes are distinguished. This recipe itself authorizes no upload/write.
5. A reader first checks the exact schema/version, then independently retrieves
   permitted files and checks every relevant byte tuple and scope. Completion:
   unsupported formats are refused; missing/clipped/changed originals remain
   explicit, rather than silently substituting latest files or excerpts.

## Shape and field meanings

The [inert JSON Schema](evidence-convention/evidence-manifest-v1.schema.json)
describes `athena.evidence_manifest.v1`, integer `schema_version: 1`, and
`mode: documentary_only`. Unknown versions/schema ids/fields are unsupported
under this convention: keep the original and return UNSUPPORTED/PARTIAL without
interpreting its verdicts. This is a reader rule, **not implemented server
validation**. A later format requires an explicit documented reader update.

| Field | Meaning |
|---|---|
| `id`, `recorded_at` | Documentary identity and time of this index, not work ownership or historical decision time. |
| `handoff` | Exact retained handoff used for this pilot/continuation; its prose remains advisory. |
| `subjects` | Unique `id`, exact `record`, and finite `scope` for each reviewed item. |
| `source_refs` | Declared code/document source refs and their provenance. These do not imply the workspace report is committed at that ref. |
| `verdicts` | Exact verdict file, subject ids, reported verdict and scope. Empty means none supplied for this subject, never implied PASS. |
| `observations` | Role, recorded actor/session labels, evidence locator and observation scope. Not authentication, independence or approval attestation. |
| `checks` | Name, PASS/FAIL/PARTIAL/UNRUN, command/cwd, actual exit (null if unrun), observed result and limits. This field distinguishes today's checks from quoted historical counts. |
| `predecessors` | Exact predecessor, precise proposition, disposition (`context_only` or `supersedes_only`) and exact successor or null. |
| `rolling_controls` | Separately dated locator/read scope for mutable controls, never folded into a historical review subject by inference. |
| `limitations` | Missing originals, unrechecked underlying evidence, unrun runtime/acceptance, visibility and custody limits. |

A `record` has root label, root-relative path, full SHA256, byte count and
`git_ref` (full 40 hex when the file itself is bound to that Git ref, otherwise
null). Optional line bounds describe the relevant passage but the digest binds
the complete file. Resolve root labels out of band within the existing read
grant; labels are not a coordination-root contract. Absolute paths,
parent traversal and arbitrary fetching are outside this format. A null Git ref
must have its reason recorded in limitations. Declared code inputs belong in
`source_refs`, not invented Git membership for a report.

When an original is missing but its declared tuple survives, retain that tuple
and mark retrieval PARTIAL. When the tuple itself is unknown, return a PARTIAL
note alongside the original record instead of inventing a hash/size or calling
an incomplete object a sealed v1 manifest.

Bound v1 to 64 KiB encoded UTF-8, at most 20 subjects, 20 verdicts, 20
observations, 40 checks, 10 predecessors, 10 rolling controls and 20 limitations.
Every verdict subject id must exist and every subject id must be unique. Reject
duplicates/inconsistent line bounds and oversize objects; JSON Schema alone
does not establish those semantic checks, byte identity, or verdict truth.

## Existing ownership and limits

Attachment metadata and bytes already belong to
[`attachments.py:335–379`](../src/athena/core/attachments.py#L335-L379): size and
SHA256 are computed from uploaded data. Metadata plus `added_attachment` audit
are committed by
[`attachment_commands.py:42–92`](../src/athena/core/attachment_commands.py#L42-L92).
Server-computed digests describe uploaded bytes; externally supplied source
digests remain declarations until checked. Download retains container visibility
[`attachments_api.py:42–124`](../src/athena/core/attachments_api.py#L42-L124).
Do not copy hidden evidence, metadata or counts into a more visible record.

The [work-context reader](WORK_CONTEXT.md#bounds-and-ordering) already exposes
attachment metadata and bounded comments; clipping is feedback, not loss proof.
Attachment storage does not prove original custody, truth, permanent retention,
reviewer independence or approval. Existing comments may be edited; preserve a
frozen exact file/attachment rather than treating a comment id as immutable bytes
([comment owner](../src/athena/aegis/comments.py#L49-L61)).

The two [fictional examples](evidence-convention/) are self-contained spelling
records. Their source, handoff and verdict labels are FICTIONAL, never genuine
owner returns, reviewer acceptance or Athena approval. Their file digests and
byte counts are real, computed documentary checks. They establish no runtime
result, retrieval benefit or new evidence card/checkpoint field.
