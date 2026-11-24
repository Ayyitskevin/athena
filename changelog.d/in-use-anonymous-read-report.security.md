- **The operator is told what an instance answers without a credential.**
  `athena-doctor` (plain and `--deployment` modes) now prints one
  `anonymous reads in this environment:` line: the `ATHENA_ANONYMOUS_READS`
  setting of the environment it runs in and whether it was set or defaulted;
  how many GET routes the credential gate lets a caller with no credential
  past (computed from the app's own routes, each asked the way its identity
  dependency decides, and stored with the release so the doctor does not
  build the app; a route past the gate may still ask the viewer to sign in or
  answer 404); the public projects and spaces, by key when the key has a
  project key's shape and as `#<id>` otherwise; and the reads no container
  scopes (issues with no project, labels, the activity feed and its CSV
  export, fleet metrics). The first start (`athena-serve --bootstrap`) prints
  one line saying what anonymous reads are. In tailnet mode `athena-serve`
  now refuses to start, and `athena-doctor --deployment` refuses, until
  `ATHENA_ANONYMOUS_READS` is set explicitly to `true` or `false` (D7). The
  default stays on (D23) and no route answers differently.
