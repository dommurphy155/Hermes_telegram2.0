#!/usr/bin/env python3
"""D M's Telegram bot — thin client of an existing Hermes installation.

    ./hermes_telegram.py setup     ask for bot token + chat id, save them
    ./hermes_telegram.py run       start the bot (long-poll; replies come from the Hermes agent)
    ./hermes_telegram.py send "hi" one-shot Bot API send to the configured chat
    ./hermes_telegram.py status    config + connectivity
    ./hermes_telegram.py doctor    verify the shared-install wiring

Env it writes/reads (HERMES_TELEGRAM_ENV, default ~/.hermes_telegram.env):
    TELEGRAM_BOT_TOKEN, TELEGRAM_HOME_CHANNEL, TELEGRAM_ALLOWED_USERS

No Hermes code is vendored here. The bot runs on the system python3 (stdlib only) and
uses the installed Hermes as its AI engine via the `hermes` CLI, with HERMES_HOME
pointed at this project's own `home/` so its sessions, state and history stay separate
from the owner's. `hermes update` cannot reach this directory, and this directory
cannot reach the owner's data.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The Hermes checkout that owns the shared install (read-only reference; never written to).
HERMES_ROOT = Path(os.environ.get("HERMES_ROOT") or Path.home() / ".hermes" / "hermes-agent")

# The bot's own data home (separate sessions/state/history from the owner's ~/.hermes).
BOT_HOME = Path(os.environ.get("HERMES_TELEGRAM_HOME") or HERE / "home")
# The installed Hermes CLI shim that owns the shared checkout.
HERMES_CLI = Path(os.environ.get("HERMES_CLI") or Path.home() / ".local" / "bin" / "hermes")
ENV_PATH = Path(os.environ.get("HERMES_TELEGRAM_ENV") or BOT_HOME / ".env")

BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
# Chat ids are numeric (user/group/channel), optionally -100… prefixed for supergroups.
CHAT_ID_RE = re.compile(r"^-?\d+$")


# ── env file ────────────────────────────────────────────────────────────────

def read_env(path: Path = ENV_PATH) -> dict:
    out = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] == '"':
            v = v[1:-1]
        elif len(v) >= 2 and v[0] == v[-1] == "'":
            v = v[1:-1].replace("'\\''", "'")
        out[k.strip()] = v
    return out


def write_env(values: dict, path: Path = ENV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_env(path)
    existing.update(values)
    lines = [f"{k}={_quote(v)}" for k, v in sorted(existing.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)  # holds a bot token


def _quote(v: str) -> str:
    """Bare when the value is a plain token; single-quoted otherwise (safer than escaping
    embedded double quotes inside double quotes, which the reader cannot unescape)."""
    if re.fullmatch(r"[A-Za-z0-9_.:/@+-]*", v or ""):
        return v
    return "'" + v.replace("'", "'\\''") + "'"


# ── transport ───────────────────────────────────────────────────────────────

def _multipart_post(url: str, fields: dict, timeout: float):
    import urllib.request
    import uuid

    boundary = uuid.uuid4().hex
    files = fields.pop("_files", {}) or {}
    parts = []
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, fh in files.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{Path(fh.name).name}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + fh.read() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(url, data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _post_json(url: str, data: dict | None = None, files: bool = False, timeout: float = 60):
    """One HTTP call over stdlib urllib. Multipart when files are attached."""
    import urllib.parse
    import urllib.request

    if files:
        return _multipart_post(url, data, timeout)
    body = urllib.parse.urlencode({k: v for k, v in (data or {}).items() if v is not None}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _api(token: str, method: str, **params):
    return _post_json(f"https://api.telegram.org/bot{token}/{method}", params)


def get_updates(token: str, offset=None, timeout_s: int = 50):
    body = _api(token, "getUpdates", offset=offset, timeout=timeout_s,
                allowed_updates=json.dumps(["message"]))
    if not body.get("ok"):
        raise RuntimeError(body.get("description", "getUpdates failed"))
    return body.get("result", [])


def api_ok(token: str) -> tuple[bool, str]:
    """True + bot username when the token is live."""
    try:
        body = _api(token, "getMe")
    except Exception as e:  # noqa: BLE001 - surfaced verbatim to the user
        return False, f"{type(e).__name__}: {e}"
    if not body.get("ok"):
        return False, str(body.get("description", "rejected"))
    me = body.get("result", {})
    return True, str(me.get("username", ""))


# ── outbound formatting ─────────────────────────────────────────────────────

def render_outbound(text: str, media: list[Path]) -> tuple[str, list[Path]]:
    """TEXT -> caption (media bubble) when it fits Telegram's 1024-char caption limit, else text."""
    text = (text or "").strip()
    captionable = [p for p in media if p.suffix.lower() in
                   {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".pdf", ".txt", ".md"}]
    if len(captionable) == 1 and text and len(text) <= 1024:
        return text, [captionable[0]]
    return "", media


