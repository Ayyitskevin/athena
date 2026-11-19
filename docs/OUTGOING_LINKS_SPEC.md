# Reading outgoing links in one SQLite statement

An agent reading a page with 100 references should pay the same number of
database round trips as one reading a page with ten. Resolve the links for
one source and the reader's access to their targets in one SQLite statement.
Apply this to the core reader for both page and issue sources. The page API
continues to check the reader's access to the source before listing its links.

## What a reader sees

- Identify the source and each target by both kind and numeric ID. Return each
  indexed edge once, ordered by target kind and then ascending numeric ID.
- Return the current target title and the existing `kind`, `id`, `title` and
  `exists` fields. A rename is visible on the next read.
- Use the current project or space to decide who may see the target. Public
  containers and shared backlog issues are visible to everyone. Private
  containers are visible to their creator, members and admins. Archiving an
  issue or page keeps its existing visibility semantics.
- All reader roles, including admins, omit missing targets. Internal callers
  that omit the actor receive broken references with `title: null` and
  `exists: false`. An explicitly anonymous reader gets the public view.

## Acceptance

Trace the core read on migrated SQLite with 10 and 100 targets for each source
kind. Count every statement between entry and return, excluding setup and HTTP
authentication. Require one in each case and check every returned record.
Exercise mixed target kinds, overlapping IDs, private memberships, missing
targets and title changes. Verify the installed page endpoint over HTTP with
synthetic readers. This change requires no migration or link-index rebuild.
