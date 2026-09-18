"""Authenticated loopback bridge for the voice extension. No API keys leave Python."""

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from websockets.sync.client import connect
from websockets.sync.server import serve

from .agent import Agent
from .browser import Browser
from .demo import load_environment

PORT = 8767
MAX_SECONDS = 120


class VoiceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def failure_details(error, stage):
    if isinstance(error, VoiceError):
        return error.code, str(error)
    known = {
        "Invalid TypeSafe response; no action executed.": (
            "decision_invalid",
            "操作対象の判断結果を検証できませんでした。操作は実行していません。",
        ),
        "Text helper returned no valid field value; nothing typed.": (
            "text_invalid",
            "入力する文字列を生成できませんでした。文字入力は行っていません。",
        ),
    }
    if str(error) in known:
        return known[str(error)]
    return "unexpected", f"{stage}で停止しました（{type(error).__name__}）。サーバーの診断ログを確認してください。"


def openai_key():
    key = os.environ.get("OPENAI_API_KEY")
    if not key and urlparse(os.environ.get("TEXT_MODEL_BASE_URL", "")).hostname == "api.openai.com":
        key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("Configure OPENAI_API_KEY or the existing OpenAI TEXT_MODEL_API_KEY in .env")
    return key


def pairing_proof(token, role, client_nonce, server_nonce):
    message = f"jev-voice-v1:{role}:{client_nonce}:{server_nonce}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def authenticate(ws, token):
    hello = json.loads(ws.recv(timeout=5))
    client_nonce = hello.get("nonce", "") if isinstance(hello, dict) else ""
    if not isinstance(client_nonce, str) or not re.fullmatch(r"[a-f0-9-]{36}", client_nonce):
        return False
    server_nonce = secrets.token_hex(32)
    ws.send(
        json.dumps(
            {
                "type": "challenge",
                "nonce": server_nonce,
                "proof": pairing_proof(token, "server", client_nonce, server_nonce),
            }
        )
    )
    answer = json.loads(ws.recv(timeout=5))
    proof = answer.get("proof", "") if isinstance(answer, dict) else ""
    return isinstance(proof, str) and secrets.compare_digest(
        proof, pairing_proof(token, "client", client_nonce, server_nonce)
    )


class Bridge:
    """One reader, bounded queues, one outstanding CDP call, no mutation retries."""

    def __init__(self, ws):
        self.ws = ws
        self.closed = threading.Event()
        self.events = queue.Queue(maxsize=400)
        self.responses = queue.Queue(maxsize=4)
        self.serial = 0
        self.diagnostics = {}
        self.deadline = time.monotonic() + MAX_SECONDS
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        try:
            for raw in self.ws:
                event = json.loads(raw)
                if not isinstance(event, dict) or event.get("type") == "cancel":
                    break
                target = self.responses if event.get("type") == "rpc_result" else self.events
                target.put_nowait(event)
        except Exception:
            pass
        finally:
            self.closed.set()

    def check(self):
        if self.closed.is_set() or time.monotonic() >= self.deadline:
            raise RuntimeError("Stopped: disconnected, cancelled, or time limit reached")

    def send(self, event):
        self.check()
        self.ws.send(json.dumps(event))

    def take(self, source, timeout=35):
        end = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < end:
            self.check()
            try:
                return source.get(timeout=0.1)
            except queue.Empty:
                pass
        raise RuntimeError("Timed out; no operation will be retried")

    def call(self, method, **params):
        self.serial += 1
        call_id = self.serial
        self.send({"type": "rpc", "id": call_id, "method": method, "params": params})
        result = self.take(self.responses, timeout=15)
        if result.get("id") != call_id or result.get("error"):
            raise RuntimeError("Browser command interrupted; stopped without retry")
        return result.get("result", {})


class BorrowedBrowser(Browser):
    def __init__(self, bridge):
        self.transport = bridge.call
        self.session = None
        self.target = None
        self.owned = False

    def close(self):
        # The extension detaches. The user's tab must never be closed.
        pass


