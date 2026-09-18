"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from browser_harness import _ipc as harness_ipc
from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
ACTIONABILITY = Path(__file__).with_name("actionability.js").read_text()
READ_STATE = Path(__file__).with_name("snapshot.js").read_text().replace("__JEV_ACTIONABILITY__", ACTIONABILITY)
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class DropdownUncertain(RuntimeError):
    """A dropdown may have changed; never automatically retry it."""


def dropdown_failure(action, reason, started):
    diagnostic = {
        "kind": "select", "target": action.get("target_label", action.get("label", ""))[:200],
        "option": action.get("option_label", "")[:200],
        "reason": reason, "mutation_started": started,
        "phase": "preflight" if started is False else "execution",
    }
    logging.getLogger(__name__).warning("Dropdown failure: %s", json.dumps(diagnostic, ensure_ascii=False))
    error = (StalePage if started is False else DropdownUncertain)(
        "Dropdown execution: " + json.dumps(diagnostic, ensure_ascii=False)
    )
    error.diagnostic = diagnostic
    return error


class Browser:
    def __init__(self, url):
        try:
            ensure_daemon()
        except RuntimeError as error:
            # Harness 0.1.13 can hide a missing DevTools port behind a generic startup error.
            if "didn't come up" in str(error):
                try:
                    log = harness_ipc.log_path(os.environ.get("BU_NAME", "default"))
                    with open(log, "rb") as stream:
                        stream.seek(0, 2)
                        stream.seek(max(0, stream.tell() - 8192))
                        detail = stream.read().decode("utf-8", errors="replace")
                except OSError:
                    detail = ""
                if detail.strip().splitlines() and "DevToolsActivePort not found" in detail.strip().splitlines()[-1]:
                    raise RuntimeError(
                        "Browser remote debugging is not enabled. In Brave, open "
                        "brave://inspect/#remote-debugging (Chrome: chrome://inspect/#remote-debugging), "
                        "enable 'Allow remote debugging for this browser instance', then rerun "
                        "and approve the connection prompt. This grants access to the browser profile."
                    ) from error
            raise
        self.target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        if getattr(self, "transport", None):
            return self.transport(method, **params)
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      const dropdown=action.proxy_for!==undefined;
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,dropdown ? 600 : autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[
                          ...root.querySelectorAll('[role="option"],[role="menuitem"]')]);
                        if (++frames>=2 && (!(autocomplete || dropdown) || options.some(e=>{
                          if (dropdown && action.prior_options.includes(window.__jevFast?.ids.get(e))) return false;
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot,
                     **({"transport": self.transport} if getattr(self, "transport", None) else {})}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        try:
            fresh = self.fresh(page, action)
        except StalePage:
            if action["kind"] == "select":
                raise dropdown_failure(action, "document_changed", False) from None
            raise
        if not fresh:
            if action["kind"] == "select":
                raise dropdown_failure(action, "page_or_target_changed", False)
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({
            "operation": "act", "session": self.session, "action": action, "text": text,
            "owned": getattr(self, "owned", True),
            **({"transport": self.transport} if getattr(self, "transport", None) else {}),
        })
        self.after_input = action if action["kind"] != "wait" else None
        if "proxy_for" in action:
            self.after_input = {**action, "prior_options": [
                a["node"] for a in page["actions"] if a.get("role") in {"option", "menuitem"}
            ]}
        return result

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        if request.get("transport"):
            return request["transport"](method, **params)
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        selecting = operation == "act" and request["action"]["kind"] == "select"
        try:
            result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        except Exception:
            if selecting:
                raise dropdown_failure(request["action"], "evaluation_response_lost", None) from None
            raise
        if result.get("exceptionDetails"):
            if selecting:
                raise dropdown_failure(request["action"], "evaluation_interrupted", None)
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            size = evaluate("({width:innerWidth,height:innerHeight})")
            call("Input.dispatchMouseEvent", type="mouseWheel", x=size["width"] / 2,
                 y=size["height"] / 2, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const {resolveTarget}=""" + ACTIONABILITY + """;
              let started=false;
              const reject=reason=>action.kind==='select' ? {ok:false,started,reason} : null;
              try {
              const e=window.__jevFast?.nodes.get(action.node);
              const hit=resolveTarget(e,action);
              if (hit.reason) return reject(hit.reason);
              const {x,y}=hit;
              // Keep ordinary new-tab links in the agent-owned tab; arbitrary popups remain unsupported.
              if (action.owned && action.kind==='click' && e.tagName==='A' &&
                  (e.getAttribute('target') || document.querySelector('base')?.target)==='_blank')
                e.setAttribute('target','_self');
              if (action.kind==='select') {
                if (e.tagName!=='SELECT') return reject('target_not_select');
                const option=window.__jevFast.nodes.get(action.option_node);
                if (!option?.isConnected || ![...e.options].includes(option)) return reject('option_missing');
                if (option.value!==action.value || option.label!==action.option_label)
                  return reject('option_changed');
                if (option.disabled || option.closest('optgroup[disabled]')) return reject('option_disabled');
                started=true;
                e.selectedIndex=option.index;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y,ok:true,started};
              } catch (_) {
                if (action.kind==='select') return reject(started ? 'execution_exception' : 'validation_exception');
                throw _;
              }
            })(""" + json.dumps({**action, "owned": request.get("owned", True)}) + ")")
            if kind == "select":
                if not isinstance(target, dict) or target.get("ok") is not True:
                    if isinstance(target, dict) and target.get("ok") is False:
                        raise dropdown_failure(action, target["reason"], target["started"])
                    raise dropdown_failure(action, "execution_not_confirmed", None)
            elif target is None:
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
