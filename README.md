<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index. For a decorated native dropdown, a matching visible surface is offered as `CLICK`; its menu choices are observed after opening.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. In Brave, open `brave://inspect/#remote-debugging` and enable **Allow remote debugging for this browser instance**. In Chrome, use `chrome://inspect/#remote-debugging`. Rerun the command and allow the connection prompt. This grants Browser Harness access to that browser profile. If startup reports `DevToolsActivePort not found`, this setting is missing.

The example configuration uses [OpenAI GPT-5.6 Luna](https://developers.openai.com/api/docs/models/gpt-5.6-luna) for field text. Set these values in `.env` and restart `uv run jev` after changes:

```dotenv
TEXT_MODEL_API_KEY=
TEXT_MODEL_BASE_URL=https://api.openai.com/v1
TEXT_MODEL=gpt-5.6-luna
TEXT_MODEL_REASONING=none
```

Fill `TEXT_MODEL_API_KEY` with your OpenAI API key. `TYPESAFE_API_KEY` is still required for operation and target choices. Luna only generates field text. `TEXT_MODEL_REASONING` is sent as OpenAI's `reasoning_effort`; Luna supports `none`, `low`, `medium`, `high`, `xhigh`, and `max`. Start with `none` for short field values. The 1,024-token completion budget includes reasoning tokens, so higher effort can exhaust it before producing field text.

Requests to `api.openai.com` use `max_completion_tokens` and `reasoning_effort`. Other endpoints retain the existing request format. To use the original OpenRouter configuration, set `TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1`, `TEXT_MODEL=inception/mercury-2.5`, and `TEXT_MODEL_REASONING=none`, with an OpenRouter key. The recorded demo and performance measurements below used that original configuration, not Luna.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Offer executable controls.** Observation and execution share visibility, disabled-state and hit-test checks. A decorated dropdown surface is offered only when its local position, interaction attributes, label and current value match the backing native control; unrelated overlays remain blocked.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. After opening a decorated dropdown, wait for newly visible options, capped at 600 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Recover before dropdown input.** A rejected target or option triggers observation and a new decision; five consecutive stale cycles stop with the last reason. If a dropdown mutation starts or its response is lost, stop without retrying. Diagnostics include the target/option labels, reason and mutation phase, without raw option values or page contents.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [actionability.js](jev_ultrafast/actionability.js) | Shared target checks and decorated dropdown recognition |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Ordinary links with `target="_blank"` open in the agent-owned tab. Shadow roots, frames, canvas, uploads, JavaScript popups, nested scrolling, and arbitrary keyboard widgets remain outside this MVP. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
node --check jev_ultrafast/actionability.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. `uv run python scripts/check_dropdown.py` exercises dropdown races and prevents duplicate execution after uncertain results. `uv run python scripts/check_surface.py` checks decorated dropdowns and delayed menus. See [dropdown recovery evidence](docs/dropdown-recovery.md) and [surface-click implementation results](docs/dropdown-surface.md). Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)

## Voice extension (Brave / Chromium)

The voice extension runs the goal-based loop on the tab where recording started.
Use push-to-talk for a single instruction, or continuous mode to add and correct
instructions while the browser works. Both support same-tab navigation and leave
the tab open when stopped.

```bash
uv sync
uv run jev-voice
```

Run from this repository so the server can load `.env`. Push-to-talk transcription uses
`gpt-transcribe`; continuous mode uses `gpt-live-transcribe`. Set `OPENAI_API_KEY`, or reuse `TEXT_MODEL_API_KEY` when
`TEXT_MODEL_BASE_URL` points to `https://api.openai.com/v1`. The existing
`TYPESAFE_API_KEY` and text-model settings are still needed. API keys stay in Python.
The server listens only on `127.0.0.1:8767`; it accepts extension origins and requires
a separate random pairing token, generated in the ignored `.voice-token` file.
Client and server prove possession using nonce-bound HMAC challenges; the token
is never sent over the socket, and audio/RPC forwarding waits for authentication.
The loopback transport assumes a trusted local OS; it is not intended for remote hosting.

1. Open `brave://extensions`, enable Developer mode, and **Load unpacked** → select
   this repository's `extension` directory.
2. Click the **Jev Voice toolbar icon** to open its settings, paste the contents of `.voice-token` into **ペアリングトークン**,
   and click **保存**. This is the pairing token, not an OpenAI API key.
3. Select a microphone if needed, click **マイクを許可・確認**, and speak during the
   five-second input-level check. Click **保存** after changing the microphone, then
   return to a normal web page.

| Operation | Trigger |
| --- | --- |
| Toggle continuous listening | **⌘ + Shift + J** while the web page has focus |
| Hold to speak, release to execute | **⌘ + Shift + I** while the web page has focus |
| Stop a running task | **Esc** in the web page |
| Open pairing and microphone settings | Click the Jev Voice extension icon |
| Cancel on tab switch | Automatic; the run never follows focus to a different tab |

The confirmed transcript appears beneath the status as **指示：…**, including for
15 seconds after stopping. Settings also shows **直近の指示** for the current browser
session. Starting another session clears the on-page transcript until the next
instruction is recognized. Continuous mode also shows the current goal and partial
transcript, and restores the display after same-tab navigation.

