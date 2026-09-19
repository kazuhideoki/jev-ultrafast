# Continuous voice sessions

The implementation follows the finalized-utterance phase of the referenced plan:
keep listening, interpret additions/corrections against the current goal, and let
Jev choose one next operation against that goal. Partial text is visible but cannot
cause browser input. No site-specific action plans or field strings are introduced.

## Ownership and concurrency

`offscreen.js` owns the microphone and authenticated local socket. A new `start`
message selects push-to-talk or continuous mode. Continuous mode never commits by
key release and keeps its microphone through same-tab navigation and task completion.
A local PCM energy detector commits speech turns explicitly; the upstream session uses
`turn_detection: null`. The live endpoint rejects server VAD for this model.

Within one Python session:

1. `Bridge.read` is the only local socket reader and separates audio/events from RPC replies.
2. The transcription uploader continuously drains audio and updates bounded label vocabulary
   from the browser owner's last observation. It never reads the browser itself.
3. The transcription receiver uses local turn/commit order and `item_id` to buffer
   out-of-order finals. A final item is delivered once. Local speech detection invalidates the
   current execution epoch immediately; deltas are display-only.
4. A single interpreter worker sends each final utterance, current goal, bounded recent
   utterances, last page and actual actions to the intent model. It returns a typed patch:
   new, amend, enqueue, pause, resume or clarify. The patch echoes the base revision and
   source item. `Goals` validates it and applies it atomically. Omitted conditions survive;
   conditions with matching IDs are replaced. Failed validation stops the session.
5. The main thread is the sole owner of the browser and agent. It checks the input epoch
   around decisions/text generation and before each RPC. Updating the goal clears the
   decision/text cache while retaining actual action history and session budgets.

Utterance history, current goal, queued goals, revision history and actual action history
are distinct in-memory data. Replaced work is marked superseded, not completed. Paused,
blocked, clarification, awaiting confirmation and completed states remain distinct.
All session content is discarded when the session ends; no recordings or traces are saved.

## Dispatch boundary

Epoch invalidation and RPC send are serialized with the goal lock, but the lock is not
held while waiting on a browser command. The extension checks the epoch again after
asynchronous attachment/focus checks and immediately before `chrome.debugger.sendCommand`.
Offscreen RPC forwarding does not block subsequent gate messages, allowing a correction
to invalidate a command still awaiting browser preflight.

An obsolete command rejected before dispatch may be discarded and replanned. If an input
subcommand has already been dispatched, interruption stops the session; it is never
retried. This includes click/typing sequences interrupted between their component CDP
calls. Errors and unknown replies also stop without retry. This cannot undo an input
already delivered to the browser, and is not a promise of zero latency from speech onset.
Transcription or interpretation failure also invalidates in-flight decisions.

## Completion and context

`DONE` is only a candidate. The browser owner obtains a fresh snapshot. Every requirement
(the purpose plus each condition) must have checks, and every check must pass:

- exact URL;
- distinctive visible result text;
- exact value or checked state of a unique observed control label.

Missing evidence, mismatches and ambiguous labels leave the goal awaiting confirmation.
Only verified completion advances queued work. These code checks are deterministic, but
choosing sufficient assertions is still a model interpretation: a weak or wrong assertion
can misrepresent the user's actual intent. Real-world evaluation is still needed.
Completion stops the loop, preserves context, and keeps the microphone open. No background
page monitoring reverts manual edits. New tasks clear unrelated conditions/queued goals.

## Verification performed

Offline unit tests exercise condition preservation/correction, duplicate utterances,
stale patch rejection without partial application, queue/verification boundaries,
ambiguous labels, ordered transcript finals, vocabulary filtering, cancellation during
both decision and text-model latency, and dispatch rejection before/after input.
A Node harness executes the real background script with delayed browser preflight and
confirms a newer epoch prevents `Input.insertText` from being dispatched.

`uv run python scripts/check_voice_extension.py` launches isolated headless Brave with
the real unpacked extension, fake microphone and local HTML fixture. Models/transcription
are offline substitutes. It independently reads actual DOM outcomes and verifies:

- legacy push-to-talk navigation, input, apply and retained tab;
- early release, Esc and tab switching during model latency;
- continuous microphone transport across same-tab navigation and verified completion;
- goal display restored after navigation;
- correction after completion;
- correction during model latency, with no obsolete value applied;
- spoken pause and Esc termination.

No paid API calls are made by tests. Real microphone recognition, account access to
`gpt-live-transcribe`, intent-model semantic quality, local silence segmentation and live end-to-end
latency are not established by these fixtures. Clause execution before final transcription
is intentionally not enabled in this phase. Numeric failure diagnostics include intent-call
and discarded-cycle counts; the historical flight benchmark remains unchanged.

## Provider contract

Implementation references checked against official documentation:

- [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription):
  `gpt-live-transcribe`, deltas/finals, `item_id`, 24 kHz PCM, vocabulary updates.
- [Voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad):
  server VAD, speech-start events and silence-based turn boundaries.

Continuous sessions use local PCM RMS detection with 650 ms silence, 300 ms prefix
padding and a normalized RMS threshold of 0.008. Continuous speech over 30 seconds
stops rather than authorizing an incomplete instruction. These are starting settings, not a measured optimum. The existing push-to-talk
path keeps `gpt-transcribe` and explicit commits. API credentials remain server-side.

## Live configuration correction (2026-09-20)

The live API rejected `server_vad` with `invalid_value` on
`session.audio.input.turn_detection`: turn detection is unsupported for this model.
The generic documentation did not establish support for this exact combination.
A configuration-only live probe (no audio or page contents) confirmed both initial
configuration and keyword updates succeed with `turn_detection: null`.
Local PCM segmentation now supplies explicit commits and gates execution as soon
as speech is detected. Provider errors are mapped to safe, actionable error codes
instead of being collapsed into an unexplained RuntimeError.
