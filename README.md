# D M's Telegram bot

A Telegram bot that runs the **real Hermes gateway adapter** against your existing
Hermes install, with its own editable copy of the adapter code.

## Quick start

    ./ht up        # start the bot gateway (foreground; Ctrl-C to stop)
    ./ht down      # stop it
    ./ht status    # running? how many commands registered?
    ./ht log       # tail the gateway log
    ./ht doctor    # verify the wiring

## The important part: the adapter code is yours to edit

The bot does **not** use the bundled adapter. It uses a copy that lives in this
project:

    home/plugins/platforms/telegram/     ← edit this. Full adapter source.

Hermes' plugin discovery treats a same-named plugin under the home's `plugins/`
directory as an override of the bundled one, and because this project's `HERMES_HOME`
is `home/`, the copy at `home/plugins/platforms/telegram/` shadows the install's copy.

Verified, not assumed: a marker string added to the copy's connect log line appeared
at startup, then disappeared when reverted.

So — edit `home/plugins/platforms/telegram/*.py`, run `./ht up`, and your changes are
live. The install's own copy is never touched.

**One manual step after replacing the copy wholesale:** the override registers as a
user plugin and needs enabling once:

    HERMES_HOME=$PWD/home hermes plugins enable telegram-platform

## How it stays separate from the prod install

- **Code** — the adapter source is a copy in this project, editable without touching prod.
- **Engine** — the installed `hermes` CLI (shared, one copy, maintained by `hermes update`).
- **Python deps** — `home/installs` and `home/tools` symlink the owner's. No duplicate venv.
- **Login** — `home/auth.json` symlinks the owner's. One credential, no copy.
- **Skills** — `home/skills` symlinks the owner's, so every existing skill is visible.
- **Sessions / history** — `home/state.db` is the bot's own. Separate conversations.
- **Updates** — nothing here sits inside the checkout, so `hermes update` cannot overwrite it.
  If an update ever moves the bundled plugin, re-check with `./ht doctor`.

## Layout

- `ht` — launcher (`up`, `down`, `status`, `log`, `doctor`)
- `hermes_telegram.py` — helper: `setup`, `status`, `doctor`, `send`
- `home/` — the bot's own HERMES_HOME
  - `plugins/platforms/telegram/` — **the editable adapter copy**
  - `config.yaml` — its own config (telegram enabled, model settings)
  - `.env` — its own bot token and allowlist (mode 600)
  - `auth.json` → owner's (shared login)
  - `installs/` → owner's (shared venv)
  - `tools/` → owner's
  - `skills/` → owner's
  - `state.db` — its own sessions and history
- `backup/` — the original vendored export, before it was replaced

## Commands

The bot registers the real Hermes command menu (60 visible, 80 more behind
`/commands`). Everything the prod bot answers, this answers.

## Access control

`TELEGRAM_ALLOWED_USERS` in `home/.env` — only those user ids get a reply.
Currently `8038953791`.