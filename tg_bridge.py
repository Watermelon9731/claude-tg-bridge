#!/usr/bin/env python3
"""Telegram -> Claude Agent SDK -> VC bridge.

Chat the Telegram bot; a persistent Claude Code session on this VPS reads the
repo (its CLAUDE.md carries the vibechat rules), answers, and drives vc.py.
Reads run free; writes (vc.py send|task|upload, git push/commit, file edits)
pause for /confirm. VC tasks are async, so finished tasks are pushed back.

Env (see .env.example): TG_BOT_TOKEN, TG_ALLOWLIST, REPO_DIR, [MODEL], [POLL_SECONDS].
Auth: `claude` CLI logged in on this box, or ANTHROPIC_API_KEY set.
"""
import asyncio
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from claude_agent_sdk import (
        ClaudeSDKClient,
        ClaudeAgentOptions,
        PermissionResultAllow,
        PermissionResultDeny,
    )
except ModuleNotFoundError:  # not installed here (e.g. running the unit tests)
    ClaudeSDKClient = ClaudeAgentOptions = PermissionResultAllow = PermissionResultDeny = None

# --- config ---------------------------------------------------------------
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
ALLOWLIST = {int(x) for x in os.environ.get("TG_ALLOWLIST", "").replace(" ", "").split(",") if x}
REPO_DIR = os.environ.get("REPO_DIR", ".")
MODEL = os.environ.get("MODEL", "claude-opus-4-8")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "90"))
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Bash commands that mutate the outside world -> need /confirm.
WRITE_RE = re.compile(r"vc\.py\s+(send|task|upload)|git\s+(push|commit)")
# Agent emits [WATCH #123] / [WATCH REQ-0056] to have a VC task tracked.
WATCH_RE = re.compile(r"\[WATCH\s+([#\w-]+)\]")

SYSTEM_PROMPT = (
    "You are driven remotely over Telegram by the repo owner. Be concise. "
    "This repo's CLAUDE.md holds the vibechat rules for driving the VC agent via "
    "scripts/vc.py (task != chat, thread != read, done != report) — follow it. "
    "Reads run freely; any write (vc.py send/task/upload, git push/commit, file edits) "
    "is gated and the owner will be asked to approve it. When you create or expect a "
    "VC task whose result should be pushed back later, put a marker `[WATCH #<id>]` "
    "in your reply so this bridge can track it."
)

# /menu buttons -> a preset prompt for the agent. key -> (button label, prompt)
QUICK_ACTIONS = {
    "vc_agents": ("📋 Task/agent VC", "Liệt kê trạng thái các VC agent và task gần đây (vc.py agents). Tóm tắt ngắn."),
    "latest_req": ("🆕 REQ mới nhất", "Tìm REQ mới nhất trong requests/ và tóm tắt trạng thái của nó."),
    "backlog": ("📌 Backlog", "Đọc 05-log/BACKLOG.md, tóm tắt các mục đang mở."),
    "status": ("📊 STATUS", "Đọc STATUS.md và tóm tắt tình hình hiện tại."),
}

# friendly model names -> full id (anything else is passed through as-is)
MODEL_PRESETS = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}

# /<cmd> [text] -> run a repo skill. cmd -> (menu description, prompt template {rest})
SKILL_CMDS = {
    "po": ("Skill Product Owner", "Dùng skill ai-concierge-po. {rest}"),
    "designer": ("Skill Designer", "Dùng skill ai-concierge-designer. {rest}"),
}

HELP = (
    "Bridge Telegram → Claude → VC.\n\n"
    "• Gõ tự do = nói với agent (đọc repo, chạy vc.py).\n"
    "• /menu — nút tác vụ nhanh\n"
    "• /status <id> — trạng thái VC task/REQ\n"
    "• /model [opus|sonnet|haiku] — xem/đổi model\n"
    "• /po <việc> — chạy skill Product Owner\n"
    "• /designer <việc> — chạy skill Designer\n"
    "• /reset — xoá context\n\n"
    "Lệnh ghi (vc.py send/task/upload, git push) sẽ hiện nút [✅ Duyệt] [❌ Từ chối]."
)


