# Deployment templates

These files are reviewable starting points, not an installer. They deliberately
contain placeholders and no machine addresses, local account names, data paths, or secrets.
Replace every `@ATHENA_*@` token in a copy, keep the rendered units outside the Git
checkout, and inspect the complete result before enabling it.

`systemd/athena.service.in` uses `athena-serve`, Athena's supported fail-closed
launcher, under a dedicated service user and a read-only system/home sandbox. Only
the rendered `@ATHENA_DATA_DIR@` is writable. Put deployment configuration in the
host-local `athena.env` referenced by the rendered unit, restrict that file to the service account, and follow
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md) for the required database, attachment,
network, authority, recovery, cookie, and request-limit posture. Never put the
rendered environment file in Git.

`systemd/athena-provenance.service.in` and `athena-provenance.timer` compare the
running process's `/version` snapshot with the deployed checkout every five minutes.
The check fails when the commits differ, either tree is dirty, the process did not
start from a Git checkout, or the response is malformed. Its nonzero exit is visible
in the journal and can be connected to the host's existing `OnFailure=` alert path.
It does not restart, modify, or deploy Athena.

Before installation:

1. Render copies and confirm no `@ATHENA_*@` placeholders remain.
2. Run `systemd-analyze verify` against all rendered system units.
3. Run `scripts/check_runtime_provenance.py` manually after starting Athena.
4. Enable the timer only after its first oneshot run exits zero.

The templates do not replace verified recovery bundles, off-host copies, readiness
monitoring, journal review, or the human gate for production changes.
