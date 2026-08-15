#!/usr/bin/env bash
# Wait until a container's declared HEALTHCHECK reports healthy.
#
# Kept out of the workflow YAML on purpose: a multi-line shell snippet embedded in a
# block scalar is one stray dedent away from silently becoming a YAML mapping, and
# this one is used twice. A script is also runnable by hand when a container will
# not come up and you want the same verdict CI would reach.
set -euo pipefail

container="${1:?usage: wait_for_container_health.sh <container> [timeout-seconds]}"
timeout="${2:-120}"

for _ in $(seq 1 "$timeout"); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)"
    case "$status" in
        healthy)
            echo "$container: healthy"
            exit 0
            ;;
        unhealthy)
            echo "$container: unhealthy" >&2
            docker logs "$container" >&2 || true
            exit 1
            ;;
        missing)
            echo "$container: no such container, or it declares no healthcheck" >&2
            exit 1
            ;;
    esac
    sleep 1
done

echo "$container: never left '$status' within ${timeout}s" >&2
docker logs "$container" >&2 || true
exit 1