# --- Telegram (blocking urllib, run via asyncio.to_thread) -----------------
def _tg_call(method: str, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{TG_API}/{method}", data=data,
                                 headers={"User-Agent": "claude-tg-bridge/1.0"})
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.load(r)


async def tg_send(chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    # Telegram caps a message at 4096 chars; chunk on line boundaries.
    # reply_markup (inline keyboard) rides on the last chunk only.
    chunks = list(_chunk(text, 4000))
    for i, chunk in enumerate(chunks):
        params = {"chat_id": chat_id, "text": chunk}
        if reply_markup and i == len(chunks) - 1:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            await asyncio.to_thread(_tg_call, "sendMessage", params)
        except Exception as e:  # noqa: BLE001 - never let a send crash the loop
            print(f"[tg_send] {e}")


async def tg_answer_callback(cb_id: str, text: str = "") -> None:
    try:
        await asyncio.to_thread(_tg_call, "answerCallbackQuery",
                                {"callback_query_id": cb_id, "text": text})
    except Exception as e:  # noqa: BLE001
        print(f"[tg_answer] {e}")


def kb(*rows) -> dict:
    """Inline keyboard from rows of (label, callback_data) tuples."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def _chunk(text: str, size: int):
    text = text or "(empty)"
    while text:
        cut = text[:size]
        if len(text) > size and "\n" in cut:
            cut = cut[: cut.rindex("\n") + 1]
        yield cut
        text = text[len(cut):]


# --- pure helpers (unit-tested) -------------------------------------------
def extract_watch_ids(text: str) -> list[str]:
    return [m.lstrip("#") for m in WATCH_RE.findall(text or "")]


def is_write_command(cmd: str) -> bool:
    return bool(WRITE_RE.search(cmd or ""))


def is_task_done(vc_output: str) -> bool:
    # ponytail: naive token match on `vc.py task-show` output. Tune this against
    # real output if the status word differs (e.g. "completed"/"success").
    return bool(re.search(r"\bdone\b", vc_output or "", re.I))


def extract_text(message) -> str:
    """Pull text out of an SDK message regardless of exact block classes."""
    parts = []
    for block in getattr(message, "content", None) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "".join(parts)


# --- per-chat state --------------------------------------------------------
class Chat:
    def __init__(self, client: ClaudeSDKClient):
        self.client = client
        self.lock = asyncio.Lock()
        self.pending: asyncio.Future | None = None  # awaiting /confirm


chats: dict[int, Chat] = {}
watchers: dict[str, int] = {}  # vc task/req id -> chat_id
current_model = MODEL          # switchable at runtime via /model


def resolve_model(arg: str) -> str:
    return MODEL_PRESETS.get(arg.lower().strip(), arg.strip())


async def switch_model(new_model: str) -> None:
    global current_model
    current_model = new_model
    for cid, chat in list(chats.items()):
        try:
            await chat.client.set_model(new_model)
        except Exception as e:  # noqa: BLE001 - rebuild on next query if unsupported
            print(f"[set_model] {e}; dropping session {cid} to rebuild")
            try:
                await chat.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            chats.pop(cid, None)


def _make_can_use_tool(chat_id: int):
    async def can_use_tool(tool_name, input_data, context):
        # Reads are in allowed_tools -> callback never fires for them.
        # Bash: allow read-only, gate writes. Everything else (Write/Edit/...) is gated.
        if tool_name == "Bash":
            cmd = input_data.get("command", "")
            if not is_write_command(cmd):
                return PermissionResultAllow(updated_input=input_data)
            proposal = f"⚠️ Duyệt lệnh ghi?\n\n{cmd}"
        else:
            proposal = f"⚠️ Duyệt {tool_name}?\n\n{json.dumps(input_data, ensure_ascii=False)[:1500]}"

        await tg_send(chat_id, proposal + "\n\n(bấm nút hoặc /confirm · /deny)",
                      reply_markup=kb([("✅ Duyệt", "confirm"), ("❌ Từ chối", "deny")]))
        fut = asyncio.get_running_loop().create_future()
        chats[chat_id].pending = fut
        try:
            approved = await fut
        finally:
            chats[chat_id].pending = None
        if approved:
            return PermissionResultAllow(updated_input=input_data)
        return PermissionResultDeny(message="Owner denied via Telegram.")

    return can_use_tool


async def get_chat(chat_id: int) -> Chat:
    chat = chats.get(chat_id)
    if chat is None:
        client = ClaudeSDKClient(ClaudeAgentOptions(
            cwd=REPO_DIR,
            system_prompt=SYSTEM_PROMPT,
            model=current_model,
            allowed_tools=["Read", "Grep", "Glob"],  # auto-approved
            permission_mode="default",
            can_use_tool=_make_can_use_tool(chat_id),
        ))
        await client.connect()
        chat = Chat(client)
        chats[chat_id] = chat
    return chat


async def run_query(chat_id: int, text: str) -> None:
    chat = await get_chat(chat_id)
    async with chat.lock:
        try:
            await chat.client.query(text)
            parts = []
            async for msg in chat.client.receive_response():
                t = extract_text(msg)
                if t:
                    parts.append(t)
            reply = "\n".join(p for p in parts if p.strip()).strip()
        except Exception as e:  # noqa: BLE001
            reply = f"[agent error] {e}"
    await tg_send(chat_id, reply or "(khong co phan hoi)")
    for wid in extract_watch_ids(reply):
        watchers[wid] = chat_id


# --- command dispatch ------------------------------------------------------
def _resolve_pending(chat_id: int, approved: bool) -> bool:
    chat = chats.get(chat_id)
    if chat and chat.pending and not chat.pending.done():
        chat.pending.set_result(approved)
        return True
    return False


async def send_menu(chat_id: int) -> None:
    rows = [[(label, f"q:{key}")] for key, (label, _) in QUICK_ACTIONS.items()]
    await tg_send(chat_id, "Chọn tác vụ:", reply_markup=kb(*rows))


async def handle_message(chat_id: int, text: str) -> None:
    if chat_id not in ALLOWLIST:
        return  # silently drop unknown chats
    text = text.strip()

    # /confirm and /deny must run even while a query holds the chat lock.
    if text in ("/confirm", "/deny"):
        if not _resolve_pending(chat_id, text == "/confirm"):
            await tg_send(chat_id, "Không có gì chờ duyệt.")
        return

    if text == "/reset":
        chat = chats.pop(chat_id, None)
        if chat:
            try:
                await chat.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        await tg_send(chat_id, "Đã reset context.")
        return

    if text in ("/menu", "/start"):
        await send_menu(chat_id)
        return

    if text == "/help":
        await tg_send(chat_id, HELP)
        return

    if text.startswith("/model"):
        arg = text[len("/model"):].strip()
        if not arg:
            await tg_send(chat_id, f"Model hiện tại: {current_model}",
                          reply_markup=kb([("Opus", "m:opus"), ("Sonnet", "m:sonnet"),
                                           ("Haiku", "m:haiku")]))
        else:
            m = resolve_model(arg)
            await switch_model(m)
            await tg_send(chat_id, f"Đã chuyển model: {m}")
        return

    if text.startswith("/status"):
        arg = text[len("/status"):].strip()
        q = (f"Kiểm tra trạng thái VC task/REQ {arg} và trả về report nếu có "
             "(dùng đúng lệnh vc.py theo CLAUDE.md)." if arg
             else "Liệt kê trạng thái các VC agent/task gần đây (vc.py agents).")
        await run_query(chat_id, q)
        return

    # skill commands: /po <việc>, /designer <việc>
    if text.startswith("/"):
        head = text.split()[0][1:]
        if head in SKILL_CMDS:
            rest = text[len(text.split()[0]):].strip()
            _, tmpl = SKILL_CMDS[head]
            await run_query(chat_id, tmpl.format(rest=rest).strip())
            return

    await run_query(chat_id, text)


async def handle_callback(chat_id: int, cb_id: str, data: str) -> None:
    if chat_id not in ALLOWLIST:
        await tg_answer_callback(cb_id)
        return
    if data in ("confirm", "deny"):
        ok = _resolve_pending(chat_id, data == "confirm")
        await tg_answer_callback(
            cb_id, ("Đã duyệt ✅" if data == "confirm" else "Đã từ chối ❌") if ok else "Không có gì chờ")
        return
    if data.startswith("q:"):
        await tg_answer_callback(cb_id)
        act = QUICK_ACTIONS.get(data[2:])
        if act:
            await run_query(chat_id, act[1])
        return
    if data.startswith("m:"):
        m = resolve_model(data[2:])
        await switch_model(m)
        await tg_answer_callback(cb_id, f"Model: {m}")
        await tg_send(chat_id, f"Đã chuyển model: {m}")
        return
    await tg_answer_callback(cb_id)


# --- VC async poller (push) ------------------------------------------------
def _vc_task_show(task_id: str) -> str:
    tid = task_id.lstrip("#")
    try:
        out = subprocess.run(["python3", "scripts/vc.py", "task-show", tid],
                             cwd=REPO_DIR, capture_output=True, text=True, timeout=60)
        return out.stdout + out.stderr
    except Exception as e:  # noqa: BLE001
        return f"[vc error] {e}"


async def poller() -> None:
    while True:
        await asyncio.sleep(POLL_SECONDS)
        for wid, chat_id in list(watchers.items()):
            out = await asyncio.to_thread(_vc_task_show, wid)
            if is_task_done(out):
                watchers.pop(wid, None)
                await tg_send(chat_id, f"✅ VC task {wid} bao done — dang lay report...")
                # Reuse the agent to fetch the real report (it knows done != report).
                asyncio.create_task(run_query(
                    chat_id,
                    f"VC task {wid} vua done. Chay vc.py de lay full report/thread "
                    "cua no (thread != read, done != report theo CLAUDE.md) roi dan lai."))


# --- main loop -------------------------------------------------------------
async def poll_updates() -> None:
    offset = 0
    while True:
        try:
            resp = await asyncio.to_thread(_tg_call, "getUpdates",
                                           {"offset": offset, "timeout": 50})
        except Exception as e:  # noqa: BLE001
            print(f"[getUpdates] {e}")
            await asyncio.sleep(3)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            if "callback_query" in upd:  # inline-button tap
                cb = upd["callback_query"]
                asyncio.create_task(handle_callback(
                    cb["message"]["chat"]["id"], cb["id"], cb.get("data", "")))
                continue
            msg = upd.get("message") or upd.get("edited_message")
            if not msg or "text" not in msg:
                continue
            asyncio.create_task(handle_message(msg["chat"]["id"], msg["text"]))


async def register_commands() -> None:
    cmds = [
        {"command": "menu", "description": "Nút tác vụ nhanh"},
        {"command": "status", "description": "Trạng thái VC task/REQ <id>"},
        {"command": "model", "description": "Xem / đổi model"},
        {"command": "po", "description": "Skill Product Owner"},
        {"command": "designer", "description": "Skill Designer"},
        {"command": "reset", "description": "Xoá context"},
        {"command": "help", "description": "Hướng dẫn"},
    ]
    try:
        await asyncio.to_thread(_tg_call, "setMyCommands", {"commands": json.dumps(cmds)})
    except Exception as e:  # noqa: BLE001
        print(f"[setMyCommands] {e}")


async def main() -> None:
    if not ALLOWLIST:
        raise SystemExit("TG_ALLOWLIST is empty — refusing to run open to everyone.")
    print(f"bridge up: repo={Path(REPO_DIR).resolve()} model={MODEL} allow={ALLOWLIST}")
    await register_commands()
    await asyncio.gather(poll_updates(), poller())


if __name__ == "__main__":
    asyncio.run(main())
