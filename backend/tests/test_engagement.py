"""IDLE / ENGAGED wake-by-name and addressee checks."""
from __future__ import annotations

from backend.voice.engagement import (
    addressed_deterministic, clearly_other, has_wake_name, strip_wake_name,
)


def test_wake_name_variants():
    for s in ("Vibe", "hey Vibe", "Vibe Trader", "hey vibe trader", "vibes", "ok Vibe", "Hi Vibe,"):
        assert has_wake_name(s), s
    assert not has_wake_name("two coffees please")
    assert not has_wake_name("buy coin 1")
    assert "how are my positions" in strip_wake_name("Vibe, how are my positions?").lower()


def test_idle_requires_name():
    assert addressed_deterministic("two coffees please", "idle") is False
    assert addressed_deterministic("Vibe, how are my positions?", "idle") is True
    assert addressed_deterministic("buy one cent of JUP", "idle") is False


def test_engaged_prefers_responding():
    assert addressed_deterministic("how are my positions?", "engaged") is True
    assert addressed_deterministic("two coffees please", "engaged") is False
    assert addressed_deterministic("can I get a grande latte", "engaged") is False
    # ambiguous small talk: unsure -> None (caller prefers responding)
    assert addressed_deterministic("what a nice day", "engaged") is None


def test_clearly_other():
    assert clearly_other("two coffees please")
    assert clearly_other("I'll have a latte")
    assert not clearly_other("Vibe, two coffees and buy coin 1")  # has wake + trading? wait it has vibe
    assert not clearly_other("sell half of coin 3")


def test_voice_partial_reply_to_question_skips_classifier(monkeypatch):
    from fastapi.testclient import TestClient

    import backend.voice.engagement as eng
    from backend.app import app

    async def boom(*a, **k):  # the classifier must not run for an answer to our own question
        raise AssertionError("classifier called")

    monkeypatch.setattr(eng, "classify_addressee", boom)
    with TestClient(app) as c:
        out = c.post("/voice/partial", json={"text": "No, let's proceed.", "final": True, "seq": 1,
                                              "mode": "engaged", "answering": True,
                                              "agent_said": "Do you want to change any of these settings?"}).json()
    assert out["addressed"] is True and not out.get("side_talk")