def send_message(token: str, chat_id: str, text: str, media: list[Path] | None = None) -> dict:
    """Send text (+ optional photos) to a chat. Returns the raw Bot API body."""
    media = [Path(p) for p in (media or []) if Path(p).exists()]
    if media:
        import mimetypes

        caption, media = render_outbound(text, media)
        if len(media) > 1:
            raise NotImplementedError("album sends need sendMediaGroup; pass one file")
        m = media[0]
        is_photo = (mimetypes.guess_type(m.name)[0] or "").startswith("image")
        with open(m, "rb") as fh:
            fields = {"chat_id": chat_id, "caption": caption or None,
                      "_files": {"photo" if is_photo else "document": fh}}
            return _post_json(f"https://api.telegram.org/bot{token}/"
                              f"{'sendPhoto' if is_photo else 'sendDocument'}", fields,
                              files=True, timeout=120)
    return _api(token, "sendMessage", chat_id=chat_id, text=text or "(no text)")


# ── agent bridge (thin client of the shared Hermes install) ──────────────────
#
# The bot ships no Hermes code. Inbound turns go through the `hermes` CLI shim
# (which `hermes update` maintains) with HERMES_HOME pointed at the bot's own
# home, so the bot gets its own sessions/state while using the install's engine,
# tools and skills. Outbound sends go back through the Bot API directly.


