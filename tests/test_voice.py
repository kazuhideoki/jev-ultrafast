import base64
import json
import queue
import threading
import time

import pytest

from jev_ultrafast import voice


def test_existing_key_only_used_for_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://example.com/v1")
    with pytest.raises(ValueError):
        voice.openai_key()
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://api.openai.com/v1")
    assert voice.openai_key() == "test-key"


def bridge_without_reader():
    bridge = voice.Bridge.__new__(voice.Bridge)
    bridge.closed = threading.Event()
    bridge.deadline = time.monotonic() + 10
    bridge.responses = queue.Queue()
    bridge.serial = 0
    bridge.sent = []
    bridge.send = bridge.sent.append
    return bridge


def test_cancel_blocks_browser_commands():
    bridge = bridge_without_reader()
    bridge.closed.set()
    with pytest.raises(RuntimeError):
        bridge.take(bridge.responses)


def test_uncertain_rpc_is_not_retried():
    bridge = bridge_without_reader()
    bridge.responses.put({"id": 1, "error": "lost"})
    with pytest.raises(RuntimeError):
        bridge.call("Input.insertText", text="example")
    assert len(bridge.sent) == 1


def test_wrong_rpc_response_stops():
    bridge = bridge_without_reader()
    bridge.responses.put({"id": 99, "result": {}})
    with pytest.raises(RuntimeError):
        bridge.call("Runtime.evaluate", expression="1")
    assert len(bridge.sent) == 1


def test_borrowed_browser_does_not_close_tab():
    bridge = bridge_without_reader()
    browser = voice.BorrowedBrowser(bridge)
    browser.close()
    assert bridge.sent == []
    assert browser.owned is False


@pytest.mark.parametrize(
    "transcript,code", [("設定を開いて", None), ("", "transcript_empty"), ("x" * 2001, "transcript_long")]
)
def test_transcription_commits_after_all_audio(monkeypatch, transcript, code):
    bridge = bridge_without_reader()
    bridge.events = queue.Queue()
    audio = base64.b64encode(bytes(4800)).decode()
    bridge.events.put({"type": "audio", "audio": audio})
    bridge.events.put({"type": "commit"})

    class Upstream:
        def __init__(self):
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def send(self, raw):
            self.sent.append(json.loads(raw))

        def recv(self, **_):
            return json.dumps(
                {"type": "conversation.item.input_audio_transcription.completed", "transcript": transcript}
            )

    upstream = Upstream()
    monkeypatch.setattr(voice, "connect", lambda *a, **kw: upstream)
    monkeypatch.setattr(voice, "openai_key", lambda: "offline")
    if code:
        with pytest.raises(voice.VoiceError) as caught:
            voice.transcribe(bridge)
        assert caught.value.code == code
    else:
        assert voice.transcribe(bridge) == transcript
    assert [e["type"] for e in upstream.sent] == [
        "session.update",
        "input_audio_buffer.append",
        "input_audio_buffer.commit",
    ]
    assert upstream.sent[0]["session"]["audio"]["input"]["transcription"]["model"] == "gpt-transcribe"
    assert upstream.sent[0]["session"]["audio"]["input"]["turn_detection"] is None


def test_failure_details_never_expose_untrusted_error_text():
    code, message = voice.failure_details(ValueError("secret-page-content"), "文字起こし")
    assert code == "unexpected"
    assert "secret-page-content" not in message
    assert "文字起こし" in message


def test_empty_transcript_gives_actionable_error():
    error = voice.VoiceError("transcript_empty", "マイクの入力音量を確認してください。")
    assert voice.failure_details(error, "文字起こし") == ("transcript_empty", str(error))


def test_auth_failure_cannot_start_model(monkeypatch):
    class Socket:
        def recv(self, **_):
            return '{"token":"wrong"}'

        def close(self, *args):
            pass

    monkeypatch.setattr(voice, "transcribe", lambda *_: pytest.fail("API must not run"))
    voice.handle(Socket(), "correct")


def test_mutual_pairing_proves_both_roles_without_sending_token():
    token = "local-secret"

    class Socket:
        def __init__(self):
            self.challenge = None

        def recv(self, **_):
            if not self.challenge:
                return json.dumps({"nonce": "a" * 36})
            return json.dumps({"proof": voice.pairing_proof(token, "client", "a" * 36, self.challenge["nonce"])})

        def send(self, raw):
            assert token not in raw
            self.challenge = json.loads(raw)
            assert self.challenge["proof"] == voice.pairing_proof(token, "server", "a" * 36, self.challenge["nonce"])

    assert voice.authenticate(Socket(), token)
    assert voice.pairing_proof(token, "client", "a", "b") != voice.pairing_proof(token, "server", "a", "b")
