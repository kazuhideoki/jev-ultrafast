"""Authenticated loopback bridge for the voice extension. No API keys leave Python."""

import base64
import hashlib
import hmac
import json
import math
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
from .intent import Goals, Superseded, context_page, interpret, vocabulary

PORT = 8767
MAX_SECONDS = 600
TRANSCRIPT_TIMEOUT_SECONDS = 30


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
        self.goals = None
        self.expected_epoch = None
        self.mutation_started = False
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

    def guard(self):
        self.check()
        goals = getattr(self, "goals", None)
        epoch = getattr(self, "expected_epoch", None)
        if goals and epoch is not None and not goals.valid(epoch):
            if self.mutation_started:
                raise RuntimeError("Instruction changed during dispatched input; no retry")
            raise Superseded("Instruction changed before input")

    def call(self, method, _mutation=False, **params):
        goals = getattr(self, "goals", None)
        # Epoch invalidation and sending the next RPC are serialized, not the network wait.
        lock = goals.lock if goals else threading.RLock()
        with lock:
            self.guard()
            self.serial += 1
            call_id = self.serial
            epoch = getattr(self, "expected_epoch", None)
            self.send({"type": "rpc", "id": call_id, "method": method, "params": params,
                       "epoch": epoch})
        result = self.take(self.responses, timeout=15)
        if result.get("id") != call_id:
            raise RuntimeError("Browser command interrupted; stopped without retry")
        if result.get("error") == "superseded":
            if self.mutation_started:
                raise RuntimeError("Instruction changed during input; no retry")
            raise Superseded("Extension rejected obsolete RPC before dispatch")
        if result.get("error"):
            raise RuntimeError("Browser command interrupted; stopped without retry")
        if _mutation:
            self.mutation_started = True
        return result.get("result", {})


class BorrowedBrowser(Browser):
    def __init__(self, bridge):
        self.transport = bridge.call
        self.session = None
        self.target = None
        self.owned = False
        self.bridge = bridge
        self.guarded_transport = True

    def act(self, action, page, text=None):
        self.bridge.mutation_started = False
        result = super().act(action, page, text)
        self.bridge.mutation_started = False
        return result

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



class AudioTurns:
    """PCM energy/silence boundaries for models without server-side VAD."""

    def __init__(self, threshold=0.008):
        self.threshold = threshold
        self.prefix = b""
        self.active = False
        self.silence_ms = 0
        self.duration_ms = 0

    def feed(self, audio):
        samples = [sample[0] for sample in struct.iter_unpack("<h", audio)]
        rms = math.sqrt(sum(value * value for value in samples) / len(samples)) / 32768
        duration = len(audio) / 48
        voiced = rms >= self.threshold
        started = voiced and not self.active
        if not self.active and not voiced:
            self.prefix = (self.prefix + audio)[-14400:]
            return False, b"", False
        chunk = self.prefix + audio if started else audio
        self.prefix = b""
        self.active = True
        self.duration_ms += duration
        self.silence_ms = 0 if voiced else self.silence_ms + duration
        if self.duration_ms > 30000:
            raise VoiceError("audio_long", "発話が30秒を超えました。区切りを入れて話してください。")
        committed = self.silence_ms >= 650
        if committed:
            self.active = False
            self.duration_ms = self.silence_ms = 0
        return started, chunk, committed


def transcription_error(event):
    error = event.get("error", {})
    code = error.get("code", "unknown")
    # Never return upstream messages: they can echo user-supplied content.
    if code in {"invalid_value", "unknown_parameter", "invalid_parameter"}:
        return VoiceError("transcription_config", "音声APIが接続設定を拒否しました。サーバーの更新が必要です。")
    if code in {"model_not_found", "insufficient_quota", "rate_limit_exceeded"}:
        return VoiceError("transcription_" + code, "音声APIのモデル利用権限・残高・利用上限を確認してください。")
    return VoiceError("transcription_failed", "音声APIで文字起こしに失敗しました。接続し直してください。")