def transcribe(bridge):
    with connect(
        "wss://api.openai.com/v1/realtime?intent=transcription",
        additional_headers={"Authorization": f"Bearer {openai_key()}"},
        open_timeout=15,
        max_size=2**20,
    ) as upstream:
        upstream.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "transcription": {"model": "gpt-transcribe"},
                                "turn_detection": None,
                            }
                        },
                    },
                }
            )
        )
        total = 0
        peak = 0
        diagnostics = getattr(bridge, "diagnostics", {})
        while True:
            event = bridge.take(bridge.events)
            if event.get("type") == "commit":
                break
            if event.get("type") != "audio":
                raise VoiceError("audio_protocol", "録音データを受け取れませんでした。拡張を再読み込みしてください。")
            audio = base64.b64decode(event["audio"], validate=True)
            total += len(audio)
            if len(audio) % 2:
                raise VoiceError("audio_format", "録音データの形式が不正です。拡張を再読み込みしてください。")
            peak = max(peak, max((abs(v[0]) for v in struct.iter_unpack("<h", audio)), default=0))
            diagnostics.update(audio_ms=round(total / 48), audio_peak=peak)
            if len(audio) % 2 or total > 24000 * 2 * 30:
                raise VoiceError("audio_long", "録音が30秒を超えました。短い指示で試してください。")
            upstream.send(json.dumps({"type": "input_audio_buffer.append", "audio": event["audio"]}))
        if total < 4800:
            raise VoiceError(
                "audio_short", "録音が短すぎます。「録音中」が表示されてから、キーを押したまま話してください。"
            )
        upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))
        bridge.send({"type": "status", "text": "文字起こし中…"})
        until = time.monotonic() + 30
        while time.monotonic() < until:
            bridge.check()
            try:
                event = json.loads(upstream.recv(timeout=0.2))
            except TimeoutError:
                continue
            if event.get("type") in {"error", "conversation.item.input_audio_transcription.failed"}:
                raise RuntimeError("OpenAI transcription failed; check model access and configuration")
            if event.get("type") == "conversation.item.input_audio_transcription.completed":
                goal = event.get("transcript", "").strip()
                diagnostics["transcript_chars"] = len(goal)
                if not goal:
                    raise VoiceError(
                        "transcript_empty",
                        f"音声を文字にできませんでした（録音 {total / 48000:.1f} 秒）。"
                        "拡張の設定でマイクの入力音量を確認してから、キーを押したまま話してください。",
                    )
                if len(goal) > 2000:
                    raise VoiceError("transcript_long", "指示が長すぎます。短い指示で試してください。")
                return goal
        raise RuntimeError("Transcription timed out")


def handle(ws, token):
    bridge = None
    stage = "接続"
    try:
        if not authenticate(ws, token):
            ws.close(1008, "Pairing required")
            return
        bridge = Bridge(ws)
        bridge.send({"type": "ready"})
        stage = "文字起こし"
        goal = transcribe(bridge)
        stage = "ブラウザ操作"
        bridge.send({"type": "transcript", "text": goal})
        with Agent(None, goal, browser=BorrowedBrowser(bridge)) as agent:
            for state in agent.run():
                bridge.check()
                bridge.send({"type": "status", "text": f"実行中 · {len(state['history'])} 操作"})
            # DONE is a model decision, not independent verification of an arbitrary goal.
            bridge.send(
                {
                    "type": "finished",
                    "status": agent.state["status"],
                    "text": "操作を終了しました（結果を確認してください）"
                    if agent.state["status"] == "done"
                    else "操作を停止しました。目的を達成できませんでした。",
                }
            )
    except Exception as error:
        if bridge and not bridge.closed.is_set():
            # Numeric diagnostics only: never log audio, transcripts, page data, or credentials.
            code, message = failure_details(error, stage)
            print(
                json.dumps(
                    {
                        "event": "voice_failure",
                        "stage": stage,
                        "code": code,
                        "exception": type(error).__name__,
                        **bridge.diagnostics,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            try:
                bridge.send({"type": "failed", "text": message, "code": code})
            except Exception:
                pass
    finally:
        if bridge:
            bridge.closed.set()
        ws.close()


def main():
    load_environment()
    openai_key()
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise ValueError("Configure TYPESAFE_API_KEY in .env")
    token_path = Path.cwd() / ".voice-token"
    if not token_path.exists():
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(32))
    token = token_path.read_text().strip()
    if len(token) < 32:
        raise ValueError("Invalid .voice-token")

    def gate(connection, request):
        origin = request.headers.get("Origin", "")
        if not re.fullmatch(r"chrome-extension://[a-p]{32}", origin):
            return connection.respond(403, "Extension origin required\n")
        if request.headers.get("Host") != f"127.0.0.1:{PORT}":
            return connection.respond(403, "Loopback host required\n")

    print(f"Voice bridge: ws://127.0.0.1:{PORT}")
    print(f"Pairing token saved in {token_path}; paste it into extension settings.")
    with serve(
        lambda ws: handle(ws, token), "127.0.0.1", PORT, process_request=gate, max_size=2**20, max_queue=16
    ) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
