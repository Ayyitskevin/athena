# Decisions waiting on the release owner

Three items in the performance/adoption guide are marked **[OPERATOR DECISION]**.
They are not blocked on research or on engineering time — they are blocked because
each one changes what Athena *claims*, and a claim is the owner's to make.

This page exists so that making them is cheap: what is true today, what each option
actually costs, what it commits the project to, and how to walk it back. Each brief
ends with a recommendation. A recommendation is not the decision.

---

## 1. A supported TLS shape (F-1.2)

### Where things stand

`athena-serve` refuses `ATHENA_COOKIE_SECURE=1` outright
(`core/deployment.py:496`), because the supported contract is direct HTTP on
loopback or a tailnet. That refusal is honest rather than lazy: setting the flag
without terminating TLS somewhere would mark cookies HTTPS-only on a connection
that is not HTTPS, and the operator would silently lose their session instead of
gaining security.

So today, transport encryption comes from WireGuard/Tailscale or it does not exist.
That is a real security position, not a gap — but it is currently implied by a
refusal message rather than stated anywhere an installer would read it.

### The options

**(a) Bless one reverse-proxy recipe.** Ship an exact Caddy or nginx config in
OPERATIONS.md, add a named deployment mode that accepts `COOKIE_SECURE=1` plus
proxy-terminated HTTPS, and give that mode its own Host/authority contract.

- **Cost:** the config is the small part. The real work is the preflight: a new
  mode means new validation, a new set of authorities to reason about, `X-Forwarded-*`
  handling that Athena currently and deliberately ignores (the rate limiters key on
  the accepted peer, "never forwarding headers" — that comment is load-bearing and
  a proxy mode is exactly what would tempt someone to change it), and a matching
  row in every place the deployment claim is written down.
- **Commits us to:** supporting a shape where an untrusted network reaches a proxy
  that reaches Athena. Every future security answer has to hold in that shape too.
  RELEASE_READINESS.md currently holds the line at "one process on a trusted local
  machine or tailnet"; this moves that line.

**(b) Declare tailnet-only transport encryption permanent.** One loud sentence in
SECURITY.md, and point the launcher's refusal message at it.

- **Cost:** an hour.
- **Commits us to:** saying no to a class of user, clearly, forever-ish. It is
  reversible — (a) remains available later — but it should be written as a
  position, not an apology.

### Recommendation

**(b), now.** Athena's differentiator is the operator-and-fleet loop, not
deployment breadth, and (a) buys reach at the cost of widening the exact claim the
readiness doc has been careful about. The guide is explicit that (a) should not be
built speculatively, and I agree with that for a stronger reason than caution:
a proxy mode nobody is running is a mode nobody is testing, and an untested
security mode is worse than a refused one.

Take (a) when a real person is blocked on it, and let their actual deployment pick
the proxy.

---

## 2. Recovery portability, or documented Linux-only (F-3.3)

### Where things stand

`core/recovery.py` publishes recovery stages with `renameat2(RENAME_NOREPLACE)`
through `ctypes`, which is Linux-only; elsewhere it raises `ENOTSUP`. So
backup/restore — the feature a self-hoster most needs to be boring — does not work
on macOS or BSD.

### A correction to the guide's cost estimate

The guide proposes `os.link` + unlink as the portable no-replace fallback and calls
it "a day of work". **That technique does not apply here.** All three call sites
(`recovery.py:1886`, `:1953`, `:2113`) publish *directories* — the error string
even says "atomic no-clobber **directory** publication" — and `os.link` cannot
hardlink a directory on Linux, macOS or any BSD. There is no portable primitive
that gives atomic no-clobber directory rename.

What actually exists off-Linux:

- **`os.mkdir` as the claim.** `mkdir` is atomic and fails with `EEXIST`, so the
  destination directory can be created as the reservation and the contents moved
  in. This changes publication from one atomic step into create-then-populate,
  which means a crash can leave a *partially published* stage — the exact failure
  `RENAME_NOREPLACE` was chosen to make impossible.
- **Symlink swap.** Publish to a uniquely-named directory and atomically swap a
  symlink. Portable and genuinely atomic, but it changes the on-disk layout, and
  `recovery.py` already refuses to verify paths it cannot check without symlinks
  (`:1150`, `:1160`) — that stance would have to be revisited.
- **Accept a TOCTOU window.** Check, then rename. Cheap, and it silently reintroduces
  the race the current code is built to exclude.

So the honest estimate for option (a) is a design change to publication, not a day
of work — and it is a design change that trades away an atomicity guarantee the
Linux path currently has, on the platforms that get the fallback.

### The options

**(a) Portable publication.** Pick one of the three above, accept that non-Linux
platforms get a weaker guarantee than Linux, and document precisely which.

**(b) Declare Linux-only.** Say it in OPERATIONS.md, and make `athena-doctor`
report it on other platforms rather than letting a macOS user discover it when a
restore fails — which is the worst possible moment.

### Recommendation

**(b), with the doctor change treated as required rather than optional.** Athena's
supported deployment is already one Linux-ish box; recovery is where a silent
platform gap does the most damage, and the current failure mode — works until you
need it — is the bad one. (b) converts that into a message at install time.

If you want (a) later, the symlink swap is the only one of the three that keeps the
atomicity property, and it should be scoped as its own item with its own tests, not
folded into a portability fix.

---

## 3. Publishing the container image (F-2.3)

### Where things stand

The `Dockerfile`, `compose.yaml` and `docs/DOCKER.md` are in the tree, and CI builds
the image, refuses it if it runs as root, checks that a fresh volume is still
refused without an explicit bootstrap, waits for the image's own `HEALTHCHECK`, and
restarts it on an existing database. **Nothing is pushed anywhere.** That was
deliberate: building and testing an image is not speculative, publishing it is a
distribution decision.

### The options

**(a) Publish to GHCR on tag.** Add a job to `publish.yml` mirroring the PyPI one —
same tag trigger, same environment gate.

- **Cost:** small in code. The ongoing cost is the part that matters: a published
  image is a promise to rebuild it when a base-image CVE lands, independently of
  whether Athena itself changed. `python:3.12-slim` moves; the image you published
  in March is not the one people pull in July.
- **Commits us to:** a second artifact with its own lifecycle, and to being the
  answer when someone runs it in a shape `docs/DOCKER.md` lists as unsupported.

**(b) Ship the Dockerfile, publish nothing.** Users build locally with
`docker compose build`. CI keeps proving the file works.

- **Cost:** nothing ongoing.
- **Commits us to:** nothing. Users who want an image build one; the recipe is
  tested on every push.

### Recommendation

**(b) for now.** The image's main value here is that the deployment story is
*executable and tested*, and (b) delivers that in full. Publishing adds a
maintenance obligation that only pays off once there are people pulling it — and
there is a specific reason to wait: Athena has no supported public-network shape
(decision 1), so a published image would mostly be pulled by people trying to do
the thing the docs say not to.

Revisit when decision 1 goes the other way, or when someone asks for the image by
name.

---

## How to record a decision

Take the brief's recommendation or don't; either way, write the outcome where the
code will be read, not only here:

- **decision 1** → SECURITY.md, plus the launcher's refusal message pointing at it.
- **decision 2** → OPERATIONS.md, plus `athena-doctor`'s output on non-Linux.
- **decision 3** → `docs/DOCKER.md`, plus a job in `publish.yml` if the answer is
  to publish.

Then mark the item **DONE** in the guide with the reasoning, the way the landed
items are — a decision that only exists in a chat is one the next reader has to
make again.
