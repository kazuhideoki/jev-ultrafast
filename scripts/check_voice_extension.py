"""Offline, isolated Brave + real extension smoke. No paid API calls or personal profile."""

import functools
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from websockets.sync.client import connect
from websockets.sync.server import serve

from jev_ultrafast import agent, voice

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "offline-extension-test-" + "x" * 32
AUDIO_BYTES = 0
MODEL_CALLS = 0
MODEL_DELAY = 0


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        html = (
            '<a href="/settings">Settings</a>'
            if self.path == "/"
            else '<label>Nickname<input id="name"></label><button onclick="document.querySelector(\'#result\')'
            '.textContent=document.querySelector(\'#name\').value">Apply</button><p id="result"></p>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(f"<!doctype html><html><body>{html}</body></html>".encode())


class CDP:
    def __init__(self, url):
        self.ws = connect(url, max_size=8 * 1024 * 1024)
        self.counter = 0

    def call(self, method, **params):
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method, "params": params}))
        while True:
            event = json.loads(self.ws.recv(timeout=20))
            if event.get("id") == self.counter:
                if "error" in event:
                    raise RuntimeError(event["error"])
                return event.get("result", {})

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if result.get("exceptionDetails"):
            raise RuntimeError(result["exceptionDetails"])
        return result.get("result", {}).get("value")


