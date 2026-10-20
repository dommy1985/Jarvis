from jarvis import telegram
from jarvis.telegram import Telegram, chunks


def test_chunks_split_on_lines_and_keep_everything():
    text = "\n".join(f"riga {n} " + "x" * 50 for n in range(200))
    parts = chunks(text, limit=500)
    assert all(len(p) <= 500 for p in parts) and len(parts) > 1
    assert "\n".join(parts) == text
    assert chunks("corto") == ["corto"] and chunks("") == [""]
    assert [len(p) for p in chunks("a" * 1200, limit=500)] == [500, 500, 200]  # riga unica lunghissima


def test_send_splits_and_returns_last_message_id(monkeypatch):
    calls = []

    def fake(self, method, body):
        calls.append((method, body))
        return {"ok": True, "result": {"message_id": 100 + len(calls)}}

    monkeypatch.setattr(Telegram, "_call", fake)
    tg = Telegram("t", 1, 16)
    text = "\n".join("y" * 100 for _ in range(100))  # 10k caratteri
    assert tg.send(text, reply_to=7) == 100 + len(calls) and len(calls) == 3
    assert calls[0][1]["reply_parameters"]["message_id"] == 7 and "reply_parameters" not in calls[1][1]
    assert all(c[1]["message_thread_id"] == 16 for c in calls)
    monkeypatch.setattr(Telegram, "_call", lambda self, m, b: None)
    assert tg.send("x") is False


def test_edit_ignores_not_modified(monkeypatch):
    seen = []
    monkeypatch.setattr(Telegram, "_call", lambda self, m, b: seen.append((m, b)) or {"ok": True})
    assert Telegram("t", 1, 16).edit(55, "z" * 9000) and seen[0][0] == "editMessageText"
    assert len(seen[0][1]["text"]) == telegram.LIMIT
    monkeypatch.setattr(Telegram, "_call", lambda self, m, b: None)
    assert Telegram("t", 1, 16).edit(55, "x") is False
