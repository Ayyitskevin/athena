# Reading backlinks in one SQLite statement

The “Referenced by” list on an issue or page shows which other issues and
pages link to it. Read that list in one SQLite statement for any number of
incoming links, using the existing target index. Both API routes check access
to the target before asking for its backlinks.

## Reader contract

- Match the target by kind and ID. Return each indexed source once, ordered
  by source kind and then ascending numeric source ID. Issues and pages can
  share an ID. An empty list is valid.
- Return the source's kind, ID, current title and boolean `exists`. A rename,
  an issue's move into or out of a project, and a change in container
  membership take effect on the next read. Moving a page within its space
  preserves its access rules. An archived source retains its usual permissions.
- Any reader can see public sources and shared backlog issues. Private
  sources are visible to the container creator, its members and admins.
  Reader-facing lists omit missing sources. Internal callers that omit the
  actor keep dangling references with a null title and `exists: false`.

## Acceptance

Use migrated SQLite and count all statements issued by the core reader
after fixture setup. For each target kind, require one statement for 10
incoming links and one for 100. Compare every returned record with the
expected sources, including current titles, order and role-specific visibility.
Check outgoing reads against their existing contract when sharing the query.

Exercise both `GET /pages/{id}/backlinks` and `GET /issues/{id}/backlinks`
with synthetic readers. Measure full authenticated requests separately at
both sizes, check that private sources are filtered, and verify a hidden or
missing target still returns 404. Existing indexed links need no rebuild.
