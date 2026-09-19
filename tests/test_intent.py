"""Offline goal/dispatch race contracts; no provider calls."""

import copy
import json
import queue
import threading
import time
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import intent, voice


def patch(goals, item, mode="new", **changes):
    return {"base_revision": goals.revision, "utterance_id": item, "mode": mode,
            "purpose": "Show matching results" if mode in {"new", "enqueue"} else None,
            "checks": [], "upsert": [], "remove": [], "question": "", **changes}


def apply(goals, item, mode="new", **changes):
    goals.begin(item)
    proposal = patch(goals, item, mode, **changes)
    goals.apply(proposal, item, item)
    return proposal


def test_correction_preserves_other_conditions_and_deduplicates():
    goals = intent.Goals()
    apply(goals, "1", upsert=[{"id": "status", "text": "Unresolved", "checks": []},
                             {"id": "owner", "text": "Tanaka", "checks": []}])
    old_epoch = goals.epoch
    proposal = apply(goals, "2", "amend", upsert=[{"id": "owner", "text": "Sato", "checks": []}])
    assert [c["text"] for c in goals.goal["conditions"]] == ["Unresolved", "Sato"]
    assert goals.history[-1]["status"] == "superseded"
    assert not goals.valid(old_epoch)
    assert not goals.apply(proposal, "2", "duplicate")
    assert goals.revision == 2


def test_new_task_drops_unrelated_conditions_and_pending_queue():
    goals = intent.Goals()
    apply(goals, "1", upsert=[{"id": "owner", "text": "Sato", "checks": []}])
    apply(goals, "2", "enqueue", purpose="Open settings")
    assert goals.goal["conditions"] and len(goals.queue) == 1
    apply(goals, "3", purpose="Open inbox")
    assert not goals.goal["conditions"] and not goals.queue


def test_invalid_patch_does_not_partially_apply():
    goals = intent.Goals()
    apply(goals, "1")
    goals.begin("2")
    original = copy.deepcopy(goals.goal)
    with pytest.raises(ValueError):
        goals.apply(patch(goals, "2", "amend", purpose="Other", remove=["missing"]), "2", "bad")
    assert goals.goal == original and goals.revision == 1 and goals.pending == {"2"}
    with pytest.raises(ValueError, match="Obsolete"):
        goals.apply({**patch(goals, "2"), "base_revision": 0}, "2", "bad")


def test_done_without_evidence_waits_and_never_advances_queue():
    goals = intent.Goals()
    apply(goals, "1")
    apply(goals, "2", "enqueue", purpose="Open settings")
    assert goals.finish(goals.epoch, "done", {"text": "results"}) == "awaiting_confirmation"
    assert len(goals.queue) == 1 and goals.task() is None
    apply(goals, "3", "amend", upsert=[{"id": "order", "text": "Sort by date", "checks": []}])
    assert "Sort by date" in goals.task()[2]


def test_verified_goal_advances_queue_and_stale_done_cannot_finish():
    goals = intent.Goals()
    apply(goals, "1", checks=[{"kind": "text", "label": "", "expected": "Saved Aurora"}])
    epoch = goals.epoch
    apply(goals, "2", "enqueue", purpose="Open settings")
    assert goals.finish(epoch, "done", {"text": "Saved Aurora"}) is None
    assert goals.finish(goals.epoch, "done", {"text": "Saved Aurora"}) == "running"
    assert goals.goal["purpose"] == "Open settings" and not goals.queue
    assert goals.history[-1]["status"] == "completed"


def test_begun_speech_blocks_dispatch_before_interpretation():
    goals = intent.Goals()
    apply(goals, "1")
    epoch = goals.epoch
    goals.begin("2")
    assert not goals.valid(epoch) and goals.task() is None
    apply(goals, "3", "pause")
    assert goals.status == "paused"


def test_control_evidence_requires_unique_exact_observed_label():
    goal = {"checks": [{"kind": "value", "label": "Owner", "expected": "Sato"}], "conditions": []}
    page = {"actions": [{"node": 1, "label": "Owner", "value": "Tanaka"}]}
    assert not intent.verify(goal, page)[0]
    page["actions"][0]["value"] = "Sato"
    assert intent.verify(goal, page)[0]
    page["actions"].append({"node": 2, "label": "Owner", "value": "Sato"})
    assert not intent.verify(goal, page)[0]


