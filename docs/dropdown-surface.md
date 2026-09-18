# Decorated dropdown surface support

Implemented and checked on 2026-09-18 with Brave Browser / Browser Harness.

Observation and execution now use the same read-only `actionability.js` validation for disabled/hidden targets and current hit-test geometry. Snapshot construction omits covered controls. It can recognize a visible sibling surface over a native select using local position, tabindex, pointer cursor, matching label/current value, and bounded dimensions. The surface becomes an observed `CLICK` target; the covered native select is not offered. After the click, ordinary observed menu options can be clicked.

The executor revalidates the backing control/surface relationship immediately before input. `aria-hidden` is not globally ignored: only a matched visual surface can use this path. No Amazon IDs, CSS classes, option values or fixed operation sequence are embedded in the implementation. Unmatched widgets remain unsupported. These structural and textual checks are heuristics, not a universal browser-widget association standard.

Opening a recognized surface waits for newly visible options, at most 600 ms, during the next observation. The completed click is already logged at this point. The wait is read-only and never replays the click. Generic interactions and text autocomplete retain their previous wait limits.

## Checks

- `uv run pytest`: **62 passed**, offline, including 8 shared-JavaScript validator cases run with Node.
- `uv run python scripts/check_surface.py`: **9 passed**, local real browser. Includes matching surface/menu clicks, rejection of unrelated overlays and disabled controls, relationship changes after observation, ordinary native selection, and a menu appearing 250 ms after one click.
- `uv run python scripts/check_dropdown.py`: **14 passed**, local real browser. Covers preflight races without value/events, recovery, and uncertain post-mutation results without reexecution. A persistently covered native dropdown is now omitted before model judgment.
- `uv run python scripts/check_guards.py`: **22 passed**, existing local real-browser regressions. The moving-target check uses action-specific freshness because movement can legitimately alter other controls' hit-test availability.
- Ruff, all three JavaScript syntax checks, and `uv build`: **passed**.

Local regression runs initially encountered intermittent Harness `Session with given id not found` errors during repeated tab creation. The dropdown suite now reuses one owned tab/session with a fresh document and Agent state for each case; the completed results above are from successful runs. No browser mutation was automatically retried.

## Live verification using the production implementation

No experimental patch was enabled. Both runs used `examples/run.py --report ...` and configured paid models.

| Goal | Final state | Actions | TypeSafe calls | Text calls | Loop time |
|---|---|---:|---:|---:|---:|
| Sort the Amazon search results by ascending price | done | 2 | 3 | 0 | 4,288 ms |
| Original Google-start USB Type-C / 3m request | blocked at final freshness check | 7 | 21 | 1 | 16,353 ms |

For the sorting run, a fresh independent read returned a URL containing `s=price-asc-rank` and a visible heading ending in `並べ替え::価格: 安い順`. There were no recorded operation failures. The report is `artifacts/surface-production-sort.json`.

For the original request, a fresh independent read confirmed arrival at Amazon product **B0DCZ6GFP4**. The title includes `USB Type C ケーブル` and `(3m)`. The run stopped after five stale cycles while checking completion: `Page changed since the decision. Choose again.` It did **not** stop on the former covered-dropdown error. The report is `artifacts/surface-production-original.json`.

This verifies the dropdown fix and product-page arrival, not a successful terminal result for the entire original task. Terminal freshness remains deliberately unchanged in this implementation. Cheapest-price correctness was not evaluated. Timing excludes initial observation and the independent final read.

## Reproduce

```sh
uv run python scripts/check_surface.py
uv run --env-file .env python examples/run.py \
  --url 'https://www.amazon.co.jp/usb-type-c-3m/s?k=usb+type+c+3m' \
  --goal 'この検索結果を価格が安い順に並べ替えてください。並べ替えが反映されたら終了してください。' \
  --report artifacts/surface-production-sort.json
```

The real-site command calls paid APIs; local checks and pytest do not.
