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


def test_local_turns_buffer_prefix_and_commit_once_after_silence():
    import struct

    quiet = bytes(4800)
    speech = struct.pack('<2400h', *([1200] * 2400))
    turns = voice.AudioTurns()
    for _ in range(20):
        assert turns.feed(quiet) == (False, b'', False)
    started, chunk, committed = turns.feed(speech)
    assert started and len(chunk) == 14400 + 4800 and not committed
    for _ in range(6):
        assert turns.feed(quiet)[2] is False
    assert turns.feed(quiet)[2] is True
    assert turns.feed(quiet) == (False, b'', False)
    assert turns.feed(speech)[0] is True


def test_local_turn_too_long_stops_without_partial_commit():
    import struct

    speech = struct.pack('<2400h', *([1200] * 2400))
    turns = voice.AudioTurns()
    for _ in range(300):
        assert turns.feed(speech)[2] is False
    with pytest.raises(voice.VoiceError, match='30秒'):
        turns.feed(speech)


def test_live_transcription_uses_manual_commits_and_maps_local_item(monkeypatch):
    import struct

    from jev_ultrafast.intent import Goals

    bridge = bridge_without_reader()
    bridge.events = queue.Queue()
    bridge.diagnostics = {}
    goals = Goals()
    output = queue.Queue()
    for audio in [struct.pack('<2400h', *([1200] * 2400)), *([bytes(4800)] * 8)]:
        bridge.events.put({'type': 'audio', 'audio': base64.b64encode(audio).decode()})
    original_check = bridge.check

    def check():
        original_check()
        if not output.empty():
            raise RuntimeError('fixture complete')

    bridge.check = check

    class Upstream:
        def __init__(self):
            self.events = queue.Queue()
            self.sent = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def close(self):
            bridge.closed.set()

        def send(self, raw):
            event = json.loads(raw)
            self.sent.append(event)
            if event['type'] == 'input_audio_buffer.commit':
                self.events.put({'type': 'input_audio_buffer.committed', 'item_id': 'remote-1'})
                self.events.put({'type': 'conversation.item.input_audio_transcription.completed',
                                 'item_id': 'remote-1', 'transcript': 'Open settings'})

        def recv(self, timeout):
            try:
                return json.dumps(self.events.get(timeout=timeout))
            except queue.Empty:
                raise TimeoutError() from None

    upstream = Upstream()
    monkeypatch.setattr(voice, 'connect', lambda *a, **kw: upstream)
    monkeypatch.setattr(voice, 'openai_key', lambda: 'offline')
    with pytest.raises(RuntimeError, match='fixture complete'):
        voice.stream_transcripts(bridge, goals, output)
    assert upstream.sent[0]['session']['audio']['input']['turn_detection'] is None
    assert sum(e['type'] == 'input_audio_buffer.commit' for e in upstream.sent) == 1
    item, text = output.get_nowait()
    assert item in goals.pending and item != 'remote-1' and text == 'Open settings'
    assert goals.epoch == 1


def test_transcription_rejection_has_actionable_safe_error():
    error = voice.transcription_error({'error': {
        'code': 'invalid_value', 'message': 'secret echoed page text',
    }})
    assert error.code == 'transcription_config'
    assert 'secret' not in str(error)