def stream_transcripts(bridge, goals, output):
    """Upload continuously, reconcile final turns in speech order, never execute deltas."""
    with connect(
        "wss://api.openai.com/v1/realtime?intent=transcription",
        additional_headers={"Authorization": f"Bearer {openai_key()}"},
        open_timeout=15, max_size=2**20,
    ) as upstream:
        stopped = threading.Event()
        errors = queue.Queue()
        committed_items = queue.Queue()
        remote_items = {}
        awaiting_finals = {}
        upstream.send(json.dumps({"type": "session.update", "session": {
            "type": "transcription", "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": 24000},
                "transcription": {"model": "gpt-live-transcribe", "delay": "low"},
                "turn_detection": None,
            }}}}))

        def upload():
            previous_words = None
            turns = AudioTurns()
            local_item = None
            try:
                while not stopped.is_set():
                    event = bridge.take(bridge.events)
                    if event.get("type") != "audio":
                        raise VoiceError("audio_protocol", "音声接続を再開してください。")
                    audio = base64.b64decode(event["audio"], validate=True)
                    if not audio or len(audio) % 2 or len(audio) > 48000:
                        raise ValueError("Invalid audio chunk")
                    bridge.diagnostics["audio_ms"] = bridge.diagnostics.get("audio_ms", 0) + len(audio) // 48
                    started, chunk, committed = turns.feed(audio)
                    if started:
                        local_item = secrets.token_hex(16)
                        with goals.lock:
                            goals.begin(local_item)
                            bridge.send({"type": "gate", "epoch": goals.epoch, "paused": True})
                    if chunk:
                        upstream.send(json.dumps({"type": "input_audio_buffer.append",
                                                  "audio": base64.b64encode(chunk).decode()}))
                    if committed:
                        with goals.lock:
                            awaiting_finals[local_item] = time.monotonic()
                        committed_items.put(local_item)
                        upstream.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    with goals.lock:
                        words = vocabulary(goals.page)
                    if words != previous_words:
                        upstream.send(json.dumps({"type": "session.update", "session": {
                            "type": "transcription", "audio": {"input": {"transcription": {
                                "model": "gpt-live-transcribe", "delay": "low", "keywords": words,
                            }}}}}))
                        previous_words = words
            except Exception as error:
                errors.put(error)

        uploader = threading.Thread(target=upload, daemon=True)
        uploader.start()
        order, finals, partials, delivered = [], {}, {}, set()
        try:
            while True:
                bridge.check()
                if not errors.empty():
                    raise errors.get_nowait()
                with goals.lock:
                    if any(time.monotonic() - since > TRANSCRIPT_TIMEOUT_SECONDS
                           for since in awaiting_finals.values()):
                        raise VoiceError(
                            "transcription_timeout", "文字起こしの応答が途絶えました。再接続してください。"
                        )
                try:
                    event = json.loads(upstream.recv(timeout=0.1))
                except TimeoutError:
                    continue
                kind, item = event.get("type"), event.get("item_id")
                if kind in {"error", "conversation.item.input_audio_transcription.failed"}:
                    raise transcription_error(event)
                if kind in {"input_audio_buffer.speech_started", "input_audio_buffer.committed"}:
                    if not isinstance(item, str) or not item:
                        raise ValueError("Missing speech item identity")
                    if item not in order and item not in delivered:
                        order.append(item)
                        if kind == "input_audio_buffer.committed" and not committed_items.empty():
                            remote_items[item] = committed_items.get_nowait()
                        with goals.lock:
                            goals.begin(remote_items.get(item, item))
                            bridge.send({"type": "gate", "epoch": goals.epoch, "paused": True})
                elif kind == "conversation.item.input_audio_transcription.delta":
                    if item in delivered:
                        continue
                    partials[item] = (partials.get(item, "") + event.get("delta", ""))[:2000]
                    bridge.send({"type": "partial", "text": partials[item]})
                elif kind == "conversation.item.input_audio_transcription.completed":
                    if item in delivered:
                        continue
                    if not isinstance(item, str) or not item:
                        raise ValueError("Missing transcript identity")
                    text = event.get("transcript", "").strip()
                    if len(text) > 2000:
                        raise ValueError("Transcript too long")
                    finals[item] = text
                while order and order[0] in finals:
                    item = order.pop(0)
                    text = finals.pop(item)
                    partials.pop(item, None)
                    delivered.add(item)
                    local = remote_items.pop(item, item)
                    with goals.lock:
                        awaiting_finals.pop(local, None)
                    output.put_nowait((local, text))
                if len(order) + len(finals) + len(partials) > 100:
                    raise ValueError("Unresolved transcription backlog")
        finally:
            stopped.set()
            # Closing upstream interrupts uploads. Bridge closure wakes its bounded queue wait.
            upstream.close()


