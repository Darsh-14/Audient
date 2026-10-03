"""api/understand.py against a stand-in OpenAI-compatible server."""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "tests"))
import stub_llm  # noqa: E402

CTX = {"today": "2026-10-02 (Friday)", "utterance": "I gotta get from Bombay over to the capital tomorrow",
       "tools": [], "state": {}, "results": [], "conversation": []}


@pytest.fixture()
def api(monkeypatch):
    srv = stub_llm.serve(0)
    monkeypatch.setenv("AUDIENT_LLM_BASE_URL", f"http://127.0.0.1:{srv.server_address[1]}/v1")
    monkeypatch.setenv("AUDIENT_LLM_API_KEY", "test-key")
    monkeypatch.setenv("AUDIENT_LLM_MODEL", "stub-model")
    monkeypatch.setenv("AUDIENT_DOTENV", "0")
    stub_llm.Stub.calls.clear()
    stub_llm.Stub.reject_json_mode = stub_llm.Stub.fenced = False
    import understand
    yield importlib.reload(understand)
    srv.shutdown()


def test_status_and_understanding(api):
    assert api.status() == {"configured": True, "model": "stub-model"}
    out = api.understand(CTX)
    assert out["result"]["tool"] == "search_flights"
    assert out["result"]["args"] == {"origin": "Mumbai", "destination": "Delhi", "date": "2026-10-03"}
    sent = stub_llm.Stub.calls[-1]
    assert sent["model"] == "stub-model" and sent["messages"][0]["role"] == "system"


def test_provider_without_json_mode_and_fenced_reply(api):
    stub_llm.Stub.reject_json_mode = True
    stub_llm.Stub.fenced = True
    out = api.understand(CTX)
    assert out["result"]["tool"] == "search_flights"
    assert "response_format" not in stub_llm.Stub.calls[-1]  # retried without JSON mode


def test_not_configured(monkeypatch):
    monkeypatch.setenv("AUDIENT_DOTENV", "0")  # ignore a developer's local .env
    monkeypatch.delenv("AUDIENT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AUDIENT_LLM_MODEL", raising=False)
    import understand
    mod = importlib.reload(understand)
    assert mod.status()["configured"] is False
    with pytest.raises(RuntimeError):
        mod.understand(CTX)
