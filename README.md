# Hermes Telegram

A Telegram bot that runs the **real Hermes gateway adapter** against an existing
Hermes install — without forking Hermes, vendoring a copy, or creating a second
environment.

It borrows the engine, venv, login and skills from your install. It keeps its own
conversation history, its own config, and an **editable copy of the adapter**, so
`hermes update` can never overwrite your work.

```
hermes install ─────────► borrowed (read-only): engine, venv, auth, skills
        │
        └─► this bot: own HERMES_HOME, own state.db, own editable adapter copy
```

---

## Setup

### 1. Prerequisites

- A working Hermes install you're logged into (`hermes` runs, `~/.hermes/auth.json` exists)
- Python 3.11+ and `git`
- A bot token from [@BotFather](https://t.me/BotFather) and your numeric Telegram user id
  (message [@userinfobot](https://t.me/userinfobot))

### 2. Clone and wire it in

```sh
git clone https://github.com/dommurphy155/Hermes_telegram2.0.git
cd Hermes_telegram2.0
./ht install          # symlinks, config seed, plugin enable — run once
```

`install` is idempotent. It:

| step | what it does |
|---|---|
| `installs`, `tools`, `skills`, `auth.json` | symlinks to your `~/.hermes/` (no copying, no 430 MB venv) |
| `config.yaml` | seeded from your install (mode 600) |
| `.env` | created empty (mode 600) |
| `telegram-platform` plugin | enabled, so the adapter override takes effect |

It never writes outside this directory.

### 3. Add your credentials

Edit `home/.env`:

```sh
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_ALLOWED_USERS=<your numeric user id>
TELEGRAM_HOME_CHANNEL=<your numeric user id>
```

Or run the wizard, which verifies the token with Telegram and sends a test message:

```sh
./ht setup
```

### 4. Run it

```sh
./ht up          # foreground; Ctrl-C to stop
./ht status      # running? how many commands registered?
./ht log         # tail the gateway log
./ht doctor      # 12-point wiring check
./ht down        # stop
```

Send `/help` to your bot. You should get the full Hermes command menu.

### Optional: point it at a different Hermes install

```sh
HERMES_CLI=/path/to/hermes HERMES_TELEGRAM_HOME=/path/to/bot/home ./ht install
```

---

## How the isolation works

| | mechanism |
|---|---|
| **Engine** | your installed `hermes` CLI (shared, maintained by `hermes update`) |
| **Python deps** | `home/installs` → your `~/.hermes/installs`. No duplicate venv. |
| **Login** | `home/auth.json` → your `~/.hermes/auth.json`. One credential. |
| **Skills** | `home/skills` → your `~/.hermes/skills`. Every skill visible. |
| **History** | `home/state.db` — its own. Separate conversations. |
| **Config** | `home/config.yaml` — its own real file. |
| **Adapter code** | `home/plugins/platforms/telegram/` — a copy, editable. |

Nothing in this project lives inside the Hermes checkout, so `hermes update`
cannot overwrite, reset or break it.

`./ht doctor` verifies all of the above, including that the override is actually
winning (it asks Hermes' plugin resolver which path it resolved to).

---

## The adapter copy is the point

The bot does **not** use the bundled adapter. It uses:

```
home/plugins/platforms/telegram/     ← edit this
```

Hermes' plugin discovery treats a same-named plugin under a home's `plugins/`
directory as an override of the bundled one. Because this bot's `HERMES_HOME` is
`home/`, that copy shadows the install's copy.

Verified, not assumed: a marker string added to the copy's connect log line
appeared at startup, then disappeared when reverted.

Edit the files, `./ht up`, and your changes are live. The install's own copy is
never touched.

If you replace the adapter wholesale, re-enable it once:

```sh
HERMES_HOME=$PWD/home hermes plugins enable telegram-platform
```

---

## What's built on top of the adapter

### `/sessions` — inline session browser

- 5 sessions per page, ⬅️➡️ pagination, sort by recent / newest / alphabetical
- **Sticky search** — tap 🔍, then each message you type narrows the list
  (`h` → `he` → `hel`), editing the same card
- Session details: Resume, Rename, Generate Summary, Delete
- **Delete** asks for confirmation (✅ / ❌) before touching anything
- **Summaries** are capped at 500 characters, edited into the card in place
- Empty shells (a `/new` where nothing was said) are never listed
- Ownership is scoped fail-closed: only sessions from *your* chat, and the check
  is repeated at resume and delete time, not just at list time

### `/undo` — pick a point to rewind to

- Lists your recent messages, newest first, truncated to 60 characters
- 5 per page with ⬅️➡️ pagination
- Tap one → confirmation → rewind
- `/undo N` (with a number) still goes straight through, unchanged
- Rewinds the exact session the list came from, even if you've since started a
  newer empty one

Both surfaces use plain text, not MarkdownV2 — chat titles with `.` `-` `!` in
them would otherwise crash the edit.

---

## Layout

```
hermes_telegram.py     setup / install / status / doctor / send
ht                     launcher (install|setup|up|down|status|log|doctor|send)
env.example            .env template
home/                  the bot's own HERMES_HOME
├── .env               its token + allowlist (mode 600, not committed)
├── config.yaml        its config (mode 600, not committed)
├── state.db           its sessions and history (not committed)
└── plugins/platforms/telegram/   ← the editable adapter copy
```

`home/` also holds runtime state (logs, sessions, caches, locks). None of it is
committed.

---

## Security

- **`home/.env` and `home/state.db` are gitignored.** The token and your
  conversation history never reach the repo. The `.gitignore` is verified before
  each push.
- **`TELEGRAM_ALLOWED_USERS`** gates every reply. Only listed user ids get one;
  anyone else is refused.
- **Ownership checks are fail-closed.** A session that can't be positively
  attributed to your chat is dropped, not shown.

If a token is ever pasted into a chat or an issue, `/revoke` it in @BotFather and
put the new one in `home/.env`. Revoking a GitHub PAT is a different credential —
do both.

---

## Troubleshooting

**`/sessions` or `/undo` missing from the menu** — the override isn't winning:

```sh
./ht doctor     # look for "override shadows the bundled adapter"
```

**`No adapter available`** — the copy registered under the wrong manifest key. It
must live at `home/plugins/platforms/telegram/`, not `home/plugins/telegram/`.

**`separate state.db ✗`** — the bot hasn't started yet. Run `./ht up` once.

**Bot replies to nothing** — check `TELEGRAM_ALLOWED_USERS` in `home/.env`, and
that the running gateway is this one:

```sh
./ht status
```

**Changes to the adapter don't take effect** — restart. `./ht down && ./ht up`.