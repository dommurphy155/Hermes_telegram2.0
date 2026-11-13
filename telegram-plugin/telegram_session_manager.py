"""Session browser model for the interactive Telegram session manager.

Pure logic: no Telegram imports, no adapter imports, no I/O. Everything here is
unit-testable in isolation; the adapter supplies rows and renders the result.

Design rule that matters: callback data carries a *visible index*, never a session
id. The index resolves against a server-side snapshot (index_map) so a stale button
tap can never resume the wrong session.
"""

PAGE_SIZE = 5

# Callback prefixes (kept short: Telegram caps callback_data at 64 bytes).
CB_LIST = "sm:l"
CB_OPEN = "sm:o"
CB_RESUME = "sm:r"
CB_RENAME = "sm:n"
CB_SUMMARY = "sm:s"
CB_BACK = "sm:b"
CB_SORT = "sm:t"
CB_SEARCH = "sm:q"
CB_EXIT = "sm:x"
CB_DELETE = "sm:d"

# Sort modes, cycled by the Sort button.
SORT_RECENT = "recent"
SORT_NEWEST = "newest"
SORT_ALPHA = "alpha"
SORT_MODES = (SORT_RECENT, SORT_NEWEST, SORT_ALPHA)

SORT_LABELS = {
    SORT_RECENT: "🕘 Recent",
    SORT_NEWEST: "🆕 Newest",
    SORT_ALPHA: "🔤 A–Z",
}

_LABEL_MAX = 28


def page_count(total: int) -> int:
    """Number of pages for ``total`` rows (0 rows → 1 page, so the header never divides by zero)."""
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def clamp_page(page: int, total: int) -> int:
    """Keep ``page`` inside ``[0, page_count-1]`` (a list that shrank under a stale tap)."""
    last = page_count(total) - 1
    if page < 0:
        return 0
    return page if page <= last else last


def page_slice(rows: list, page: int) -> list:
    """The rows visible on ``page`` (0-based)."""
    start = clamp_page(page, len(rows)) * PAGE_SIZE
    return rows[start:start + PAGE_SIZE]


def page_buttons(page: int, pages: int) -> list:
    """Pagination row per spec: first page ➡️ only, middle ⬅️➡️, final ⬅️ only, single page none."""
    if pages <= 1:
        return []
    page = max(0, min(page, pages - 1))
    buttons = []
    if page > 0:
        buttons.append(("⬅️", f"{CB_LIST}:{page - 1}"))
    if page < pages - 1:
        buttons.append(("➡️", f"{CB_LIST}:{page + 1}"))
    return buttons


def index_map(rows: list) -> dict:
    """``{visible_index: session_id}`` — the stable-ID authority for callback taps.

    Indices are absolute (0-based over the whole filtered list), so they survive
    page navigation and match the buttons rendered on every page.
    """
    return {i: str(r.get("id") or "") for i, r in enumerate(rows) if r.get("id")}


def resolve_index(rows: list, index_map_: dict, raw: str):
    """``(index, session_id)`` for a callback payload; ``None`` when stale/invalid."""
    try:
        idx = int(raw)
    except (TypeError, ValueError):
        return None
    sid = index_map_.get(idx)
    if not sid:
        return None
    if idx < 0 or idx >= len(rows):
        return None
    return idx, sid


def has_content(row: dict) -> bool:
    """True when a session holds at least one message.

    ``/new`` creates a row immediately, so an untouched session is a shell with
    ``message_count=0``, no title, no preview and no activity description. Those are
    not chats and must never be listed — otherwise spamming ``/new`` floods the browser.
    """
    try:
        if int(row.get("message_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return any(str(row.get(field) or "").strip()
               for field in ("title", "preview", "last_activity_description"))


def label_for(row: dict) -> str:
    """Button label: title, 📌-prefixed when pinned, truncated to the button budget."""
    title = str(row.get("title") or "").strip()
    if not title:
        title = str(row.get("preview") or "").strip() or "(untitled)"
    title = " ".join(title.split())
    if len(title) > _LABEL_MAX:
        title = title[:_LABEL_MAX - 1].rstrip() + "…"
    return f"📌 {title}" if row.get("pinned") else title


def sort_rows(rows: list, mode: str) -> list:
    """Stable sort by ``mode``; unknown mode falls back to recent (the caller's order)."""
    if mode == SORT_ALPHA:
        return sorted(rows, key=lambda r: str(r.get("title") or r.get("preview") or "").lower())
    if mode == SORT_NEWEST:
        return sorted(rows, key=lambda r: str(r.get("created_at") or r.get("started_at") or ""), reverse=True)
    return list(rows)


def header_text(page: int, pages: int, total: int, sort_mode: str, query: str = "") -> str:
    """List header: title, result count, page indicator, active sort.

    Plain text: session titles and search terms routinely contain characters MarkdownV2
    reserves (``.``, ``-``, ``(``) and one unescaped char makes Telegram refuse the edit.
    """
    lines = [f"🗂 Sessions"]
    scope = f" — filter: {query}" if query else ""
    lines.append(f"{total} session{'s' if total != 1 else ''}{scope} · {SORT_LABELS.get(sort_mode, sort_mode)}")
    if pages > 1:
        lines.append(f"Page {page + 1}/{pages}")
    return "\n".join(lines)


def details_text(row: dict, summary: str = None) -> str:
    """Session details body: name, full id, and either the preview or the generated summary.

    With ``summary`` the preview is REPLACED (never appended) and the view stays plain text —
    summaries routinely contain characters MarkdownV2 reserves (``.``, ``-``, ``(``), and an
    unescaped one makes Telegram reject the whole edit.
    """
    name = str(row.get("title") or "").strip() or "(untitled)"
    sid = str(row.get("id") or "")
    lines = [f"📋 {name}", "", f"{sid}"]
    if summary:
        lines += ["", "📝 Summary:", str(summary).strip()]
    else:
        preview = str(row.get("preview") or "").strip()
        if preview:
            lines += ["", preview[:120]]
    return "\n".join(lines)