Wait for **録音中** before speaking; microphone startup is not instantaneous. The
hold shortcut is implemented by a content script, so it doesn't work in the address
bar, browser-internal pages, or cross-origin frames. The shortcuts are page key listeners rather than browser commands. In push-to-talk,
page blur or navigation cancels recording; navigation after committing is allowed.
Push-to-talk recording is limited to 30 seconds and a run to 120 seconds.
Continuous mode keeps recording through same-tab navigation, with a 10-minute
session limit. Both retain the 60-action / 120-decision budget for the whole session;
updating a goal does not reset it. Cancellation prevents subsequent commands; it cannot undo an
already-dispatched click or submission.

Audio is sent incrementally over an authenticated local WebSocket, then upstream
to OpenAI. In push-to-talk, releasing the key flushes the final audio chunk and commits the turn;
`gpt-transcribe` starts transcription after commit. The confirmed transcript becomes
the original goal with no extra planning model. The existing TypeSafe operation and
operation-specific target selection, TYPE_TEXT helper, stale guards, and execution
history remain in use. The extension uses `chrome.debugger` for browser input, so
Chromium displays a debugging notice while attached. No remote-debugging browser
flag or Browser Harness connection is needed for the extension.

Visible page information goes to TypeSafe and the configured text model (for
field generation and, in continuous mode, intent interpretation). Continuous
transcription also receives up to 40 observed control labels as vocabulary hints;
input values and page-body text are not included in those hints. Labels themselves
can contain personal information. The extension doesn't receive provider credentials. It stores only its local
pairing token and selected microphone, plus the latest status and transcript in
browser-session memory; the voice bridge doesn't save recordings or
traces. Failed runs log only the stage, error code, audio duration/peak level, and
transcript length and call/discard counts; they do not log audio or transcript contents.
Push-to-talk completion remains a model decision and asks the user to check the outcome.

Scope: ordinary HTML / ARIA controls and same-tab HTTP(S) navigation, including
cross-origin navigation. Iframes, shadow DOM, canvas, file uploads, nested scrolling,
and continuing in a newly opened tab are outside this MVP. Borrowed tabs keep their
viewport and link targets unchanged. If a link activates a new tab, the original run
stops rather than moving to it. Opening DevTools or revoking debugger access can
interrupt the run; uncertain input is never automatically repeated.

### Continuous goals

Press **⌘ + Shift + J**, wait for **連続録音中**, and speak normally. Pauses of about
650 ms let the local PCM energy/silence detector commit a turn. Confirmed utterances are interpreted by
`INTENT_MODEL` (defaults to `TEXT_MODEL`, e.g. Luna) with a structured goal patch.
The existing `TEXT_MODEL_API_KEY` and base URL are reused. Each nonempty finalized
utterance adds one intent-model request; browser decisions and field-text calls
remain separate. The historical flight timings above do not cover this mode.

- Add a condition or correct one: retain other conditions, invalidate old decisions
  and generated field text, then reobserve and work toward the latest goal.
- Ask for a different task: replace the active goal and discard its queued follow-ups.
  An explicit “after that” queues a separate goal until the current one is verified.
- Say stop/pause: interpretation pauses execution while the microphone stays open.
  Say resume or provide a revised goal to continue. **Esc**, another **⌘ + Shift + J**,
  tab switch or window-focus loss closes the entire session.
- Local speech detection immediately suspends new commands while interpretation is pending.
  Already-dispatched input cannot be undone. An interruption partway through a multi-command
  browser action ends the session without replaying the action; start a new session afterward.
- `DONE` triggers a fresh observation and code checks of the goal's expected URL,
  visible result text, or uniquely labelled control values/checked state. All purpose
  and condition checks must pass. Missing, ambiguous or failed checks produce
  **確認待ち**, not success; queued work stays pending. Checks are proposed by the
  intent model, so their semantic coverage is not a general correctness guarantee.
- Verified completion stops browser work but retains the goal and microphone for
  another instruction. It does not continuously enforce conditions after completion.

Partial transcripts are displayed but **never authorize browser mutations**. This
implements the plan's finalized-utterance stage. Executing clauses before an utterance
finishes remains a separate improvement requiring negation/correction evaluation.
See [continuous voice design and evidence](docs/continuous-voice.md).

### Voice verification

```bash
uv run pytest
uv run python scripts/check_voice_extension.py
```

The latter launches **isolated headless Brave** with the real unpacked extension,
a fake microphone, a local fixture, and offline transcription/model substitutes.
It checks actual page navigation, text input, application of the value, the end
notification, preservation of the tab, early key release, Esc cancellation, and
cancellation during model latency when switching tabs. It also checks continuous
recording through navigation, retained goal display, post-completion corrections,
old-decision rejection during model latency, and spoken pause. It doesn't touch a personal browser
profile or call paid APIs. Real-microphone Japanese recognition, live provider
availability, and end-to-end latency must be evaluated separately; existing flight
demo timing does not include voice input.
