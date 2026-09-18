# Dropdown preflight recovery

Verified on 2026-09-18 with the local Brave Browser through Browser Harness.

Before changing a native dropdown, the executor validates the observed target, hit-test position and actual option node. Removed/replaced options, changed values/labels and disabled options are rejected. It changes `selectedIndex` only after validation, so duplicate option values cannot redirect selection to another option.

A structured rejection before mutation becomes `StalePage`: the decision is consumed, the page is observed again and the policy makes a new decision. Five consecutive stale cycles stop the run with the last reason. Successful execution resets this count. A stale observation after execution preserves the already-recorded action.

Once mutation has started, an exception stops the run. A missing evaluation result or transport failure also stops the run because the mutation may have happened. Neither condition replays the selection. A stopped run rejects subsequent tick/predict/act commands.

Diagnostics are emitted to the Python logger and recorded in `state.failures`. They contain target/option labels (each capped at 200 characters), a code-owned reason, phase and `mutation_started`: `false` means confirmed preflight rejection, `true` means mutation started, and `null` means unknown after an interrupted/missing response. Raw option values, exception payloads, credentials and page contents are not included in these diagnostics. `state.error` is shown in the inspector and command-line example.

## Offline and local-browser checks

- `uv run pytest`: **54 passed**, no paid API calls.
- `uv run python scripts/check_dropdown.py`: **14 passed**, real local browser with deterministic policy choices and no model calls. Tests inject DOM changes after the freshness guard, before executor validation. They measure selected value and `input`/`change` counts; verify recovery after an overlay is removed; verify the five-cycle persistent-overlay limit; and simulate an exception after `input` plus a lost response after both events. Each uncertain case performs only one selection.
- `uv run python scripts/check_guards.py`: **22 passed**, existing real-browser regressions with no model calls.
- `uv run ruff check .`, `node --check jev_ultrafast/static/app.js`, `node --check jev_ultrafast/snapshot.js`, `uv build`: **passed**.

## Live reproduction

```sh
uv run --env-file .env python examples/run.py \
  --url 'https://www.google.com/' \
  --goal '日本のアマゾンで一番安いUSB Type-Cのケーブルの3メートルのものについて調べてページを見つけて' \
  --report artifacts/dropdown-live-final-20260918.json
```

The final-code run stopped cleanly with `status=blocked`, after **10 executed actions**, **19 policy decisions**, **1 text-model call** (20 model calls total), and **10,103 ms** of agent-loop time. Timing excludes initial observation and the independent final-page read.

The last failure was:

```json
{"kind":"select","target":"並べ替え::","option":"価格: 安い順","reason":"target_covered","mutation_started":false,"phase":"preflight"}
```

The agent reobserved/redecided and hit the five-consecutive-stale-cycle limit. Stale cycles also include prediction/observation failures, so their count need not equal the number of recorded dropdown failures. Four dropdown preflight rejections were recorded in this run (three covered-target failures and one page/target freshness failure).

A fresh, read-only browser evaluation after stopping independently returned:

- URL: <https://www.amazon.co.jp/usb-type-c-3m/s?k=usb+type+c+3m>
- Title: `Amazon.co.jp : usb type c 3m`
- Search heading: `検索結果 3,000 以上 のうち 1-48件 "usb type c 3m"`
- Displayed sort: `おすすめ`

This confirms arrival at an Amazon search-results page, **not a product page or successful price sorting**. The selected native sort control was covered; this change does not add support for the site's custom dropdown. Cheapest-price correctness was not evaluated.

The ignored local report is `artifacts/dropdown-live-final-20260918.json`; it contains counts, failure diagnostics and a fresh URL/title/heading read, not a full-page dump. An earlier development run (`artifacts/dropdown-live-20260918.json`) also stopped with the covered sort control after 3 actions, 12 policy decisions and 1 text-model call, at `https://www.amazon.co.jp/type-c-to-3m/s?k=type-c+to+type-c+3m`. Its elapsed-time field predates the correction that records the final failed cycle, so use the final-code report for timing.

Follow-up: [decorated-dropdown solution experiments](dropdown-solution-experiment.md) compare two opt-in prototypes without changing the production executor.