def until(callback, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = callback()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out")


def fake_transcribe(bridge):
    global AUDIO_BYTES
    audio_size = 0
    while True:
        event = bridge.take(bridge.events)
        if event["type"] == "commit":
            break
        assert event["type"] == "audio"
        AUDIO_BYTES += len(event["audio"])
        audio_size += len(event["audio"])
    if audio_size < 6400:
        raise voice.VoiceError("audio_short", "録音が短すぎます。キーを押したまま話してください。")
    return "Open settings, enter Aurora as nickname, and apply it."


def fake_choose(page, goal, history):
    global MODEL_CALLS
    MODEL_CALLS += 1
    time.sleep(MODEL_DELAY)
    if "Aurora" in page["text"] and any(h["kind"] == "fill" for h in history):
        # Input value is separate from visible text; final result must be rendered.
        selected = "DONE"
        operation = "DONE"
    else:
        wanted = (
            ("Settings", "click")
            if "/settings" not in page["url"]
            else (("Nickname", "fill") if not any(h["kind"] == "fill" for h in history) else ("Apply", "click"))
        )
        action = next(a for a in page["actions"] if wanted[0] in a["label"] and a["kind"] == wanted[1])
        selected = action["id"]
        operation = "TYPE_TEXT" if wanted[1] == "fill" else "CLICK"
    return {
        "choice": selected,
        "operation": operation,
        "target": None,
        "confidence": 1,
        "probabilities": {selected: 1},
        "latency_ms": 0,
        "usage": {},
    }


def main():
    global MODEL_DELAY
    voice.transcribe = fake_transcribe
    agent.choose = fake_choose
    agent.field_text = lambda _: ("Aurora", {"model": "offline-fixture", "latency_ms": 0, "usage": {}})
    fixture = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    server = serve(functools.partial(voice.handle, token=TOKEN), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    executable = os.environ.get("BRAVE_EXECUTABLE", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser")
    with tempfile.TemporaryDirectory(prefix="jev-voice-test-") as profile:
        extension = Path(profile) / "extension"
        shutil.copytree(ROOT / "extension", extension)
        for name in ("offscreen.js", "manifest.json"):
            path = extension / name
            path.write_text(path.read_text().replace("127.0.0.1:8767", f"127.0.0.1:{server.socket.getsockname()[1]}"))
        process = subprocess.Popen(
            [
                executable,
                "--headless=new",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-device-for-media-stream",
                "--use-fake-ui-for-media-stream",
                "--remote-debugging-port=0",
                f"--user-data-dir={profile}",
                f"--disable-extensions-except={extension}",
                f"--load-extension={extension}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        clients = []
        try:
            portfile = Path(profile) / "DevToolsActivePort"
            port = until(lambda: portfile.read_text().splitlines()[0] if portfile.exists() else None)
            base = f"http://127.0.0.1:{port}"
            def targets():
                return httpx.get(base + "/json/list", trust_env=False).json()
            worker = until(lambda: next((t for t in targets() if t["type"] == "service_worker"), None))
            extension_id = worker["url"].split("/")[2]
            config = httpx.put(
                base + "/json/new?chrome-extension://" + extension_id + "/options.html", trust_env=False
            ).json()
            options = CDP(config["webSocketDebuggerUrl"])
            clients.append(options)
            until(lambda: options.evaluate('typeof chrome.storage !== "undefined"'))
            options.evaluate(
                f'document.querySelector("#token").value={json.dumps(TOKEN)};'
                'document.querySelector("#save").click()'
            )
            until(lambda: options.evaluate('document.querySelector("#status").textContent === "保存しました。"'))
            assert options.evaluate('chrome.storage.local.get("token").then(x=>x.token)') == TOKEN
            options.evaluate('document.querySelector("#microphone").click()')
            until(lambda: options.evaluate(
                'document.querySelector("#status").textContent.startsWith("音声入力を確認しました")'
            ))
            page_info = httpx.put(base + f"/json/new?http://127.0.0.1:{fixture.server_port}/", trust_env=False).json()
            page = CDP(page_info["webSocketDebuggerUrl"])
            clients.append(page)
            page.call("Page.bringToFront")
            until(lambda: page.evaluate('document.querySelector("a")?.textContent === "Settings"'))
            # Real trusted keyboard events exercise content script -> recording -> backend -> CDP.
            page.call("Input.dispatchKeyEvent", type="keyDown", key="I", code="KeyI", modifiers=12)
            until(
                lambda: options.evaluate('chrome.storage.session.get("status").then(x=>x.status?.startsWith("録音中"))')
            )
            time.sleep(0.5)
            page.call("Input.dispatchKeyEvent", type="keyUp", key="I", code="KeyI", modifiers=12)
            until(lambda: page.evaluate('document.querySelector("#result")?.textContent === "Aurora"'))
            until(
                lambda: options.evaluate(
                    'chrome.storage.session.get("status").then(x=>x.status?.startsWith("操作を終了"))'
                )
            )
            assert any(t["id"] == page_info["id"] for t in targets()), "User tab was closed"
            assert page.evaluate("location.pathname") == "/settings"
            instruction = "Open settings, enter Aurora as nickname, and apply it."
            assert options.evaluate('document.querySelector("#transcript").textContent') == instruction
            # Pierce the closed overlay shadow root to independently check rendered transcript text.
            document = page.call("DOM.getDocument", depth=-1, pierce=True)
            assert "指示：" + instruction in json.dumps(document, ensure_ascii=False)
            print(
                "PASS: push-to-talk, audio transport, same-tab navigation, field input, "
                "Apply result, end notification, tab retained"
            )
            print(f"Fake microphone audio received: {AUDIO_BYTES} base64 characters. Paid API calls: 0.")

            def status_starts(text):
                return options.evaluate(
                    'chrome.storage.session.get("status").then(x=>x.status?.startsWith('
                    + json.dumps(text) + '))'
                )

            # Release before microphone startup completes: must terminate, not keep recording.
            time.sleep(.2)
            before = MODEL_CALLS
            page.call("Input.dispatchKeyEvent", type="keyDown", key="I", code="KeyI", modifiers=12)
            page.call("Input.dispatchKeyEvent", type="keyUp", key="I", code="KeyI", modifiers=12)
            until(lambda: status_starts("録音が短すぎます"))
            assert MODEL_CALLS == before

            # Esc during recording must not invoke the decision model.
            time.sleep(.2)
            page.call("Input.dispatchKeyEvent", type="keyDown", key="I", code="KeyI", modifiers=12)
            until(lambda: status_starts("録音中"))
            page.call("Input.dispatchKeyEvent", type="keyDown", key="Escape", code="Escape")
            until(lambda: status_starts("中止しました"))
            page.call("Input.dispatchKeyEvent", type="keyUp", key="I", code="KeyI")
            assert MODEL_CALLS == before

            # A model response arriving after switching tabs must never execute input.
            time.sleep(.2)
            page.call("Page.navigate", url=f"http://127.0.0.1:{fixture.server_port}/settings")
            until(lambda: page.evaluate('document.querySelector("#name")?.value === ""'))
            MODEL_DELAY = 1
            page.call("Input.dispatchKeyEvent", type="keyDown", key="I", code="KeyI", modifiers=12)
            until(lambda: status_starts("録音中"))
            time.sleep(.3)
            page.call("Input.dispatchKeyEvent", type="keyUp", key="I", code="KeyI", modifiers=12)
            until(lambda: MODEL_CALLS > before)
            options.call("Page.bringToFront")
            until(lambda: status_starts("タブが切り替わった") or status_starts("フォーカスが変わった"))
            time.sleep(1.1)
            assert page.evaluate('document.querySelector("#name").value') == ""
            assert page.evaluate('document.querySelector("#result").textContent') == ""
            print("PASS: early key release, Esc cancellation, tab-switch cancellation during model latency")
        finally:
            for client in clients:
                client.ws.close()
            process.terminate()
            process.wait(timeout=15)
            server.shutdown()
            fixture.shutdown()


if __name__ == "__main__":
    main()
