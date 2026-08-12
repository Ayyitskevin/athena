"""The vendored htmx bundle is hash-pinned, like every other executable dependency.

Every Python dependency is exact-pinned and audited (constraints/,
test_supply_chain), and every GitHub Action is SHA-pinned (test_workflow_security).
htmx.min.js was the one executable dependency shipped by content alone: vendored
(no CDN, so no `integrity` attribute exists to check it), excluded from CodeQL's
analysis paths, and served to every browser session — so a tampered copy would
have ridden on PR-review vigilance alone. This pin turns a byte change to the
bundle into a red build instead: upgrading htmx becomes a deliberate act that
names the new version and its digest in the same diff.
"""

import hashlib
from pathlib import Path

# htmx 1.9.12 — digest verified against the official release artifact
# (bigskysoftware/htmx tag v1.9.12, dist/htmx.min.js). To upgrade: fetch the new
# dist file from the htmx release tag, verify it there, replace the vendored
# copy, and update the version in this comment and the digest below together.
HTMX_SHA256 = "449317ade7881e949510db614991e195c3a099c4c791c24dacec55f9f4a2a452"


def test_vendored_htmx_matches_pinned_digest():
    bundle = Path(__file__).resolve().parents[1] / "src/athena/static/htmx.min.js"
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    assert digest == HTMX_SHA256, (
        "src/athena/static/htmx.min.js no longer matches the pinned digest. If "
        "this is a deliberate upgrade, verify the file against the official htmx "
        "release tag, then update HTMX_SHA256 and the version comment beside it."
    )
