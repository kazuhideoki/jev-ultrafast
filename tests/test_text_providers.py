"""Offline HTTP contracts for text providers; never use real API credentials."""

import json

import httpx
import pytest

from jev_ultrafast import model
from jev_ultrafast.demo import load_environment


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_openai_luna_from_env_file(monkeypatch, tmp_path, effort):
    for name in ("TEXT_MODEL_API_KEY", "TEXT_MODEL_BASE_URL", "TEXT_MODEL", "TEXT_MODEL_REASONING"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TEXT_MODEL_API_KEY=offline-test\n"
        "TEXT_MODEL_BASE_URL=https://api.openai.com/v1/\n"
        "TEXT_MODEL=gpt-6-luna\n"
        f"TEXT_MODEL_REASONING={effort}\n"
    )
    load_environment()

    def handle(request):
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer offline-test"
        body = json.loads(request.content)
        assert body["model"] == "gpt-6-luna"
        assert body["reasoning_effort"] == effort
        assert body["max_completion_tokens"] == 1024
        assert "max_tokens" not in body
        assert "reasoning" not in body
        assert body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"text":"東京"}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(model, "CLIENT", client)
        assert model.field_text({"goal": "Search for Tokyo"})[0] == "東京"


@pytest.mark.parametrize(
    "base,effort,expected",
    [
        ("https://openrouter.ai/api/v1", "none", {"reasoning": {"enabled": False}}),
        ("https://openrouter.ai/api/v1", None, {"reasoning": {"effort": "low"}}),
        ("https://api.deepseek.com/v1", None, {"thinking": {"type": "disabled"}}),
        ("https://api.openai.com/v1", None, {"reasoning_effort": "none"}),
    ],
)
def test_provider_request_compatibility(monkeypatch, base, effort, expected):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "offline-test")
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", base)
    monkeypatch.setenv("TEXT_MODEL", "test-model")
    if effort is None:
        monkeypatch.delenv("TEXT_MODEL_REASONING", raising=False)
    else:
        monkeypatch.setenv("TEXT_MODEL_REASONING", effort)

    def handle(request):
        body = json.loads(request.content)
        for name in ("reasoning", "reasoning_effort", "thinking"):
            if name in expected:
                assert body[name] == expected[name]
            else:
                assert name not in body
        if "reasoning_effort" not in expected:
            assert body["max_tokens"] == 1024
            assert "max_completion_tokens" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"text":"Tokyo"}'}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(model, "CLIENT", client)
        assert model.field_text({"goal": "Search for Tokyo"})[0] == "Tokyo"
