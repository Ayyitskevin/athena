# OpenCode comment workflow

Athena's `.github/workflows/opencode.yml` lets the repository owner invoke
OpenCode from an issue or pull-request review comment with `/oc` or `/opencode`.
This is an owner-operated engineering aid, not part of Athena's application
runtime or supported deployment surface.

## Operator threat model

The owner identity gate authorizes a run; it does not make repository context
trusted. Issue and pull-request bodies, comments, reviews, diffs, and the target
branch may all be contributor-controlled. Inspect that context before invoking
the workflow.

OpenCode is an unsandboxed agent. During an authorized run it can use shell,
file, network, and GitHub tools while the process holds a provider key and can
exchange the job's OIDC identity for a short-lived, write-capable OpenCode
GitHub App token. OpenCode's permission prompts are policy controls, not an OS
sandbox. The controls below prevent contributor configuration from executing
automatically during initialization; they cannot make an adversarial prompt or
agent tool call safe. A human owner remains the execution gate.

## Execution controls

- Both the original `github.actor` and the current `github.triggering_actor`
  must be `github.repository_owner`. This prevents another repository writer
  from replaying an owner-authorized historical run. The first step is trusted
  inline Python that authorizes `/oc` or `/opencode` only at the start of the
  comment or after whitespace, and only before whitespace or end-of-comment.
  Lookalikes such as `/occult`, `/opencodeWhatever`, and `prefix/oc` do not
  proceed to checkout, download, or secret exposure.
- Checkout uses the immutable `github.workflow_sha`, never the event's
  potentially attacker-controlled pull-request ref. OpenCode later obtains the
  target context using its short-lived App token.
- The job names `ubuntu-24.04` rather than the moving `ubuntu-latest` alias.
  GitHub's hosted image is still a mutable external base, but a future default
  OS migration cannot silently change this lane's platform. The job also fails
  before secret exposure if the image ever supplies managed `/etc/opencode`
  configuration that could override repository controls.
- Before any target-branch checkout, Git sparse-checkout persistently excludes
  every `opencode.json`, `opencode.jsonc`, and `.opencode` path at any depth.
  The documented flags close the legacy GitHub-run path, but the exact
  binary's newer internal location-service stack does not consult them.
  Persistent sparse checkout is the fail-closed control that keeps those paths
  absent during branch checkout and initialization. This lane cannot safely
  review or edit quarantined paths; the unsandboxed agent can intentionally
  change sparse-checkout state, so use manual review for such changes.
- `HOME`, `TMPDIR`, and every XDG directory are isolated under the hosted
  runner's private temporary directory. The documented project-config and
  pure-mode flags remain defense in depth; default auth plugins, external
  skills, Claude Code imports, model-catalog refreshes, auto-update, session
  sharing, inherited OTLP export, all LSP servers, and formatter execution are
  disabled. An empty auth document prevents inherited credentials from being
  loaded, and the OIDC exchange endpoint is fixed explicitly.
- The isolated OpenCode cache, including its `bin` and `packages` paths, is
  pre-created and made read-only before startup. The pinned provider and
  ripgrep paths do not need cache writes. Any missed automatic model, skill,
  npm-package, or helper-binary materialization therefore fails closed instead
  of becoming executable code.
- Athena's trusted `AGENTS.md` is copied from the `github.workflow_sha` checkout
  alongside a minimal formatter- and LSP-disabled config in an isolated global
  OpenCode config directory, which is then made read-only. This preserves
  repository instructions while preventing the startup dependency installer
  from materializing mutable plugin packages.
- `GIT_LFS_SKIP_SMUDGE=1` is inherited by later Git operations so a contributor
  branch cannot trigger Git LFS downloads while provider and App credentials are
  live. Changes that require LFS materialization need a separate manual review.
- The fixed `opencode/claude-sonnet-4-6` model resolves from the binary's
  embedded model catalog to its bundled provider implementation; the workflow
  does not fetch a newer catalog or provider package during initialization.
- The job has `contents: read` for checkout and `id-token: write` for the App
  exchange. `USE_GITHUB_TOKEN=false` prevents use of the job token for writes.
  The provider key is exposed only to the final, authorized execution step.
- OpenCode's combined log stream passes through trusted code copied from the
  `github.workflow_sha` checkout into the read-only config directory. It masks
  Basic/Bearer authorization values and raw GitHub token formats that GitHub
  cannot reliably redact after transformation, while shell `pipefail` preserves
  the agent's failure status. This limits accidental log disclosure; it cannot
  stop the unsandboxed agent from intentionally using or exfiltrating authority.
- Session sharing is disabled with `SHARE=false`. Per-target concurrency is
  job-scoped behind the owner gates, so an unauthorized public comment cannot
  cancel an active owner run. Only one authorized job per target runs at once,
  and `cancel-in-progress=false` protects that running mutation. GitHub may
  replace an older pending job when another is queued; a 20-minute timeout
  remains the hard execution bound. Hard spend and rate limits remain
  provider-side owner controls.

