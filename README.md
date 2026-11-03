# roxy-vllm

A robust proxy that sits between mobile clients and a [vLLM](https://github.com/vllm-project/vllm) server. Mobile OSes don't allow apps to keep streaming when backgrounded (iOS suspends the process; Android aggressively restricts background networking). This proxy handles the handoff: while the app is in the foreground it's a 1:1 SSE token stream, and the moment the app disappears it silently switches to server-side collection, then delivers the finished reply via push notification — all without interrupting vLLM.

Nothing about the model is hardcoded. Every request field (`model`, sampling params, tools, etc.) is forwarded to vLLM verbatim; the client chooses the model.

## How it works

The proxy always streams from vLLM internally. The client controls what happens to that stream with one optional field:

| Mode | Trigger | Behavior |
|---|---|---|
| **Foreground** | no `device_token` | Pure 1:1 SSE pass-through. If the app backgrounds mid-stream (explicit signal or TCP disconnect), the proxy keeps draining vLLM, collects the result, and pushes when done. |
| **Collection** | `device_token` present | The proxy consumes the entire stream itself, assembles a standard OpenAI `chat.completion` JSON object, stores it, sends a push notification, and returns the final JSON. The app never sees the stream. |

Proxy-only request fields (stripped before forwarding to vLLM):

- `device_token` — APNs token (or any identifier your push relay understands). **Its presence is what triggers collection mode.**
- `conversation_id` — optional; correlates the push with the app's conversation and keys the stored result.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/chat/completions` | Main endpoint. OpenAI-compatible; drop-in for the vLLM URL. |
| `POST` | `/api/chat/background` | Called by the app when it enters background: `{"conversation_id": "...", "device_token": "..."}`. Flips the matching foreground stream to collection immediately (TCP disconnect detection on iOS can lag many seconds after suspension). |
| `GET` | `/api/chat/result/{conversation_id}` | Poll/re-fetch a completed background response (e.g. after a push tap, since APNs payloads cap at 4 KB). Stored results expire after `RESULT_TTL_SECONDS` (default 1 h). |
| `GET` | `/healthz` | Checks upstream vLLM reachability. |

## Configuration

All via environment variables — nothing model-specific is compiled in:

| Variable | Default | Purpose |
|---|---|---|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | Upstream vLLM base (OpenAI-compatible API). |
| `PUSH_RELAY_URL` | *(empty)* | Your small relay service that holds the APNs signing key and talks to Apple. The proxy POSTs `{device_token, title, body, data}` to it. Unset = pushes are logged, not sent (so the flow is testable before push is wired up). |
| `PUSH_RELAY_API_KEY` | *(empty)* | Sent as `Authorization: Bearer …` to the relay. |
| `RESULT_TTL_SECONDS` | `3600` | How long completed results stay fetchable. |

## Running

### In Docker (recommended)

```bash
./start.sh    # build image + run container on host port 7777
./stop.sh     # stop and remove the container
```

Defaults: proxy on host port **7777**, upstream vLLM expected on the **host at port 8888** (reached via `host.docker.internal`, which works on both Docker Desktop and Linux thanks to `host-gateway`). Override anything with env vars:

```bash
PROXY_PORT=9000 VLLM_PORT=8000 ./start.sh
PUSH_RELAY_URL=https://push.yourserver.com PUSH_RELAY_API_KEY=*** ./start.sh
```

`start.sh` rebuilds the image each time, replaces any existing container (safe to re-run), sets `--restart unless-stopped`, and finishes with a `/healthz` check that tells you whether the proxy is up and whether vLLM is reachable.

### Bare metal (no Docker)

```bash
pip install -r requirements.txt
VLLM_BASE_URL=http://localhost:8888/v1 uvicorn main:app --host 0.0.0.0 --port 7777
```

Quick smoke test:

```bash
# Foreground (streaming) mode
curl -N localhost:7777/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hi"}]}'

# Collection mode (device_token present -> single JSON response + push)
curl localhost:7777/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"hi"}],
       "device_token":"abc123","conversation_id":"conv-1"}'

curl localhost:7777/api/chat/result/conv-1
```

## iOS client integration

The app needs four pieces:

**1. Point the API client at the proxy, not vLLM.** Base URL becomes `https://<proxy>/v1`. If you keep using an OpenAI-style SDK, `device_token`/`conversation_id` go in as extra body fields — the proxy strips them before forwarding.

**2. Register for pushes and keep the token.** `UNUserNotificationCenter.requestAuthorization` + `UIApplication.registerForRemoteNotifications()`, relay `didRegisterForRemoteNotificationsWithDeviceToken` to your backend. The proxy never talks to Apple directly — it calls your push relay, which holds the APNs key (never ship that key in an app or a public proxy).

**3. Decide the mode per request:**

- **Foreground send:** omit `device_token`. Stream normally. Also generate and remember a `conversation_id` locally so the background signal (below) can reference it.
- **Background send:** when you *know* the app won't stay foreground to watch a stream (user hit send just before switching apps, or a long job you're intentionally detaching), include `device_token` in the request body. The call will hang until generation completes and return the final JSON — fire it as a `URLSession` background-task request so it can finish after suspension — and a push arrives when it's done.

