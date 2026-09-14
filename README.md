# claude-tg-bridge

Drive a Claude Code session on your VPS from a Telegram bot. You chat the bot →
a persistent Claude Agent SDK session reads your framework repo (its `CLAUDE.md`
carries the vibechat rules), answers, and drives `scripts/vc.py`. Reads run
free; writes pause for approval. Async VC tasks get pushed back when they finish.

```
Telegram  ──►  tg_bridge.py (VPS)  ──►  Claude Agent SDK (cwd = your repo)
   ▲                                          │  reads repo, runs vc.py
   └────────── report / reply ◄───────────────┘  writes gated by /confirm
```

## What it does

- **Persistent per-chat session** — context carries across messages; `/reset` clears it.
- **Write gate** — reads/grep/`vc.py read|agents|thread` run automatically; any
  write (`vc.py send|task|upload`, `git push|commit`, file edits) sends you the
  exact command and waits for `/confirm` or `/deny`.
- **Async tracking** — when a VC task is created the agent tags it `[WATCH #id]`;
  a background poller detects `done` and pushes the report. `/status <id>` pulls on demand.
- **Access control** — only chat IDs in `TG_ALLOWLIST` are served; others dropped.

## Commands

| command | effect |
|---|---|
| *(any text)* | talk to the agent |
| `/confirm` `/deny` | approve / reject a pending write |
| `/status <id>` | fetch a VC task/REQ status + report |
| `/reset` | drop the conversation, start fresh |

## Setup (on the VPS)

```bash
git clone git@github.com-private:Watermelon9731/claude-tg-bridge.git
cd claude-tg-bridge
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt          # claude-agent-sdk
npm install -g @anthropic-ai/claude-code  # the SDK drives the `claude` CLI
claude login                             # or export ANTHROPIC_API_KEY
cp .env.example .env && $EDITOR .env     # bot token, allowlist, REPO_DIR
python tg_bridge.py
```

Create the bot with [@BotFather](https://t.me/BotFather); get your chat ID by
messaging the bot then opening `https://api.telegram.org/bot<TOKEN>/getUpdates`.

Run under systemd (auto-restart, survives reboot): edit paths in
`claude-tg-bridge.service`, then `sudo cp` it to `/etc/systemd/system/`,
`sudo systemctl enable --now claude-tg-bridge`.

## Test

```bash
python3 test_tg_bridge.py   # checks write-detection, watch parsing, chunking
```

## Notes / limits (ponytail)

- In-memory state — a restart drops sessions and watch list (VC tasks still run;
  re-issue `/status`). Add SQLite only if you need history across restarts.
- `is_task_done()` matches the word `done` in `vc.py task-show` output — tune the
  token if your output uses a different status word.
- No webhook (long-poll needs no public IP), single-user (allowlist = you).
