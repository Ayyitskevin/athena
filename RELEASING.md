# Releasing Athena

Everything mechanical is automated. The two things that are not — accepting the
residual risk, and applying the tag — are deliberately left to a human, because
they are judgements rather than steps. This page is the order.

Read [`docs/RELEASE_READINESS.md`](docs/RELEASE_READINESS.md) first. Its promotion
checklist is the actual gate; this file only tells you how to work through the last
two boxes without getting them out of order.

## The version lives in two files

`pyproject.toml` and `src/athena/__init__.py` both carry it, and they drifting apart
is the classic release bug — the wheel says one thing and the running app reports
another. The publish workflow refuses to run when they disagree, and refuses a tag
that does not name the version being packaged. So a bump is one commit that changes
both, and nothing else:

```bash
# both, in the same commit
grep -n '^version' pyproject.toml
grep -n '^__version__' src/athena/__init__.py
```

While Athena is pre-1.0 the version line is `0.1.0aN`. A heading in the CHANGELOG
is a **milestone**, not a release; it becomes a release only once a matching tag
exists, and `## [Unreleased]` stays until you tag.

## Order

**1. Land everything.** The tag must point at a commit already on `main`. The
publish workflow checks this; it does not merge for you.

**2. Confirm hosted CI is green at that exact commit.** Not "green on the PR" —
green on the merge commit you are about to tag. The workflow re-checks `test`,
`audit` and `container` and refuses if any of them is missing or red, so this step
is a courtesy to yourself rather than the enforcement.

**3. Rehearse against TestPyPI.** Actions → Publish → Run workflow, and type the
version you expect (`0.1.0a1`). That types the version rather than clicking a
button on purpose: it is the last cheap moment to notice you are shipping something
other than what you think. The rehearsal builds the sdist, builds the wheel *from
that sdist*, verifies the wheel against both the checkout's verifier and the
sdist's own copy, and uploads to TestPyPI through the same trusted-publishing
mechanism the real thing uses.

Then install from TestPyPI into a clean environment and boot it — the point of a
rehearsal is the install, not the upload:

```bash
python3.12 -m venv /tmp/athena-rehearsal
/tmp/athena-rehearsal/bin/pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  athena==<version>
/tmp/athena-rehearsal/bin/athena-serve --bootstrap --host 127.0.0.1 --port 8000
```

**4. Update RELEASE_READINESS.md** with the evidence from the exact commit you are
about to tag — the commit SHA, the environment, the observed gate numbers. Land
that too. It describes the release, so it must be *in* the release.

**5. Accept the residual risk.** The list is in RELEASE_READINESS.md and it is not
waived by green tests: it is supply-chain, deployment-shape and repository-settings
risk. Reading it and deciding is the release owner's act. If you would not say the
list out loud to someone installing this, do not tag.

**6. Tag.** This is the trigger; nothing publishes without it.

```bash
git tag -a v0.1.0a1 -m "Athena 0.1.0a1 — local alpha" <merge commit>
git push origin v0.1.0a1
```

**7. Approve the deployment.** The tag starts the Publish workflow, which stops at
the `pypi` environment and waits. Approving it is the last human step; the upload
happens after.

**8. Tick the last two boxes** in RELEASE_READINESS.md's promotion checklist, and
move the CHANGELOG's `[Unreleased]` heading to the released version.

## One-time setup, before the first release

Neither of these exists yet, and the publish workflow cannot run until they do —
which is the correct state for a project that has never released.

- **A PyPI trusted publisher.** On PyPI, add a publisher for this repository with
  workflow `publish.yml` and environment `pypi`. Repeat on TestPyPI with
  environment `testpypi`. Trusted publishing means no API token is stored anywhere;
  the workflow mints a short-lived credential from GitHub's OIDC identity.
- **Two GitHub Environments**, `pypi` and `testpypi`, with required reviewers on
  `pypi`. That is what turns step 7 into a real pause rather than a formality.

## If something goes wrong

**A bad tag, before the environment is approved:** delete it and start again. The
job is waiting; nothing has been uploaded.

```bash
git tag -d v0.1.0a1 && git push origin :refs/tags/v0.1.0a1
```

**A bad release, after upload:** PyPI does not allow re-uploading a version, ever,
even after a delete. Yank it and ship the next number. This is why step 3 exists.

```bash
# on pypi.org: yank the release, then
# bump to the next version and release again
```

## What is not automated, and will not be

Signing and attestation, publishing the container image, and any promotion beyond
PyPI. Those are open decisions rather than missing work — see the guide's
`[OPERATOR DECISION]` items and `docs/DECISIONS_PENDING.md`.
