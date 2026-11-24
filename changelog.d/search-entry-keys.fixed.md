- **Creating an issue or a comment no longer scans the whole search index.**
  Every issue create, edit and comment deleted its old full-text entry by
  columns the index cannot look up, so each write read every entry ever
  written, and writes slowed as the workspace grew. A new `search_entries`
  table maps each issue, page and comment to its entry, so the write deletes
  by key. Migration `search_entry_keys` builds the map from the existing
  index in one transaction, without rewriting any text. Search results are
  unchanged on an index with one entry per source, which is every index only
  Athena has written; an older duplicate entry of a source, if one exists, is
  removed. Stop Athena before migrating: a process still running the previous
  release writes entries the map does not know.
