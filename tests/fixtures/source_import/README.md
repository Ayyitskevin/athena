# Source import fixtures

Small JSON exports used by `tests/test_source_import.py` and
`athena-validate-source`.

| File | Shape |
|------|--------|
| `jira_cloud_search.json` | Jira Cloud issue search (`expand` + `issues[]`) with comments, components, attachments metadata, links |
| `confluence_cloud_content.json` | Confluence content search (`results[]`) with storage HTML |

Real operator dumps can be validated without writing Athena:

```bash
athena-validate-source jira-project /path/to/export.json
athena-validate-source confluence-space /path/to/export.json
```

Attachment **blobs** are never imported by the mapper; the mapping report lists
them under `attachment_manifest` (name/size/url only).