def continuous(bridge):
    goals = bridge.goals = Goals()
    browser = BorrowedBrowser(bridge)
    goals.page = context_page(browser.observe(screenshot=False))
    utterances, failures = queue.Queue(maxsize=100), queue.Queue()

    def worker_failed(error):
        # A failed input pipeline must invalidate in-flight model results immediately.
        with goals.lock:
            goals.epoch += 1
            goals.status = "blocked"
            failures.put(error)
            try:
                bridge.send({"type": "gate", "epoch": goals.epoch, "paused": True})
            except Exception:
                pass

    def transcriber():
        try:
            stream_transcripts(bridge, goals, utterances)
        except Exception as error:
            worker_failed(error)

    def interpreter():
        try:
            while not bridge.closed.is_set():
                try:
                    item, text = utterances.get(timeout=0.1)
                except queue.Empty:
                    continue
                if not text:
                    with goals.lock:
                        goals.pending.discard(item)
                        goals.seen.add(item)
                    continue
                bridge.send({"type": "transcript", "text": text})
                # Only this worker applies patches. Browser completion may advance a queued goal;
                # pending utterances prevent that advance until the patch is applied.
                patch = interpret(goals.context(item, text))
                goals.apply(patch, item, text)
                bridge.diagnostics["intent_calls"] = bridge.diagnostics.get("intent_calls", 0) + 1
                with goals.lock:
                    bridge.send({"type": "goal", "revision": goals.revision, "goal": goals.goal,
                                 "state": goals.status,
                                 "question": goals.question if goals.status == "clarification" else ""})
        except Exception as error:
            worker_failed(error)

    workers = [threading.Thread(target=fn, daemon=True) for fn in (transcriber, interpreter)]
    for worker in workers:
        worker.start()
    agent = None
    applied = None
    try:
        while True:
            bridge.check()
            if not failures.empty():
                raise failures.get_nowait()
            task = goals.task()
            if not task:
                time.sleep(0.03)
                continue
            epoch, revision, goal = task
            bridge.expected_epoch = epoch
            try:
                with goals.lock:
                    bridge.guard()
                    bridge.send({"type": "gate", "epoch": epoch, "paused": False})
                if agent is None:
                    agent = Agent(None, goal, browser=browser)
                    agent.goal_guard = bridge.guard
                if applied != (epoch, revision):
                    agent.update_goal(goal, revision)
                    agent.state["page"] = agent.browser.observe(screenshot=False)
                    applied = (epoch, revision)
                with goals.lock:
                    goals.page = context_page(agent.state["page"])
                    goals.actions = agent.state["history"][-8:]
                state = agent.command("tick")
                with goals.lock:
                    goals.page = context_page(state["page"])
                    goals.actions = state["history"][-8:]
                if state["status"] in {"done", "blocked"}:
                    bridge.guard()
                    page = agent.browser.observe(screenshot=False)
                    status = goals.finish(epoch, state["status"], page)
                    if status:
                        with goals.lock:
                            goals.page = context_page(page)
                            bridge.send({"type": "goal", "revision": goals.revision, "goal": goals.goal,
                                         "state": goals.status, "question": ""})
                        labels = {"completed": "画面上の条件を確認しました · 次の指示を待っています",
                                  "awaiting_confirmation": "確認待ち · 結果を確認して追加の指示を話してください",
                                  "blocked": "操作を停止しました · 追加の指示を待っています",
                                  "running": "次の目標を実行中…"}
                        bridge.send({"type": "status", "text": labels[status]})
                else:
                    bridge.send({"type": "status", "text": f"聞き取り継続 · 実行中 · {len(state['history'])} 操作"})
            except Superseded:
                bridge.diagnostics["discarded_cycles"] = bridge.diagnostics.get("discarded_cycles", 0) + 1
                if agent:
                    agent.pending_text = None
                    agent.state["decision"] = None
                applied = None
            finally:
                bridge.expected_epoch = None
    finally:
        if agent:
            agent.close()
        for worker in workers:
            worker.join(timeout=0.3)

def handle(ws, token):
    bridge = None
    stage = "接続"
    try:
        if not authenticate(ws, token):
            ws.close(1008, "Pairing required")
            return
        bridge = Bridge(ws)
        bridge.send({"type": "ready"})
        start = bridge.take(bridge.events)
        if start.get("type") != "start":
            raise VoiceError("protocol", "拡張を再読み込みしてください。")
        if start.get("continuous") is True:
            stage = "連続操作"
            continuous(bridge)
            return
        bridge.deadline = time.monotonic() + 120
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