def build_brain(env: dict):
    """Return an async callable (text, chat_id) -> str, or None for echo mode."""
    mode = (env.get("HERMES_TELEGRAM_BRAIN") or "agent").lower()

    if mode == "none":
        async def echo(text: str, chat_id: str) -> str:
            return f"echo: {text}"
        return echo

    if mode == "cmd":
        cmd = env.get("HERMES_TELEGRAM_BRAIN_CMD")
        if not cmd:
            return None

        async def run_cmd(text: str, chat_id: str) -> str:
            import shlex
            import subprocess
            p = await asyncio.create_subprocess_exec(
                *shlex.split(cmd), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await p.communicate((text + "\n").encode())
            return (out or err).decode(errors="replace").strip() or "(empty)"
        return run_cmd

    # agent mode: one-shot turn through the installed Hermes CLI.
    if not HERMES_CLI.exists():
        print(f"[brain] no hermes CLI at {HERMES_CLI}; falling back to echo", file=sys.stderr)
        return build_brain({**env, "HERMES_TELEGRAM_BRAIN": "none"})

    async def agent_reply(text: str, chat_id: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            str(HERMES_CLI), "chat", "-q", text, "--oneshot",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HERMES_HOME": str(BOT_HOME)})
        out, err = await proc.communicate()
        reply = _clean_agent_output(out.decode(errors="replace"))
        if not reply:
            detail = err.decode(errors="replace").strip()[:300]
            raise RuntimeError(f"agent returned nothing: {detail or 'no stderr'}")
        return reply

    return agent_reply


def _clean_agent_output(raw: str) -> str:
    """Strip the `☤ Hermes` banner block `hermes chat -q ... --oneshot` prints around the reply.

    Layout:  Query: …
             ──────────────
             ☤ Hermes
             <reply lines>
             ──────────────
             Resume this session with: …
             Session: …
    """
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return ""
    # drop everything before the reply block, keep only what follows the ☤ Hermes banner
    if "☤ Hermes" in text:
        text = text.split("☤ Hermes", 1)[1].strip()
        # the reply sits between the two ─── rules
        parts = re.split(r"─{5,}", text)
        text = parts[0] if len(parts) >= 2 else (parts[1] if parts else text)
    text = re.split(r"\nResume this session with:|\nSession:\s|\nDuration:\s|\nMessages:\s", text)[0]
    return text.strip()


# ── setup wizard ────────────────────────────────────────────────────────────

def setup_wizard() -> int:
    env = read_env()
    print("\nHermes Telegram setup")
    print("─" * 40)
    have_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if have_token and api_ok(have_token)[0]:
        print(f"Bot token already saved and live (@{api_ok(have_token)[1]}).")
        if input("Replace it? [y/N]: ").strip().lower() != "y":
            token = have_token
        else:
            token = _prompt_token()
    else:
        if have_token:
            print("Saved token is rejected by Telegram — re-enter it.")
        token = _prompt_token()
    if not token:
        return 1
    write_env({"TELEGRAM_BOT_TOKEN": token})

    ok, info = api_ok(token)
    if not ok:
        print(f"\nTelegram rejected this token: {info}")
        return 1
    print(f"Token verified — bot @{info}.")

    print("\nChat id = where messages are delivered.")
    print("  • DM with the bot: your numeric user id (message @userinfobot)")
    print("  • group: the group id (negative, add the bot and send a message)")
    print("  • channel: the channel id (e.g. -1001234567890)")
    try:
        chat_id = input("Chat id [%s]: " % env.get("TELEGRAM_HOME_CHANNEL", "")).strip() \
            or env.get("TELEGRAM_HOME_CHANNEL", "")
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    if not chat_id or not CHAT_ID_RE.match(chat_id):
        print("That doesn't look like a numeric chat id.")
        return 1

    body = send_message(token, chat_id, f"✅ Hermes Telegram connected as @{info}.")
    if not body.get("ok"):
        print(f"Test message failed: {body.get('description')}")
        return 1
    write_env({"TELEGRAM_HOME_CHANNEL": chat_id})
    print(f"Test message sent to {chat_id}. Env saved to {ENV_PATH} (mode 600).")
    return 0


def _prompt_token() -> str:
    try:
        while True:
            token = input("Bot token from @BotFather: ").strip()
            if not token:
                return ""
            if not BOT_TOKEN_RE.match(token):
                print("  Expected <digits>:<hash> (e.g. 123456789:AA…). Try again.")
                continue
            ok, info = api_ok(token)
            if not ok:
                print(f"  Telegram says no: {info}")
                return ""
            return token
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ── run loop ────────────────────────────────────────────────────────────────

async def run_forever(env: dict) -> int:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("No TELEGRAM_BOT_TOKEN — run `hermes_telegram.py setup` first.", file=sys.stderr)
        return 1
    ok, info = api_ok(token)
    if not ok:
        print(f"Telegram rejected the token: {info}", file=sys.stderr)
        return 1

    brain = build_brain(env)
    if brain is None:
        print("No brain configured (HERMES_TELEGRAM_BRAIN_CMD empty).", file=sys.stderr)
        return 1

    home = env.get("TELEGRAM_HOME_CHANNEL", "")
    brain_mode = (env.get("HERMES_TELEGRAM_BRAIN") or "agent").lower()
    # Allowlist: only these users/chats get answered. Empty = answer anyone.
    allowed = {a.strip() for a in (env.get("TELEGRAM_ALLOWED_USERS") or "").split(",") if a.strip()}
    offset = None
    print(f"Bot @{info} online"
          + (f" → home channel {home}" if home else "")
          + (f" · locked to {', '.join(sorted(allowed))}" if allowed else " · answering anyone")
          + ". Ctrl-C to stop.", flush=True)
    consecutive_errors = 0
    while True:
        try:
            updates = await asyncio.to_thread(get_updates, token, offset, 50)
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            delay = min(60, 2 ** min(consecutive_errors, 5))
            print(f"getUpdates failed ({e}); retrying in {delay}s", file=sys.stderr)
            await asyncio.sleep(delay)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message")
            if not msg:
                continue
            text = msg.get("text") or msg.get("caption") or ""
            if not text:
                continue
            src = str(msg["chat"]["id"])
            sender = str(msg.get("from", {}).get("id") or src)
            who = msg.get("from", {}).get("first_name", "there")
            if allowed and sender not in allowed and src not in allowed:
                print(f"[{time.strftime('%H:%M:%S')}] ignored {who} ({sender}) — not allowed", flush=True)
                continue
            print(f"[{time.strftime('%H:%M:%S')}] {who} ({src}): {text[:80]}", flush=True)
            try:
                reply = await brain(text, src)
                # Reply to the chat the message came from, never the home channel —
                # otherwise two bots sharing one inbox interleave in the wrong thread.
                await asyncio.to_thread(send_message, token, src, reply, None)
                print(f"  ← reply ({len(reply)} chars) sent to {src} via brain", flush=True)
            except Exception as e:  # noqa: BLE001
                detail = str(e)
                print(f"  ! brain failed: {detail[:300]}", flush=True)
                try:
                    await asyncio.to_thread(
                        send_message, token, src,
                        f"⚠️ The agent is unavailable right now.\n\n{detail[:300]}", None)
                except Exception as e2:  # noqa: BLE001
                    print(f"  ! fallback send failed: {e2}", flush=True)


def run_foreground(env: dict) -> int:
    try:
        asyncio.run(run_forever(env))
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


# ── status / doctor ─────────────────────────────────────────────────────────

def cmd_status(env: dict) -> int:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    print(f"env file:     {ENV_PATH}")
    print(f"bot home:     {BOT_HOME}")
    print(f"hermes CLI:   {HERMES_CLI} ({'found' if HERMES_CLI.exists() else 'MISSING'})")
    print(f"brain:        {(env.get('HERMES_TELEGRAM_BRAIN') or 'agent')}")
    print(f"home channel: {env.get('TELEGRAM_HOME_CHANNEL', '(none)')}")
    print(f"allowed:      {env.get('TELEGRAM_ALLOWED_USERS') or '(anyone)'}")
    if not token:
        print("\nNo token — run ./ht setup")
        return 1
    ok, info = api_ok(token)
    print(f"\nTelegram:     {'OK as @' + info if ok else 'FAILED: ' + info}")
    return 0 if ok else 1


def _override_winner():
    """Path of the plugin that actually wins the `platforms/telegram` key, or None.

    Asks Hermes' own discovery (via the owner's venv, which has the deps) rather than
    guessing — a same-named copy under the home's plugins/ shadows the bundled one.
    """
    import subprocess
    code = (
        "import sys, logging; sys.path.insert(0, %r); logging.basicConfig(level=logging.CRITICAL)\n"
        "import hermes_bootstrap\n"
        "from hermes_cli.plugins_discovery import collect_directory_manifests, resolve_manifest_winners\n"
        "w = resolve_manifest_winners(collect_directory_manifests())\n"
        "m = w.get('platforms/telegram')\n"
        "print(m.path if m else '')\n" % str(HERMES_ROOT)
    )
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=120, env={**os.environ, "HERMES_HOME": str(BOT_HOME)})
        out = (p.stdout or "").strip().splitlines()
        return Path(out[-1]) if out and out[-1] else None
    except Exception:  # noqa: BLE001
        return None


def cmd_doctor(env: dict) -> int:
    """Check the seams this design depends on: CLI, venv borrow, separate state, owner intact."""
    ok = True

    def check(label: str, good: bool, detail: str = ""):
        nonlocal ok
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {label}" + (f" — {detail}" if detail else ""))

    print("shared install wiring:")
    check("hermes CLI exists", HERMES_CLI.exists(), str(HERMES_CLI))
    check("bot home exists", BOT_HOME.is_dir(), str(BOT_HOME))

    inst = BOT_HOME / "installs"
    check("installs is a symlink to owner's venv", inst.is_symlink(),
          f"→ {os.readlink(inst)}" if inst.is_symlink() else "not a symlink (would duplicate deps)")
    check("tools symlink present", (BOT_HOME / "tools").is_symlink())
    check("auth.json symlinked (shared login)", (BOT_HOME / "auth.json").is_symlink())
    check("skills visible", (BOT_HOME / "skills").exists())
    check("config.yaml present", (BOT_HOME / "config.yaml").is_file())
    check("separate state.db", (BOT_HOME / "state.db").is_file(),
          "bot has its own" if (BOT_HOME / "state.db").is_file() else "")
    check("owner state.db separate", (Path.home() / ".hermes" / "state.db").is_file())

    # The editable adapter copy: it must exist and must WIN over the bundled one.
    override = BOT_HOME / "plugins" / "platforms" / "telegram" / "adapter.py"
    check("editable adapter copy present", override.is_file(), str(override))
    winner = _override_winner()
    check("override shadows the bundled adapter",
          winner is not None and str(winner).startswith(str(BOT_HOME)),
          str(winner) if winner else "could not determine")

    print("\nagent engine:")
    try:
        import subprocess
        p = subprocess.run([str(HERMES_CLI), "--version"], capture_output=True, text=True,
                           timeout=60, env={**os.environ, "HERMES_HOME": str(BOT_HOME)})
        ver = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else "?"
        check("hermes runs", p.returncode == 0, ver[:60])
    except Exception as e:  # noqa: BLE001
        check("hermes runs", False, str(e)[:80])

    print("\n" + ("all good." if ok else "problems above."))
    return 0 if ok else 1


# ── install: wire a fresh clone into an existing Hermes install ─────────────

def _link(name: str, target: Path, force: bool = False) -> tuple[bool, str]:
    """Point BOT_HOME/name at target. Creates parents, never copies the target's data."""
    dest = BOT_HOME / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        if os.path.realpath(dest) == os.path.realpath(target):
            return True, "already linked"
        if not force:
            return False, f"exists as a symlink to {os.readlink(dest)} (use --force)"
        dest.unlink()
    elif dest.exists():
        return False, f"exists as a real file/dir (move it aside, or use --force)"
    dest.symlink_to(target)
    return True, f"-> {target}"


def install_shared(force: bool = False) -> int:
    """Shared-database mode: the bot runs *as* the owner's Hermes home.

    Hermes requires the session DB's resolved parent to equal HERMES_HOME
    (gateway/run_runtime.py), so a separate home can never read the owner's
    chats — symlinking state.db alone also gives each gateway its own -wal.
    The only supported shape is one home, one DB, one gateway: the editable
    adapter is installed as a *user plugin* inside the owner's home, and the
    bot is started with that home.

    `hermes update` rewrites the checkout, not ~/.hermes/plugins, so the
    override survives updates.
    """
    print("\nHermes Telegram — install (shared database)")
    print("─" * 44)

    if not HERMES_CLI.exists():
        print(f"✗ hermes CLI not found at {HERMES_CLI}")
        print("  Install Hermes first, or set HERMES_CLI=/path/to/hermes")
        return 1
    try:
        import subprocess
        p = subprocess.run([str(HERMES_CLI), "--version"], capture_output=True, text=True, timeout=60)
        ver = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else "?"
        print(f"✓ hermes CLI: {ver[:60]}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ cannot run hermes: {exc}")
        return 1

    owner_home = Path.home() / ".hermes"
    if not (owner_home / "auth.json").exists():
        print(f"✗ {owner_home}/auth.json missing — run `hermes` once and log in first.")
        return 1
    print(f"✓ owner install: {owner_home}")

    ok = True

    # 1. the editable adapter, as a user plugin inside the owner's home
    src = HERE / "telegram-plugin"
    dst = owner_home / "plugins" / "platforms" / "telegram"
    if not src.is_dir():
        print(f"✗ missing override source: {src}")
        return 1
    if dst.exists():
        if not force:
            print(f"✓ adapter present: {dst} (use --force to overwrite)")
        else:
            shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
            print(f"✓ adapter reinstalled: {dst}")
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        print(f"✓ adapter installed: {dst}")

    # 2. enable it there, so the user copy wins over the bundled plugin
    try:
        import subprocess
        p = subprocess.run([str(HERMES_CLI), "plugins", "enable", "telegram-platform"],
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "HERMES_HOME": str(owner_home)})
        good = p.returncode == 0
        print(f"{'✓' if good else '✗'} plugin override enabled"
              + ("" if good else f": {(p.stdout or p.stderr).strip()[:100]}"))
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"✗ plugin enable failed: {exc}")
        ok = False

    # 3. token: the owner's .env is what this home reads
    owner_env = owner_home / ".env"
    if owner_env.exists() and "TELEGRAM_BOT_TOKEN=" in owner_env.read_text(encoding="utf-8", errors="replace"):
        print("✓ owner .env has TELEGRAM_BOT_TOKEN")
    else:
        print(f"✗ {owner_env} has no TELEGRAM_BOT_TOKEN — add it, or run ./ht setup")

    # 4. drop the separate home so nothing shadows the owner's
    if BOT_HOME.exists() and BOT_HOME.is_dir() and not BOT_HOME.is_symlink():
        moved = BOT_HOME.with_name(BOT_HOME.name + "_isolated_" + time.strftime("%Y%m%d_%H%M%S"))
        try:
            BOT_HOME.rename(moved)
            print(f"✓ separate home moved aside: {moved.name}")
        except OSError as exc:
            print(f"! could not move separate home aside: {exc}")

    print()
    if not ok:
        print("Some steps failed — fix the ✗ lines above and re-run with --force.")
        return 1
    print(f"The bot now runs with HERMES_HOME={owner_home}")
    print("Start it with:  HERMES_HOME=%s ./ht up" % owner_home)
    print("Restart after editing the adapter: kill -USR1 the gateway pid")
    return 0