def test_vocabulary_excludes_values_and_body():
    assert intent.vocabulary({"text": "private body", "actions": [
        {"node": 1, "label": "Owner", "value": "private value"},
        {"node": 2, "label": "bad\nkeyword"}, {"node": 3, "label": "a@example.com"},
    ]}) == ["Owner"]


@pytest.mark.parametrize("during", ["choose", "text"])
def test_revision_changes_during_model_call_never_act(monkeypatch, during):
    page = {"actions": [{"id": "e1", "kind": "fill", "label": "Search", "node": 1}],
            "fingerprint": "p", "title": "test", "text": "", "url": "https://example.test"}
    browser = Mock(observe=Mock(return_value=page), fresh=Mock(return_value=True))
    agent = loop.Agent(None, "old", browser=browser)
    goals = intent.Goals()
    apply(goals, "1")
    epoch = goals.epoch

    def guard():
        if not goals.valid(epoch):
            raise intent.Superseded()

    agent.goal_guard = guard

    def choose(*_):
        if during == "choose":
            goals.begin("2")
        return {"choice": "e1"}

    def text(*_):
        goals.begin("2")
        return "old", {"model": "offline", "latency_ms": 0}

    monkeypatch.setattr(loop, "choose", choose)
    monkeypatch.setattr(loop, "field_text", text)
    with pytest.raises(intent.Superseded):
        agent.command("tick")
    browser.act.assert_not_called()
    agent.update_goal("new", 2)
    assert agent.pending_text is None and agent.state["decision"] is None


def test_rpc_revision_gate_and_uncertain_input():
    bridge = voice.Bridge.__new__(voice.Bridge)
    bridge.closed = threading.Event()
    bridge.deadline = time.monotonic() + 5
    bridge.goals = intent.Goals()
    apply(bridge.goals, "1")
    bridge.expected_epoch = bridge.goals.epoch
    bridge.mutation_started = False
    bridge.serial = 0
    bridge.responses = queue.Queue()
    bridge.send = Mock()
    bridge.goals.begin("2")
    with pytest.raises(intent.Superseded):
        bridge.call("Input.insertText", _mutation=True, text="old")
    bridge.send.assert_not_called()
    bridge.mutation_started = True
    with pytest.raises(RuntimeError, match="no retry"):
        bridge.call("Input.insertText", _mutation=True, text="old")
    bridge.send.assert_not_called()


def test_stream_orders_final_transcripts_and_never_dispatches_deltas(monkeypatch):
    goals = intent.Goals()
    events = iter([
        {"type": "input_audio_buffer.speech_started", "item_id": "1"},
        {"type": "conversation.item.input_audio_transcription.delta", "item_id": "1", "delta": "Open"},
        {"type": "input_audio_buffer.committed", "item_id": "1"},
        {"type": "input_audio_buffer.speech_started", "item_id": "2"},
        {"type": "input_audio_buffer.committed", "item_id": "2"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "2", "transcript": "Sato"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "1", "transcript": "Tanaka"},
        {"type": "conversation.item.input_audio_transcription.completed", "item_id": "1", "transcript": "duplicate"},
    ])
    output = queue.Queue()
    bridge = Mock()
    bridge.closed = threading.Event()
    bridge.take.side_effect = lambda *_: bridge.closed.wait(1)

    class Upstream:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def send(self, raw):
            assert json.loads(raw)["type"] == "session.update"

        def close(self):
            pass

        def recv(self, **_):
            try:
                return json.dumps(next(events))
            except StopIteration:
                raise RuntimeError("fixture end") from None

    monkeypatch.setattr(voice, "connect", lambda *a, **kw: Upstream())
    monkeypatch.setattr(voice, "openai_key", lambda: "offline")
    with pytest.raises(RuntimeError, match="fixture end"):
        voice.stream_transcripts(bridge, goals, output)
    bridge.closed.set()
    assert [output.get_nowait(), output.get_nowait()] == [("1", "Tanaka"), ("2", "Sato")]
    assert output.empty() and goals.epoch == 2
    assert goals.pending == {"1", "2"}