**4. Signal backgrounding in `sceneDidEnterBackground` (or `applicationDidEnterBackground`).** iOS suspends the process within seconds, and the proxy's TCP-level disconnect detection can lag badly. So:

```swift
func sceneDidEnterBackground(_ scene: UIScene) {
    guard let conv = activeConversationId, !streamFinished else { return }
    // Best-effort, fired with a short-lived beginBackgroundTask so it
    // actually leaves the device before suspension:
    notifyProxyBackgrounded(conversationId: conv, deviceToken: apnsToken)
}
```

The proxy sees the flag, cuts the stream to zero, and drains vLLM itself. On the app side, when the push arrives, use its `data.conversation_id` to `GET /api/chat/result/{conversation_id}` and render the full reply (the notification body itself is only a ~200-char preview).

Edge case: if the app is **killed** rather than backgrounded, the background signal may never fire — but the proxy still catches the hard `CancelledError` on the socket and collects anyway. The only thing missing is a push target, so make sure the app has sent its `device_token` at least once (e.g. on login) and the proxy's `registered_tokens` map can fall back to it.

## Android

**Short answer: yes, we still need the proxy — but the client-side story is different.**

Android doesn't freeze background processes the way iOS does. A backgrounded app keeps running at "cached" priority and its SSE socket generally stays open while the device is awake. The things that actually kill the stream are:

- **Doze mode** (unplugged + stationary + screen off): suspends *all* network access, ignores wakelocks, drops live sockets.
- **App Standby / Standby Buckets**: defers background networking for recently-unopened apps, and throttles push delivery for inactive ones.
- **OEM killers** (Samsung, Xiaomi, Oppo, Vivo — see [dontkillmyapp.com](https://dontkillmyapp.com)): aggressively kill cached processes and their sockets regardless of Doze. A socket you assumed was alive often isn't.
- **WorkManager is not an option** for a live token stream — it runs on JobScheduler, which is explicitly deferred during Doze.

So the architecture stays the same with one client-side enhancement:

1. **Server-side collection remains the mandatory floor.** Because Doze and OEM killers sever sockets unpredictably, the proxy must always be able to collect the full response and notify — same handoff flow as iOS. All the disconnect-detection and result-fetch work is shared between platforms.
2. **On top of that, use a foreground service (FGS)** with an ongoing notification while the user awaits a stream. The app counts as foreground-like: exempt from App Standby, network preserved, true token-by-token streaming UX for most sessions. Caveats: it must be started while the app is visible (fine — the user starts the stream in-app); Android 15 caps `dataSync`-type FGS at 6 h/day; and the ongoing notification is user-visible.
3. **If the FGS socket dies anyway** (Doze kicks in, OEM killer, etc.), fall back to the iOS flow: the proxy detects the disconnect, collects, and sends a **high-priority FCM `remote_notification`** ("reply ready") — normal-priority/batched FCM messages get deferred by Doze and bucket throttling, so priority matters. The app fetches the full reply from `GET /api/chat/result/{conversation_id}` on tap. This matches what production chat apps do: FGS for user-visible ongoing work, push for completed/background results.

### FCM vs APNs for the push relay

The proxy's `PUSH_RELAY_URL` contract (`{device_token, title, body, data}`) doesn't change — the relay just grows a second backend:

| | APNs (iOS) | FCM (Android) |
|---|---|---|
| Endpoint | Apple HTTP/2 API | `POST https://fcm.googleapis.com/v1/projects/{project}/messages:send` |
| Auth | Per-app `.p8` signing key, JWT-minted per request | One Google service-account credential (OAuth2 token with `firebase.messaging` scope) — easiest via the `firebase-admin` Python SDK |
| Payload cap | 4 KB | 4 KB (same limit — send a fetch-back id, not the whole LLM reply) |
| Delivery | Near-immediate | Best-effort; `priority: high` + `remote_notification` bypasses Doze, everything else gets batched/delayed |

Practical notes for the Android client:

- Send the **FCM registration token** in the same `device_token` field — the relay distinguishes platform (or the app can add a `platform` field; the proxy forwards it through to the relay untouched).
- The background signal endpoint (`POST /api/chat/background`) is still worth calling from `onStop()`, but it's less critical than on iOS since there's no hard suspension deadline.
- Treat push as "wake + fetch from the proxy", not "payload in the notification" — FCM gives no delivery guarantee comparable to a live socket.

