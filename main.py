"""
vLLM proxy with iOS background handoff.

Two modes, selected per-request by the presence of `device_token`:

  1. Foreground (no device_token): pure 1:1 SSE pass-through to the iOS app.
     If the app backgrounds mid-stream (explicit handoff signal, or a TCP
     disconnect), the proxy keeps draining vLLM, collects the result, and
     fires a push notification when the generation is complete.

  2. Background (device_token present): the proxy consumes the entire stream
     itself, assembles a standard OpenAI chat.completion object, stores it,
     sends a push notification, and returns the final JSON. The app never
     sees the stream.

Everything the client sends (model, sampling params, tools, ...) is forwarded
to vLLM verbatim; only the proxy's own fields are stripped. Nothing about the
model is hardcoded here.

Proxy-only request fields (stripped before forwarding):
  - device_token     : hex APNs token (or any identifier your push relay
                       understands). Its PRESENCE triggers collection mode.
  - conversation_id  : optional; used to correlate the push notification and
                       to store/retrieve the completed result.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vllm-proxy")

# --------------------------------------------------------------------------
# Configuration (all via environment; nothing model-specific is hardcoded)
# --------------------------------------------------------------------------
VLLM_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
CHAT_COMPLETIONS_URL = f"{VLLM_BASE_URL}/chat/completions"

# A small relay service you own that holds the APNs signing key and talks to
# Apple. The proxy POSTs {device_token, title, body, data} to this URL.
PUSH_RELAY_URL = os.environ.get("PUSH_RELAY_URL", "")
PUSH_RELAY_API_KEY = os.environ.get("PUSH_RELAY_API_KEY", "")

# How long to keep completed results around for the app to re-fetch.
RESULT_TTL_SECONDS = int(os.environ.get("RESULT_TTL_SECONDS", 3600))
PUSH_BODY_PREVIEW_CHARS = 200

app = FastAPI(title="vLLM iOS Background Handoff Proxy")

# --------------------------------------------------------------------------
# In-memory state
# --------------------------------------------------------------------------
# conversation_id -> stored result: {"response": <openai completion json>, "completed_at": ts}
completed_results: Dict[str, Dict[str, Any]] = {}

# conversation_id -> device_token registered by the app when it backgrounds.
# Lets a foreground stream that gets cut over find a token to push to.
registered_tokens: Dict[str, str] = {}

# conversation_ids whose stream has been handed off to the background collector.
backgrounded: set = set()


def _prune_results() -> None:
    now = time.time()
    stale = [k for k, v in completed_results.items() if now - v["completed_at"] > RESULT_TTL_SECONDS]
    for k in stale:
        completed_results.pop(k, None)


# --------------------------------------------------------------------------
# Push notification
# --------------------------------------------------------------------------
async def send_push(device_token: str, title: str, body: str, data: Dict[str, Any]) -> None:
    """
    Deliver a notification via your push relay (which owns the APNs key /
    Firebase credentials). If no relay is configured we just log, so the
    proxy is usable before push is wired up.
    """
    if not device_token:
        logger.info("No device token; skipping push. data=%s", data)
        return
    if not PUSH_RELAY_URL:
        logger.info("[PUSH stub] to=%s title=%r body=%r data=%s", device_token, title, body, data)
        return

    payload = {"device_token": device_token, "title": title, "body": body, "data": data}
    headers = {"Content-Type": "application/json"}
    if PUSH_RELAY_API_KEY:
        headers["Authorization"] = f"Bearer {PUSH_RELAY_API_KEY}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(PUSH_RELAY_URL, json=payload, headers=headers)
            logger.info("Push relay responded %s for token %s...", r.status_code, device_token[:12])
    except Exception as e:  # never let push failure kill the request
        logger.error("Push delivery failed: %s", e)


# --------------------------------------------------------------------------
# SSE parsing / completion assembly
# --------------------------------------------------------------------------
def _sse_data(line: str) -> Optional[str]:
    """Extract the JSON payload from an SSE 'data:' line, or None to skip."""
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    return data


class SSEAccumulator:
    """
    Line-buffers RAW SSE bytes and parses 'data:' lines into chunk dicts.

    Used by both the pass-through (to shadow what it forwards) and the
    background collector, so a handoff that happens mid-line continues
    parsing from exactly the right place. Both paths must consume the
    response with aiter_bytes() — httpx forbids mixing iterator styles.
    """

    def __init__(self) -> None:
        self._buf = b""
        self.chunks: list = []

    def _consume_line(self, raw: bytes) -> None:
        data = _sse_data(raw.decode(errors="replace").strip())
        if data is None:
            return
        try:
            self.chunks.append(json.loads(data))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed SSE chunk")

    def feed(self, data: bytes) -> None:
        self._buf += data
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                return
            line, self._buf = self._buf[:idx], self._buf[idx + 1:]
            self._consume_line(line)

    def flush(self) -> None:
        """Parse a trailing line that never got its newline (stream just ended)."""
        if self._buf:
            self._consume_line(self._buf)
            self._buf = b""


def assemble_completion(chunks: list) -> Dict[str, Any]:
    """
    Rebuild a standard chat.completion object from collected SSE chunks,
    mirroring whatever metadata vLLM sent (id, model, created, usage, ...).
    """
    template = chunks[0] if chunks else {}
    content_parts: list = []
    reasoning_parts: list = []
    finish_reason = None
    role = "assistant"

    for chunk in chunks:
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if isinstance(delta.get("content"), str):
                content_parts.append(delta["content"])
            if isinstance(delta.get("reasoning_content"), str):
                reasoning_parts.append(delta["reasoning_content"])
            if delta.get("role"):
                role = delta["role"]
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    message: Dict[str, Any] = {"role": role, "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    completion = {
        "id": template.get("id", f"proxy-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": template.get("created", int(time.time())),
        "model": template.get("model", "unknown"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
    }
    # vLLM only sends usage on the final chunk when stream_options.include_usage is set
    for chunk in reversed(chunks):
        if chunk.get("usage"):
            completion["usage"] = chunk["usage"]
            break
    return completion


# --------------------------------------------------------------------------
# Background collector: drains the vLLM stream and pushes when done
# --------------------------------------------------------------------------
async def collect_and_notify(
    client: httpx.AsyncClient,
    vllm_response: httpx.Response,
    accum: SSEAccumulator,
    *,
    conversation_id: str,
    device_token: Optional[str],
) -> Dict[str, Any]:
    """Consume the rest of the upstream SSE stream, store the result, push.

    `accum` may already hold chunks parsed by the foreground pass-through
    before the handoff; we continue feeding it from where it left off.
    """
    try:
        async for data in vllm_response.aiter_bytes():
            accum.feed(data)
        accum.flush()
    except httpx.HTTPError as e:
        logger.error("Upstream stream error while collecting %s: %s", conversation_id, e)
    finally:
        await vllm_response.aclose()
        await client.aclose()
        backgrounded.discard(conversation_id)

    completion = assemble_completion(accum.chunks)
    full_text = completion["choices"][0]["message"]["content"]

    _prune_results()
    completed_results[conversation_id] = {"response": completion, "completed_at": time.time()}
    logger.info("Collected %d chunks (%d chars) for conversation %s",
                len(accum.chunks), len(full_text), conversation_id)

    await send_push(
        device_token or registered_tokens.get(conversation_id, ""),
        title="Reply ready",
        body=full_text[:PUSH_BODY_PREVIEW_CHARS] + ("…" if len(full_text) > PUSH_BODY_PREVIEW_CHARS else ""),
        data={"conversation_id": conversation_id, "result_id": completion.get("id")},
    )
    return completion


# --------------------------------------------------------------------------
# Main endpoint: 1:1 pass-through, or collect mode when device_token is set
# --------------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body: Dict[str, Any] = await request.json()

    # Strip proxy-only fields; forward EVERYTHING else to vLLM untouched
    device_token: Optional[str] = body.pop("device_token", None) or None
    conversation_id: str = body.pop("conversation_id", None) or uuid.uuid4().hex
    upstream_body = dict(body)
    upstream_body["stream"] = True  # we always stream from vLLM internally

    # Pass through auth so the proxy stays invisible to any upstream auth
    forward_headers = {"Content-Type": "application/json"}
    if auth := request.headers.get("authorization"):
        forward_headers["Authorization"] = auth

    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        req = client.build_request("POST", CHAT_COMPLETIONS_URL, json=upstream_body, headers=forward_headers)
        vllm_response = await client.send(req, stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        logger.error("Failed to connect to vLLM at %s: %s", CHAT_COMPLETIONS_URL, e)
        raise HTTPException(status_code=502, detail="Upstream vLLM server unavailable")

    if vllm_response.status_code != 200:
        detail = (await vllm_response.aread()).decode(errors="replace")
        await vllm_response.aclose()
        await client.aclose()
        return JSONResponse(status_code=vllm_response.status_code,
                            content={"error": {"message": detail, "type": "upstream_error"}})

    # ---- Mode 2: device_token present -> collect everything, push when done
    if device_token:
        completion = await collect_and_notify(
            client, vllm_response, SSEAccumulator(),
            conversation_id=conversation_id,
            device_token=device_token,
        )
        return JSONResponse(content=completion)

    # ---- Mode 1: pure SSE pass-through to the foreground app.
    # Bytes are forwarded VERBATIM (never re-framed: SSE blank lines are the
    # event delimiters, and eating them breaks every standards-compliant
    # client with "stream ended in the middle of an event").
    async def passthrough():
        handed_off = False
        accum = SSEAccumulator()
        try:
            async for data in vllm_response.aiter_bytes():
                accum.feed(data)  # shadow-parse for a possible handoff
                if await request.is_disconnected() or conversation_id in backgrounded:
                    # App backgrounded: keep draining vLLM server-side, push later.
                    # These bytes go to the collector instead of the dead client.
                    handed_off = True
                    logger.info("Handing off %s to background collector", conversation_id)
                    asyncio.create_task(collect_and_notify(
                        client, vllm_response, accum,
                        conversation_id=conversation_id,
                        device_token=registered_tokens.get(conversation_id),
                    ))
                    return
                yield data
        except asyncio.CancelledError:
            # Hard TCP disconnect (app killed / suspended without signaling)
            if not handed_off and conversation_id not in backgrounded:
                handed_off = True
                logger.info("Client disconnected mid-stream on %s; collecting server-side", conversation_id)
                asyncio.create_task(collect_and_notify(
                    client, vllm_response, accum,
                    conversation_id=conversation_id,
                    device_token=registered_tokens.get(conversation_id),
                ))
            raise
        finally:
            if not handed_off:
                await vllm_response.aclose()
                await client.aclose()

    return StreamingResponse(
        passthrough(),
        media_type="text/event-stream",
        headers={"X-Conversation-Id": conversation_id, "Cache-Control": "no-cache"},
    )


# --------------------------------------------------------------------------
# Supporting endpoints
# --------------------------------------------------------------------------
@app.post("/api/chat/background")
async def signal_background(payload: Dict[str, Any]):
    """
    Called by the iOS app in applicationDidEnterBackground (before the OS
    suspends the socket). Registers the device token and flips the matching
    foreground stream into collection mode. This avoids waiting for TCP
    disconnect detection, which can lag by many seconds on iOS.
    """
    conversation_id = payload.get("conversation_id")
    if not conversation_id:
        raise HTTPException(status_code=400, detail="Missing conversation_id")
    if device_token := payload.get("device_token"):
        registered_tokens[conversation_id] = device_token
    logger.info("Background handoff signal for %s (token=%s)",
                conversation_id, bool(payload.get("device_token")))
    backgrounded.add(conversation_id)
    return {"status": "handoff_acknowledged"}


@app.get("/api/chat/result/{conversation_id}")
async def get_result(conversation_id: str):
    """Poll for a completed background response (e.g. app relaunched via push tap)."""
    _prune_results()
    entry = completed_results.get(conversation_id)
    if not entry:
        raise HTTPException(status_code=404, detail="No completed result for this conversation")
    return entry["response"]


@app.get("/healthz")
async def healthz():
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{VLLM_BASE_URL}/models")
            return {"status": "ok", "upstream": r.status_code}
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"status": "upstream_unreachable", "error": str(e)})
