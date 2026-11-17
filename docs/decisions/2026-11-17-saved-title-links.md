# Saved title links retain page identity

**Status** Accepted on 2026-11-17 following independent specification review.

**Context** Reusing a renamed page's old title could send a conductor's browser and an agent reading the same issue to different runbooks.

**Decision** Record which page a bare title resolved to when the author saves an issue or page. Keep its ID until the author removes the token. Browser reads and agent work context use the saved identity and the viewer's current access.

**Consequences** Renaming a runbook requires no edits to referencing documents. Older data can lack enough evidence to recover a token's identity. Authors can repair those links by naming the page ID explicitly. A selective import can carry an identity only when its target is also in the bundle.

**Sources** Kevin requested this behavior and independent review on 2026-11-17. The [specification](../RUNBOOK_LINK_IDENTITY_SPEC.md) records the cases and verification criteria.

**Superseded-by** None.
