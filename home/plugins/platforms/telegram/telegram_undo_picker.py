"""Pure logic for the interactive ``/undo`` picker.

The gateway's ``/undo [N]`` takes a count and rewinds blindly. This module turns that into
a choice: list the user's recent messages (5 per page, newest first, truncated), let them
tap the one they want to go back to, and translate that tap into the ``N`` the core
primitive expects.

No Telegram imports, no I/O — the adapter owns rendering and the DB.
"""

PAGE_SIZE = 5

CB_LIST = "un:l"      # pagination within the picker
CB_PICK = "un:p"      # a chosen turn → rewind to just before it
CB_BACK = "un:b"      # back to the list from a detail view
CB_EXIT = "un:x"      # close the picker

# How much of each message to show before ellipsising.
PREVIEW_LIMIT = 60


def truncate(text: str, limit: int = PREVIEW_LIMIT) -> str:
    """One-line preview: collapsed whitespace, cut on a word boundary, single ellipsis."""
    clean = " ".join((text or "").split())
    if not clean:
        return "(empty message)"
    if len(clean) <= limit:
        return clean
    cut = clean[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,.;:") + "…"


def turns_from_messages(messages: list) -> list:
    """``[{ordinal, text, preview}]`` for every user turn, oldest first.

    ``ordinal`` is the 0-based index the core ``rewind_user_turn`` expects, so it is
    assigned over *all* user turns — never over the page slice, or the mapping breaks.
    """
    turns = []
    for msg in messages or []:
        if (msg.get("role") or "") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(p.get("text", "") for p in content
                             if isinstance(p, dict) and p.get("type") == "text")
        else:
            text = ""
        ordinal = len(turns)
        turns.append({"ordinal": ordinal, "text": text, "preview": truncate(text)})
    return turns


def newest_first(turns: list) -> list:
    """The picker lists newest first — that is the end you reach for, not the start."""
    return list(reversed(turns))


def page_count(total: int) -> int:
    if total <= 0:
        return 1
    return (total + PAGE_SIZE - 1) // PAGE_SIZE


def clamp_page(page: int, total: int) -> int:
    last = page_count(total) - 1
    if page < 0:
        return 0
    return page if page <= last else last


def page_slice(rows: list, page: int) -> list:
    """Rows visible on ``page`` (0-based), plus their absolute offset in ``rows``."""
    start = clamp_page(page, len(rows)) * PAGE_SIZE
    return rows[start:start + PAGE_SIZE], start


def turns_to_undo(total_turns: int, ordinal: int) -> int:
    """``/undo N`` value that rewinds to just *before* ``ordinal``.

    ``rewind_user_turn`` counts back from the newest turn, so picking the third of five
    means undoing the last three. Picking the newest turn is a plain ``/undo 1``.
    """
    if total_turns <= 0:
        return 0
    ordinal = max(0, min(ordinal, total_turns - 1))
    return total_turns - ordinal


def nav_buttons(page: int, pages: int) -> list:
    """``[(label, page)]`` — first page ➡️ only, middle ⬅️➡️, last ⬅️ only, single none."""
    if pages <= 1:
        return []
    page = max(0, min(page, pages - 1))
    out = []
    if page > 0:
        out.append(("⬅️", page - 1))
    if page < pages - 1:
        out.append(("➡️", page + 1))
    return out


def header_text(page: int, pages: int, total: int) -> str:
    """Plain text: the picker must never rely on MarkdownV2 escaping."""
    if total <= 0:
        return "↩️ Undo\n\nNo user messages in this session yet."
    where = f"Page {page + 1}/{pages}" if pages > 1 else "all on one page"
    return (f"↩️ Undo — pick a point to go back to\n"
            f"{total} message{'s' if total != 1 else ''} · {where}\n\n"
            f"Newest first. Choosing one undoes everything after it.")


def confirm_text(preview: str, turns: int) -> str:
    """Shown after a tap: how much will be undone, and where the text lands."""
    return (f"↩️ Undo {turns} turn{'s' if turns != 1 else ''}?\n\n"
            f"Back to before: {preview}\n\n"
            f"The removed text goes back to your composer.")