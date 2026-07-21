# OpenCode workflow security

`.github/workflows/opencode.yml` runs a repository-writing agent in response to
comment commands. These controls are part of the repository. They do not prove
that external billing, GitHub App, secret, or repository settings are configured.

## Enforced in the workflow

- GitHub's `comment.author_association` must be `OWNER`, `MEMBER`, or
  `COLLABORATOR`; public commenters with `NONE`, `CONTRIBUTOR`, or
  `FIRST_TIME_CONTRIBUTOR` are rejected before a runner is allocated.
- OpenCode is downloaded as the fixed `v1.18.4` Linux x64 release archive from
  its exact GitHub release URL. The archive must match the hard-coded SHA-256
  before extraction, and the extracted executable must report version `1.18.4`.
- The workflow invokes the verified CLI directly with `opencode github run`.
  `USE_GITHUB_TOKEN: "false"` preserves OIDC/App-token authorization and
  `SHARE: "false"` prevents public session sharing.
- The CLI performs a second authorization check and accepts only actors with
  GitHub `write` or `admin` repository permission.
- The job-scoped `GITHUB_TOKEN` has `contents: read` only, checkout credentials
  are not persisted, and checkout is pinned to a full signed commit SHA.
- One run is allowed per repository concurrency group. A newer trusted command
  cancels an older run, and `timeout-minutes` caps one run at 30 minutes.

## Fixed release and provenance evidence

Evidence was checked against the authoritative GitHub repositories and release
API on 2026-07-20:

- Archive URL:
  `https://github.com/anomalyco/opencode/releases/download/v1.18.4/opencode-linux-x64.tar.gz`
- GitHub release API digest:
  `sha256:bab463c3fb3224d388bb7cfad63f38703df9cf0be2cfd2ce8cb49d886b53a174`
- The downloaded 59,265,621-byte archive matched that digest, contained one
  executable named `opencode`, and the extracted binary reported `1.18.4`.
- `actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803`
  is the signed commit targeted by the authoritative `v6` tag.

The OpenCode release tag ref resolves to
`49c69c5ed3ccf706b61b3febb43c8aaff7f8325e`, whose GitHub verification state is
`unsigned`. Release metadata separately reports `target_commitish`
`4872c48c230728150e8e3406722943450ed58dcb`; that does not make the tag target
signed. The pinned asset digest proves that the downloaded bytes equal the
reviewed release asset. It does **not** establish signed source provenance or a
reproducible source-to-binary build. If either is required, keep this workflow
disabled.

## Why OIDC remains enabled

`id-token: write` is required by the fixed CLI's GitHub integration. Version
`1.18.4` calls `core.getIDToken("opencode-github-action")` and exchanges that
OIDC token for a short-lived OpenCode GitHub App token. Removing the permission
makes the integration fail. `USE_GITHUB_TOKEN: "false"` is explicit because
the alternative would require broader repository-write permissions on
`GITHUB_TOKEN` and bypass the CLI's actor-permission check.

## Why session sharing is disabled

OpenCode `v1.18.4` shares sessions for public repositories when `SHARE` is
unset. Its `SHARE=false` path returns before calling the session-sharing service.
`SHARE: "false"` is therefore a required privacy control, not an optional
preference.

## Cost and emergency controls

The association gate, concurrency cancellation, download timeouts, checksum
failure, and job timeout reduce accidental runs, overlap, and runaway duration.
They are **not** a request-rate limit or provider spending ceiling.

The maintainer must supply the real cost boundary outside this repository:

1. Use a dedicated OpenCode credential with the lowest practical prepaid balance
   or provider-enforced hard budget/rate limit. If no acceptable hard limit is
   available, keep this workflow disabled.
2. For an immediate stop, disable the workflow in GitHub Actions and remove the
   `OPENCODE_API_KEY` secret. Suspending or uninstalling the OpenCode GitHub App
   separately revokes its repository-writing path.
3. Review Actions runs and provider usage after enabling it. Credential rotation,
   billing changes, and App/settings changes require the maintainer.

## Forbidden regressions

Do not reintroduce any of the following:

- `anomalyco/opencode/github@...` or another composite OpenCode action.
- A moving `actions/cache@v4`, any other moving action tag, or `@latest`.
- `curl | bash`, `opencode.ai/install`, or any installer that resolves a current
  version at runtime.
- An OpenCode archive without an exact version, SHA-256 check, executable check,
  and exact version check.
- A run without explicit `USE_GITHUB_TOKEN: "false"` and `SHARE: "false"`.

## Updating the fixed release

Treat a release update as a security-sensitive change. Reverify all metadata and
the downloaded bytes; never substitute a moving tag or installer:

```bash
gh api repos/anomalyco/opencode/releases/tags/<version>
gh api repos/anomalyco/opencode/git/ref/tags/<version>
gh api repos/anomalyco/opencode/commits/<tag-target-sha>
curl --fail --show-error --silent --location --proto '=https' \
  --proto-redir '=https' --output /tmp/opencode.tar.gz <exact-asset-url>
sha256sum /tmp/opencode.tar.gz
tar -tzvf /tmp/opencode.tar.gz
```

Compare the local digest with the GitHub release API `digest`, extract to a
disposable directory, verify `opencode --version`, inspect the matching GitHub
CLI source for OIDC, authorization, and sharing behavior, then run `actionlint`,
the repository's policy tests, and `git diff --check`. Human review remains
required.
