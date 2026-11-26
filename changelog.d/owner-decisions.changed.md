- **The owner's open decisions are made**
  (`docs/decisions/2026-11-26-owner-decisions.md`). Transport encryption is the
  tailnet's, permanently: SECURITY.md says so in its own section and
  `athena-serve`'s `ATHENA_COOKIE_SECURE` refusal names it. Recovery is
  Linux-only: OPERATIONS.md says so, and `athena-doctor` now warns in every mode on
  a platform without Linux, procfs or `renameat2`, so the gap shows at setup
  rather than during a restore. No container image is published: docs/DOCKER.md
  says why. Every prune verdict is keep, with four subsystems on a watch list that
  names the evidence that would park each.