## Pinned release closure

Before the agent starts, the workflow downloads only the OpenCode `v1.18.10`
Linux x64 and ripgrep `15.1.0` x86_64 Linux musl release assets over HTTPS with
bounded retries, transfer time, and size. The repository-owned installer reads
each verified archive through an anonymous snapshot and writes only the
selected regular file to a new private directory.

For OpenCode it requires:

- archive size `59,327,159` bytes and SHA-256
  `6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976`;
- exactly one root archive member named `opencode`, with no symlink, traversal,
  nested path, or extra member;
- binary size `179,206,272` bytes and SHA-256
  `2735f786be499db50c823d961fb8627dfb74f920e2320686b67e6c5c81c66f16`;

For ripgrep it requires:

- archive size `2,263,077` bytes and SHA-256
  `1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599`;
- exactly 16 archive members and the regular member
  `ripgrep-15.1.0-x86_64-unknown-linux-musl/rg`;
- binary size `5,445,512` bytes and SHA-256
  `ebeaf56f8a25e102e9419933423738b3a2a613a444fd749d695e15eba53f71f2`.

Both installs require a new private directory and no-clobber destination. The
final step repeats both size/digest checks, checks versions `1.18.10` and
`15.1.0`, and proves that `rg` resolves to the private verified binary before
OpenCode starts. This prevents OpenCode's built-in ripgrep fallback from
downloading and executing an unchecked release asset during the secret-bearing
process.

No cache action, moving release lookup, remote install script, or third-party
composite action participates in the execution path.

Hashes prove exact asset bytes, not source-to-binary provenance. GitHub's API
marks the `v1.18.10` release immutable and returned a Sigstore bundle containing
an in-toto Release Attestation v0.2 that binds this exact archive digest and tag
to commit `7902e04c3a67f7c69726bc955efb46e29214c797`. During this review the bundle
was observed through GitHub's attestations API but not independently reverified
by the local CLI. The release commit/tag itself is unsigned, and the release
attestation is not reproducible-build or source-to-binary provenance.

The pinned binaries, mutable hosted-runner image, OpenCode API and GitHub App,
model provider, billing controls, and the agent's intentional network/tool
execution therefore remain explicit external trust boundaries.
Upstream App-token cleanup is best-effort: a forced timeout can bypass its
revocation path. No later workflow step consumes the checked-out repository,
and hosted-runner teardown is the final containment boundary, but operators
must still use issuer-side controls during an incident.

## Upgrade procedure

Treat every OpenCode or ripgrep upgrade as security-sensitive. Record the exact
release URL, release API asset digest, archive size/digest, selected member and
member count, extracted binary size/digest, and reported version. For OpenCode,
also record the `github run --help` result, release tag/commit state, and any
attestation. Evidence precedence is: official release/attestation API metadata,
locally downloaded bytes, locally extracted bytes, then CLI output.

For this pin, the core evidence can be reproduced with:

```bash
gh api repos/anomalyco/opencode/releases/tags/v1.18.10
gh api 'repos/anomalyco/opencode/attestations/sha256:6b1113da704253fb4da12b41e4236acecb9f2b62949c945f6eeacaa15111b976'
gh api repos/BurntSushi/ripgrep/releases/tags/15.1.0
sha256sum opencode-linux-x64.tar.gz
stat --format='%s' opencode-linux-x64.tar.gz
tar --list --verbose --file opencode-linux-x64.tar.gz
sha256sum ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz
tar --list --verbose --file ripgrep-15.1.0-x86_64-unknown-linux-musl.tar.gz
```

Install both assets with `.github/scripts/install_verified_opencode.py`, then
independently check their installed size/digest, `opencode --version`,
`opencode github run --help`, and `rg --version`. Update the workflow, this
document, and `tests/test_opencode_workflow_policy.py` together. Require human
review before publication or any billable live run.

This review used both real release assets to verify archive/binary identity,
private modes, reported versions, and `github run --help`. Adversarial archive
cases use synthetic fixtures, and actionlint validates workflow structure. No
billable `github run` was executed.

## Incident procedure

1. Cancel every active OpenCode workflow run, then disable the workflow. Confirm
   that no run remains queued or in progress.
2. Revoke or rotate the provider credential at its issuer. Removing the GitHub
   Actions secret alone does not neutralize a key already injected into a
   process.
3. Suspend or uninstall the OpenCode GitHub App and revoke any controllable App
   sessions or tokens.
4. Preserve workflow logs, provider audit/billing records, and GitHub App audit
   evidence before cleanup.
5. Inventory and contain branches, commits, pull requests, comments, reviews,
   and reactions created or changed by the run. Any rollback is human-approved.
6. Re-enable only after no run is active, the provider key is revoked, App
   authority is controlled, repository mutations are accounted for, and the
   triggering defect has been reviewed and fixed.
