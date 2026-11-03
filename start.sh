#!/usr/bin/env bash
#
# Start the roxy-vllm proxy in a Docker container.
#
#   ./start.sh
#
# Override defaults with env vars:
#   PROXY_PORT=9000 VLLM_PORT=8000 PUSH_RELAY_URL=https://push.example.com ./start.sh
#
set -euo pipefail

IMAGE=roxy-vllm
CONTAINER=roxy-vllm

PROXY_PORT="${PROXY_PORT:-7777}"        # port exposed on the host
VLLM_HOST="${VLLM_HOST:-host.docker.internal}"
VLLM_PORT="${VLLM_PORT:-8888}"          # vLLM on the host

cd "$(dirname "$0")"

echo "==> Building image ${IMAGE}"
docker build -t "$IMAGE" .

echo "==> Stopping any existing container"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

echo "==> Starting ${CONTAINER} on host port ${PROXY_PORT} -> vLLM at http://${VLLM_HOST}:${VLLM_PORT}/v1"
docker run -d \
  --name "$CONTAINER" \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p "${PROXY_PORT}:7777" \
  -e VLLM_BASE_URL="http://${VLLM_HOST}:${VLLM_PORT}/v1" \
  ${PUSH_RELAY_URL:+-e PUSH_RELAY_URL="$PUSH_RELAY_URL"} \
  ${PUSH_RELAY_API_KEY:+-e PUSH_RELAY_API_KEY="$PUSH_RELAY_API_KEY"} \
  ${RESULT_TTL_SECONDS:+-e RESULT_TTL_SECONDS="$RESULT_TTL_SECONDS"} \
  "$IMAGE"

# Give uvicorn a moment, then sanity-check the container and upstream.
sleep 2
HEALTH="$(curl -s --max-time 5 "http://localhost:${PROXY_PORT}/healthz" 2>/dev/null || true)"
case "$HEALTH" in
  *'"status":"ok"'*)
    echo "==> OK: proxy healthy on http://localhost:${PROXY_PORT}, upstream vLLM reachable" ;;
  *'"status":"upstream_unreachable"'*)
    echo "!! WARNING: proxy is up on ${PROXY_PORT} but vLLM is unreachable at http://${VLLM_HOST}:${VLLM_PORT}/v1" ;;
  *)
    echo "!! WARNING: proxy not responding. Check 'docker logs ${CONTAINER}'" ;;
esac

echo "==> Done. Logs: docker logs -f ${CONTAINER}"
