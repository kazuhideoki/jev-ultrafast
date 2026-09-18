# Voice extension review — 2026-09-19

Scope: the voice MVP introduced in `8221097`, plus the fixes recorded here.
Three read-only reviewers received the source/diff and project rules without the
parent conversation. Their questions covered asynchronous lifecycle, trust
boundaries, and audio/provider/installation evidence. The parent independently
reproduced the actionable findings and made the final decisions.

## Independent findings and disposition

- **L1 / B1: cancellation during the last active-tab query could still dispatch
  new input.** Both reviewers identified the same issue; this is one finding,
  not two independent votes for correctness. A Node VM reproduction dispatched
  `Input.insertText` after cancellation. The service worker now checks run identity
  and the stopping flag *after* the awaited query, immediately before dispatch.
  The regression test failed before the fix and passes afterward.
- **B2: a substitute listener on the local port could receive the token and send
  unsolicited browser RPC.** This requires a local process binding the port while
  the intended bridge is absent; no remote-page attack was established. Mutual,
  nonce-bound HMAC authentication now proves possession of the pairing token before
  audio/RPC forwarding. The token itself is not transmitted. Offline tests reject
  unsolicited RPC and invalid server proofs without releasing queued audio.
- **E1: the offline browser smoke does not establish speech-recognition quality.**
  It substitutes the transcription and decision models. This is an evidence limit,
  not a demonstrated provider bug. The limitation remains documented. A separate
  earlier manual provider diagnostic recognized a 1.8-second synthetic Japanese
  phrase, but does not prove real-microphone end-to-end reliability or latency.

## Additional parent finding

The manifest allows Chrome 116+, while `chrome.offscreen.hasDocument()` requires
Chrome 150 according to the [official API reference](https://developer.chrome.com/docs/extensions/reference/api/offscreen).
The extension now uses `runtime.getContexts()`, available since Chrome 116.
A regression mock without `hasDocument` failed before and passes after the fix.

## Validation and remaining scope

- `uv run ruff check .`, `uv run pytest`, JavaScript syntax checks, and `uv build`.
- The pytest suite includes Node event-loop tests for cancellation, compatibility,
  and unauthenticated local-server messages; tests do not call paid APIs.
- Isolated Brave smoke uses the actual extension and mutual authentication. It
  independently checks navigation, applied field value, visible transcript,
  retained user tab, short recordings, Esc, and tab switching during model latency.
- No claim of independently verified arbitrary goal completion or faster-than-mouse
  latency is made. New-tab continuation, frames, and shadow DOM remain outside MVP.
- The local OS and processes with access to the pairing file remain trusted. This
  is a loopback service, not a remote multi-user execution gateway.