def install_wizard(force: bool = False) -> int:
    """Wire this checkout into the Hermes install it should borrow from.

    Everything borrowed is a symlink; only config.yaml and .env are real files here.
    Nothing outside this directory is written to, so `hermes update` cannot touch it.
    """
    print("\nHermes Telegram — install")
    print("─" * 44)

    if not HERMES_CLI.exists():
        print(f"✗ hermes CLI not found at {HERMES_CLI}")
        print("  Install Hermes first, or set HERMES_CLI=/path/to/hermes")
        return 1
    try:
        import subprocess
        p = subprocess.run([str(HERMES_CLI), "--version"], capture_output=True, text=True, timeout=60)
        ver = (p.stdout or p.stderr).strip().splitlines()[0] if (p.stdout or p.stderr).strip() else "?"
        print(f"✓ hermes CLI: {ver[:60]}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ cannot run hermes: {exc}")
        return 1

    # The owner's home is whatever the CLI resolves to — that's the install we borrow from.
    owner_home = Path.home() / ".hermes"
    if not (owner_home / "auth.json").exists():
        print(f"✗ {owner_home}/auth.json missing — run `hermes` once and log in first.")
        return 1
    print(f"✓ owner install: {owner_home}")

    ok = True
    for name, target in (
        ("installs", owner_home / "installs"),   # shared venv, ~430 MB
        ("tools", owner_home / "tools"),         # shared tool binaries
        ("skills", owner_home / "skills"),       # shared skill library
        ("auth.json", owner_home / "auth.json"), # one credential, no copy
    ):
        good, why = _link(name, target, force=force)
        print(f"{'✓' if good else '✗'} {name}: {why}")
        ok = ok and good

    # config.yaml: seed from the owner's, so telegram/model settings come along.
    cfg = BOT_HOME / "config.yaml"
    if not cfg.exists():
        src = owner_home / "config.yaml"
        if src.exists():
            cfg.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            os.chmod(cfg, 0o600)
            print("✓ config.yaml: seeded from the owner install")
        else:
            print("✗ config.yaml: owner has none; copy one in by hand")
            ok = False
    else:
        print("✓ config.yaml: present")

    env = ENV_PATH
    if not env.exists():
        env.write_text(
            "# Copy of env.example. Fill these in, then run `./ht setup`.\n"
            "TELEGRAM_BOT_TOKEN=\n"
            "TELEGRAM_ALLOWED_USERS=\n"
            "TELEGRAM_HOME_CHANNEL=\n", encoding="utf-8")
        os.chmod(env, 0o600)
        print(f"✓ {env.name}: created (mode 600) — fill it in next")
    else:
        print(f"✓ {env.name}: present")

    # The override only wins once it is enabled as a user plugin.
    try:
        import subprocess
        p = subprocess.run([str(HERMES_CLI), "plugins", "enable", "telegram-platform"],
                           capture_output=True, text=True, timeout=120,
                           env={**os.environ, "HERMES_HOME": str(BOT_HOME)})
        good = p.returncode == 0
        print(f"{'✓' if good else '✗'} plugin override enabled"
              + ("" if good else f": {(p.stdout or p.stderr).strip()[:100]}"))
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"✗ plugin enable failed: {exc}")
        ok = False

    print()
    if not ok:
        print("Some steps failed — fix the ✗ lines above and re-run with --force.")
        return 1
    token = read_env().get("TELEGRAM_BOT_TOKEN", "")
    print("Next: fill in home/.env, then run ./ht setup"
          if not token else "Next: ./ht up")
    return 0


# ── cli ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "setup"
    env = read_env()
    if cmd == "setup":
        return setup_wizard()
    if cmd == "install":
        if "--shared" in argv:
            return install_shared(force="--force" in argv)
        return install_wizard(force="--force" in argv)
    if cmd == "run":
        return run_foreground(env)
    if cmd == "send":
        if len(argv) < 3:
            print("usage: hermes_telegram.py send <text>", file=sys.stderr)
            return 2
        if not env.get("TELEGRAM_BOT_TOKEN"):
            print("No TELEGRAM_BOT_TOKEN — run setup first.", file=sys.stderr)
            return 1
        body = send_message(env["TELEGRAM_BOT_TOKEN"], env.get("TELEGRAM_HOME_CHANNEL", ""), argv[2])
        print(json.dumps(body, indent=2))
        return 0 if body.get("ok") else 1
    if cmd == "status":
        return cmd_status(env)
    if cmd == "doctor":
        return cmd_doctor(env)
    print(f"unknown command: {cmd}\n"
          "usage: hermes_telegram.py [setup|run|send \"text\"|status|doctor]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))