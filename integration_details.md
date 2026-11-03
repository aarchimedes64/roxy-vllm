Here's the full integration guide for the iOS app, in order of what to build.

---

1. Networking layer: point at the proxy

Replace the vLLM base URL with the proxy's, e.g. https://your-proxy.example.com/v1/chat/completions. The request body is exactly what you'd send to vLLM — plus two optional fields the proxy strips:

struct ChatRequest: Encodable {
    let model: String
    let messages: [ChatMessage]
    // ...temperature, max_tokens, etc. pass through untouched
    let device_token: String?       // presence = collection mode
    let conversation_id: String?    // correlates push + stored result
}

Two decisions per request:

- Normal chat send (user watching the screen): omit device_token, generate a conversation_id locally (UUID), store it as the "active stream", and consume SSE as usual.
- Fire-and-forget send: include device_token. The connection will hang open until generation finishes and return one JSON chat.completion. Don't use a normal URLSession call for this — use a background session configuration so it survives suspension (details in §4).

For SSE parsing in foreground mode, if you're not already streaming: URLSession.shared.bytes(for:), then for try await line in bytes.lines, filter line.hasPrefix("data:"), JSON-decode the payload, pull choices[0].delta.content. Watch for data: [DONE].

2. APNs registration

func application(_ app: UIApplication,
                 didFinishLaunching options: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
    UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    UIApplication.shared.registerForRemoteNotifications()
    return true
}

func application(_ application: UIApplication,
                 didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    let token = deviceToken.map { String(format: "%02x", $0) }.joined()
    // Upload to YOUR backend (the push relay's datastore),
    // keyed to the user, with an "ios" platform tag.
    Task { await backend.uploadPushToken(token, platform: "ios") }
}

Three gotchas here:

- Re-upload the token every launch. APNs tokens rotate (OS updates, reinstalls, network changes). A stale token is the #1 silent failure in push setups.
- The token is hex — the proxy treats device_token as an opaque string, so make sure you hex-encode; don't send raw Data.
- The proxy never talks to Apple. Your device_token flows: chat request → proxy → push relay → APNs. The relay is where your .p8 key lives, so never put APNs credentials in the app or the proxy.

3. Track in-flight streams

Add a tiny piece of state to whatever owns chat sends:

@Observable final class StreamCoordinator {
    var activeConversationId: String?   // set when a foreground send starts
    var streamFinished = false          // set on [DONE] / error
}

You need this because the background signal (§4) must reference the conversation the proxy is currently streaming to you. Handle it as a single active stream first — that matches a chat app's real usage; you can generalize to a set later.

4. The backgrounding signal (the critical piece)

iOS suspends you within seconds of backgrounding and the proxy's TCP disconnect detection can lag badly, so you must tell the proxy explicitly. Do it in sceneDidEnterBackground with a beginBackgroundTask so the POST actually leaves the device:

func sceneDidEnterBackground(_ scene: UIScene) {
    guard let conv = coordinator.activeConversationId, !coordinator.streamFinished else { return }

    let signal = {
        try? await self.proxy.signalBackground(
            conversationId: conv,
            deviceToken: PushTokenStore.current   // cached from §2
        ) // POST /api/chat/background, 2s timeout
    }

    var bgTask: UIBackgroundTaskIdentifier = .invalid
    bgTask = UIApplication.shared.beginBackgroundTask(withName: "stream-handoff") {
        if bgTask != .invalid { UIApplication.shared.endBackgroundTask(bgTask) }
    }
    Task {
        await signal()
        // Cancel the in-flight SSE task too, so URLSession stops reading;
        // the proxy has already taken over collection.
        coordinator.cancelActiveStream()
        UIApplication.shared.endBackgroundTask(bgTask)
    }
}

Order matters: fire the signal first, then cancel your stream task. If you cancel first, you're racing the proxy's disconnect detection. And don't bother waiting for the signal's response — fire it with a short timeout and move on.

About the ~5 seconds you get: beginBackgroundTask gives you roughly 30s in practice but you should budget ~5. On a bad network the POST may not leave. Two mitigations: keep a persistent connection pool warm to the proxy (don't build a fresh TLS handshake in the background), and remember the app will be killed a lot — the proxy's disconnect detection is the safety net.

5. Collection mode sends (fire-and-forget)

When the app knows it won't stay foreground (long job, or user hits send and immediately backgrounds), include device_token and use a background URLSession:

let config = URLSessionConfiguration.background(withIdentifier: "collect-\(UUID())")
config.isDiscretionary = false
config.sessionSendsLaunchEvents = true
let task = URLSession(configuration: config).dataTask(for: proxyRequest) // device_token set

This survives suspension and calls back with the full JSON completion when generation finishes — if the app is merely backgrounded, you get the body; if it was killed, you get it on next relaunch via the completion-handler mechanism. Either way, treat the push notification as the primary signal and this response as a bonus.

6. Push receipt and result rendering

func userNotificationCenter(_ center: UNUserNotificationCenter,
                            didReceive response: UNNotificationResponse,
                            withCompletionHandler complete: () -> Void) {
    guard let conv = response.notification.request.content.userInfo["conversation_id"] as? String
    else { complete(); return; complete() }

    Task {
        // Full text is NOT in the push (4KB APNs cap; body is a 200-char preview)
        let completion = try? await proxy.fetchResult(conversationId: conv) // GET /api/chat/result/{id}
        await MainActor.run { chatStore.appendAssistantMessage(from: completion, for: conv) }
        complete()
    }
}

Also implement willPresent if you want banners while the app is foregrounded — you'll usually want to suppress them there (completionHandler([])), because if the app is foreground the stream already delivered the reply. Dedupe on your side: if streamFinished for that conversation, ignore the push's content.

7. Launch reconciliation (the missed-push path)

Push is best-effort; the app can be killed and relaunched days later with a finished reply sitting in the proxy's result store (1h TTL). On every launch:

for conv in localStore.pendingAssistantConversations {   // user sent, never got reply
    if let completion = try? await proxy.fetchResult(conversationId: conv) {
        chatStore.appendAssistantMessage(from: completion, for: conv)
    }
}

This makes the whole system self-healing: even if the push never arrived, opening the app recovers the answer.

8. Push relay (the one backend piece you must build)

A tiny service holding the APNs .p8 key + key id + team id, exposing POST {device_token, title, body, data} to match the proxy's contract. Set the proxy's PUSH_RELAY_URL to it. Until it exists, leave PUSH_RELAY_URL unset — the proxy logs pushes and everything else still works, so you can build and test steps 1–7 first.

Suggested build order

1. Foreground streaming through the proxy with conversation_id (push unset) — proves pass-through
2. Kill the app mid-stream, watch proxy logs for the handoff + stub push, then hit GET /api/chat/result/{id} — proves collection
3. APNs registration + relay, real notifications end-to-end
4. sceneDidEnterBackground signal + background session sends
5. Launch reconciliation

One last thing: if the proxy is http:// (dev/LAN), you'll need an ATS exception in Info.plist — or just put TLS in front with Caddy or a load balancer from day one.