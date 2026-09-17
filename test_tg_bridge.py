"""Runnable checks for the non-trivial logic. `python3 test_tg_bridge.py`.

Only stubs TG_BOT_TOKEN so the module imports without a real bot.
"""
import os

os.environ.setdefault("TG_BOT_TOKEN", "test:token")
os.environ.setdefault("TG_ALLOWLIST", "111,222")

import tg_bridge as b  # noqa: E402


def test_write_detection():
    assert b.is_write_command("python3 scripts/vc.py task 'do X'")
    assert b.is_write_command("python3 scripts/vc.py send '@ai hi'")
    assert b.is_write_command("git push origin main")
    assert b.is_write_command("git commit -m x")
    assert not b.is_write_command("python3 scripts/vc.py read -n 20")
    assert not b.is_write_command("python3 scripts/vc.py agents")
    assert not b.is_write_command("git status")
    assert not b.is_write_command("ls -la")


def test_watch_extraction():
    assert b.extract_watch_ids("done [WATCH #15850] ok") == ["15850"]
    assert b.extract_watch_ids("[WATCH REQ-0056] and [WATCH #99]") == ["REQ-0056", "99"]
    assert b.extract_watch_ids("nothing here") == []


def test_task_done():
    assert b.is_task_done("status: done  receipt: ...")
    assert b.is_task_done("Task DONE")
    assert not b.is_task_done("status: running")
    assert not b.is_task_done("")


def test_chunking():
    assert list(b._chunk("abc", 10)) == ["abc"]
    parts = list(b._chunk("x" * 25, 10))
    assert "".join(parts) == "x" * 25 and all(len(p) <= 10 for p in parts)


def test_allowlist_parsed():
    assert b.ALLOWLIST == {111, 222}


def test_keyboard_shape():
    k = b.kb([("A", "a"), ("B", "b")], [("C", "c")])
    assert k == {"inline_keyboard": [
        [{"text": "A", "callback_data": "a"}, {"text": "B", "callback_data": "b"}],
        [{"text": "C", "callback_data": "c"}],
    ]}


def test_resolve_model():
    assert b.resolve_model("opus") == "claude-opus-4-8"
    assert b.resolve_model("SONNET") == "claude-sonnet-4-6"
    assert b.resolve_model("haiku") == "claude-haiku-4-5"
    assert b.resolve_model("claude-opus-4-8") == "claude-opus-4-8"  # passthrough


def test_skill_cmd_template():
    _, tmpl = b.SKILL_CMDS["po"]
    assert tmpl.format(rest="làm REQ-1").strip() == "Dùng skill ai-concierge-po. làm REQ-1"
    assert "ai-concierge-designer" in b.SKILL_CMDS["designer"][1]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
