# Athena in a container — the same fail-closed launcher, nothing widened.
#
# Two stages so the runtime image carries no build tooling and no repository: the
# builder produces a wheel, the runtime installs it under the same
# constraints/ci-py312.txt graph CI pins, and the source tree never ships.
#
# What this image deliberately does NOT do:
#
#   * It does not bootstrap. `athena-serve` refuses a database that does not exist,
#     and creating the first admin is a separate, explicit act (see docs/DOCKER.md).
#     An entrypoint that quietly migrated an empty volume and opened first-admin
#     bootstrap would undo the gate the launcher exists to hold.
#   * It does not bind a routable address. ATHENA_NETWORK_MODE defaults to `local`,
#     which permits loopback ONLY — there is no `public` mode, in the container or
#     out of it. A published port therefore does not reach it; docs/DOCKER.md names
#     the shapes that do work and the ones that remain unsupported.
#   * It does not run as root.

FROM python:3.12-slim AS builder

WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY constraints ./constraints

# `--no-isolation` with an explicitly installed backend, matching how CI builds the
# release candidate. The default isolated build would silently fetch an unpinned
# setuptools into a throwaway environment — the one place in this repo where a
# dependency would arrive unpinned, inside the artifact that ships. The version is
# pyproject's own `build-system.requires` pin; `build` comes from the CI graph.
RUN python -m pip install --no-cache-dir \
        -c constraints/ci-py312.txt build setuptools==83.0.0 \
    && python -m build --no-isolation --wheel --outdir /wheels


FROM python:3.12-slim AS runtime

# A fixed uid/gid, so a bind-mounted host directory has a predictable owner to
# chown to. Named volumes take their ownership from the image and need nothing.
RUN groupadd --system --gid 10001 athena \
    && useradd --system --uid 10001 --gid athena --home-dir /var/lib/athena athena

COPY --from=builder /wheels /wheels
COPY constraints/ci-py312.txt /tmp/ci-py312.txt
# Base install only: the MCP server is an optional runtime extra (`athena-mcp`
# needs `pip install athena[mcp]`), and an image whose job is `athena-serve` is
# better off without the dependency surface it would add.
RUN python -m pip install --no-cache-dir -c /tmp/ci-py312.txt /wheels/*.whl \
    && rm -rf /wheels /tmp/ci-py312.txt

# One directory holds both durable things — the SQLite file and the attachment
# blobs — so an operator has exactly one path to back up and one volume to mount.
ENV ATHENA_DB=/var/lib/athena/athena.db \
    ATHENA_ATTACH_DIR=/var/lib/athena/attachments \
    ATHENA_NETWORK_MODE=local \
    PYTHONUNBUFFERED=1
RUN mkdir -p /var/lib/athena/attachments && chown -R athena:athena /var/lib/athena
VOLUME ["/var/lib/athena"]

USER athena
WORKDIR /var/lib/athena
EXPOSE 8000

# Asks the app over loopback from inside the container, which is the only place a
# `local`-mode bind is reachable — and is exactly what an orchestrator should be
# checking anyway: that the process is serving, not that a port is published.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["athena-serve"]