def test_short_answer_request_contains_question_and_original_goal(monkeypatch):
    goals = intent.Goals()
    apply(goals, "1", upsert=[{"id": "status", "text": "Unresolved only", "checks": []}])
    goals.begin("2")
    goals.apply(patch(goals, "2", "clarify", question="担当者は田中さんですか、佐藤さんですか？"),
                "2", "担当者も絞り込んで")
    goals.begin("3")
    context = goals.context("3", "後者で")
    proposal = patch(goals, "3", "amend", checks=None,
                     upsert=[{"id": "owner", "text": "佐藤さん", "checks": []}])

    def post(_url, _key, body):
        sent = json.loads(body["messages"][1]["content"])
        assert sent["utterance"] == "後者で"
        assert sent["pending_clarification"]["original_request"] == "担当者も絞り込んで"
        assert sent["pending_clarification"]["question"] == "担当者は田中さんですか、佐藤さんですか？"
        assert sent["recent_dialogue"][-1] == {
            "role": "assistant", "content": "担当者は田中さんですか、佐藤さんですか？"}
        assert sent["goal"]["conditions"][0]["text"] == "Unresolved only"
        return {"choices": [{"message": {"content": json.dumps(proposal)}}]}

    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline")
    monkeypatch.setattr(intent, "post_json", post)
    goals.apply(intent.interpret(context), "3", "後者で")
    assert goals.status == "running"
    assert [c["text"] for c in goals.goal["conditions"]] == ["Unresolved only", "佐藤さん"]
    assert goals.clarification is None and goals.question == ""
    assert goals.context("4", "はい")["pending_clarification"] is None


def test_initial_clarification_keeps_original_request_across_followups():
    goals = intent.Goals()
    apply(goals, "open A or B", "clarify", question="AですかBですか？")
    apply(goals, "which?", "clarify", question="Aは一覧、Bは設定です。どちらですか？")
    context = goals.context("answer", "B")
    assert context["goal"] is None
    assert context["pending_clarification"]["original_request"] == "open A or B"
    assert context["pending_clarification"]["question"].startswith("Aは一覧")
    assert goals.task() is None
    # Context snapshots cannot be mutated by later question updates.
    apply(goals, "answer", purpose="Open settings")
    assert goals.task()[2] == "Open settings"
    assert context["pending_clarification"]["question"].startswith("Aは一覧")
    assert goals.clarification is None


def test_pause_resume_cannot_bypass_unanswered_question():
    goals = intent.Goals()
    apply(goals, "1")
    apply(goals, "2", "clarify", question="AですかBですか？")
    for i in range(10):
        apply(goals, f"pause-{i}", "pause")
    apply(goals, "resume", "resume")
    assert goals.status == "clarification" and goals.task() is None
    assert goals.context("answer", "B")["pending_clarification"]["question"] == "AですかBですか？"
    apply(goals, "unrelated", purpose="Open a different page")
    assert goals.clarification is None and goals.question == ""


def test_empty_clarification_is_rejected_without_state_change():
    goals = intent.Goals()
    goals.begin("1")
    with pytest.raises(ValueError, match="requires a question"):
        goals.apply(patch(goals, "1", "clarify", question="  "), "1", "ambiguous")
    assert goals.revision == 0 and goals.dialogue == [] and goals.clarification is None


def test_clarified_followup_restores_original_goal_then_advances():
    goals = intent.Goals()
    apply(goals, "1", purpose="Save report", checks=[{"kind": "text", "label": "", "expected": "Saved"}])
    apply(goals, "after that open A or B", "clarify", question="A or B?")
    apply(goals, "B", "enqueue", purpose="Open B")
    assert goals.status == "running" and goals.task()[2] == "Save report"
    assert goals.clarification is None and goals.question == ""
    assert [g["purpose"] for g in goals.queue] == ["Open B"]
    assert goals.finish(goals.epoch, "done", {"text": "Saved"}) == "running"
    assert goals.task()[2] == "Open B" and goals.queue == []


@pytest.mark.parametrize("status", ["completed", "paused", "awaiting_confirmation"])
def test_clarified_followup_preserves_prior_execution_state(status):
    goals = intent.Goals()
    apply(goals, "1", purpose="Current")
    goals.status = status
    apply(goals, "next A or B", "clarify", question="A or B?")
    apply(goals, "B", "enqueue", purpose="Open B")
    assert goals.clarification is None
    if status == "completed":
        assert goals.task()[2] == "Open B"
    else:
        assert goals.status == status and goals.task() is None
        assert goals.queue[0]["purpose"] == "Open B"
