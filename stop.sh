#!/usr/bin/env bash
#
# Stop and remove the roxy-vllm proxy container.
#
#   ./stop.sh
#
set -euo pipefail

CONTAINER=roxy-vllm

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "==> Stopping ${CONTAINER}"
  docker stop "$CONTAINER" >/dev/null
  docker rm "$CONTAINER" >/dev/null
  echo "==> Removed."
else
  echo "==> No ${CONTAINER} container found; nothing to do."
fi
