"""uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'"""

import argparse
import json
from pathlib import Path

from jev_ultrafast import Agent

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
parser.add_argument("--report", type=Path, help="Save counts, dropdown diagnostics and a fresh final-page read.")
args = parser.parse_args()

with Agent(args.url, args.goal) as agent:
    for state in agent.run():
        print(f"{state['elapsed_ms']:>5} ms  {len(state['history'])} actions  {state['status']}")
    if state.get("error"):
        print(state["error"])
    print(state["page"]["url"])

    if args.report:
        # A fresh read is independent of DONE and never replays an operation.
        final_page = agent.browser.evaluate("({url:location.href,title:document.title,"
                                            "headings:[...document.querySelectorAll('h1')]"
                                            ".map(e=>e.innerText.trim()).filter(Boolean).slice(0,5)})")
        report = {
            "status": state["status"], "error": state.get("error"),
            "actions": len(state["history"]), "decisions": len(state["decisions"]),
            "text_calls": len(state["text_calls"]), "elapsed_ms": state["elapsed_ms"],
            "failures": state.get("failures", []), "final_page": final_page,
        }
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False))
