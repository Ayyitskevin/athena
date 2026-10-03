# Fictional, self-contained examples

These are inert FICTIONAL documentary records, not genuine owner returns,
reviewer acceptance, Athena approval or runtime evidence. All sample source,
handoff and verdict files are committed here. SHA256/byte counts in example-v1.json
and example-v2.json are actually computed from those files, not opaque placeholders.

- [example-v1.json](example-v1.json): original source with a fictional spelling failure.
- [example-v2.json](example-v2.json): changed source with a separate fictional verdict,
  superseding only the exact spelling proposition; the original files remain.
- [Schema](evidence-manifest-v1.schema.json): standalone documentary validation,
  not an Athena server interface.

Root label `repository` means this repository's root, resolved within the reader's
existing read grant. All locators are repo-relative. The enclosing Git commit pins
files as a unit; internal git_ref is null to avoid circular commit self-reference.

From the repository root, an existing JSON Schema validator can check both:

```sh
jsonschema -V Draft202012Validator \
  -i docs/evidence-convention/example-v1.json \
  -i docs/evidence-convention/example-v2.json \
  docs/evidence-convention/evidence-manifest-v1.schema.json
```

A reader still independently checks referenced bytes, size and subject scope.
Format validation is not truth, independence, authorization or acceptance.
