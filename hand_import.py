"""
Parse raw PokerStars/PT4 hand-history text for the admin "Import Hands" feature.

Pure text parsing only (no DB) — auth_db.import_hands() orchestrates the DB side
(stream lookup/creation, duplicate detection, header rewrite, insertion). The
text shape mirrors what src/export/pt4_formatter.py produces:

    PokerStars Hand #<id>:  Hold'em No Limit ($50/$100 USD) - 2026/06/01 00:38:49 ET
    Table '<name>' 9-max Seat #1 is the button
    ...
    <name> is one of: a full YouTube URL, "HCL_38m49s-40m00s <videoId>",
    "Livestream <videoId>", or "Livestream".
"""

import re

from auth_db import extract_video_id  # reuse the canonical video-id extractor


class HandParseError(ValueError):
    """A block of text that doesn't look like a parseable hand."""


# Voluntary in-game actions (blind/ante/straddle "posts ..." lines are excluded).
_ACTION_RE = re.compile(r":\s+(folds|checks|calls|bets|raises)\b")
# A board street line — capture every [...] group so we can take the last (new) one.
_STREET_RE = re.compile(r"^\*\*\*[^\n]*\b(?:FLOP|TURN|RIVER)\b[^\n]*\*\*\*(.*)$")
_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_HEADER_RE = re.compile(r"^PokerStars Hand #(\S+?):", re.MULTILINE)
_TABLE_RE = re.compile(r"^Table '([^']*)'", re.MULTILINE)
# The date/time tail of the header: "- 2026/06/01 00:38:49 ET".
_HEADER_DT_RE = re.compile(r"(-\s+)(\d{4}/\d{2}/\d{2})(\s+)(\d{2}:\d{2}:\d{2})(\s+ET)")
_T_PARAM_RE = re.compile(r"[?&](?:t|start)=(\d+)")
_HCL_TS_RE = re.compile(r"HCL_(\d+)m(\d+)s")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def split_hands(text):
    """Split a blob into individual hand blocks. Tolerates any number of blank
    lines between hands (the exporter uses two) and also pasted hands with no
    blank-line separators, by breaking before each 'PokerStars Hand #' header."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    out = []
    for block in re.split(r"\n\s*\n+", text):
        block = block.strip()
        if not block:
            continue
        # A block may still hold several headers if hands were pasted back-to-back.
        for sub in re.split(r"\n(?=PokerStars Hand #)", block):
            sub = sub.strip()
            if sub:
                out.append(sub)
    return out


def secs_to_hms(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _count_cards(text):
    """Billable cards = hole cards (each 'Dealt to ... [..]') + board cards (the
    new card(s) on each FLOP/TURN/RIVER line, including multi-run boards)."""
    n = 0
    for line in text.split("\n"):
        if line.startswith("Dealt to "):
            m = _BRACKET_RE.search(line)
            if m:
                n += len(m.group(1).split())
            continue
        sm = _STREET_RE.match(line)
        if sm:
            groups = _BRACKET_RE.findall(sm.group(1))
            if groups:
                n += len(groups[-1].split())  # only the newly-dealt card(s)
    return n


def _count_actions(text):
    return len(_ACTION_RE.findall(text))


def _video_id_from(name, full_text):
    """Video id from the table name (URL or trailing bare id), else the body."""
    vid = extract_video_id(name)
    if vid:
        return vid
    # "HCL_… <id>" / "Livestream <id>" — the id is the last whitespace token.
    last = name.split()[-1] if name.split() else ""
    if _BARE_ID_RE.match(last):
        return last
    return extract_video_id(full_text)


def parse_hand(block):
    """Parse one hand block into a dict. Raises HandParseError if it's malformed.

    Returns: hand_number, table_name, video_id, youtube_url, timestamp_seconds,
    has_timestamp, cards_count, actions_count, text (the original block)."""
    hm = _HEADER_RE.search(block)
    if not hm:
        raise HandParseError("missing 'PokerStars Hand #' header")
    tm = _TABLE_RE.search(block)
    if not tm:
        raise HandParseError("missing Table line")

    hand_number = hm.group(1)
    table_name = tm.group(1).strip()
    video_id = _video_id_from(table_name, block)

    # Timestamp: prefer a ?t= / &start= param, then an HCL_<m>m<s>s table name.
    ts, has_ts = 0, False
    pm = _T_PARAM_RE.search(table_name) or _T_PARAM_RE.search(block)
    if pm:
        ts, has_ts = int(pm.group(1)), True
    else:
        hclm = _HCL_TS_RE.search(table_name)
        if hclm:
            ts, has_ts = int(hclm.group(1)) * 60 + int(hclm.group(2)), True

    if table_name.lower().startswith("http"):
        youtube_url = table_name
    elif video_id:
        youtube_url = f"https://youtu.be/{video_id}" + (f"?t={ts}" if has_ts else "")
    else:
        youtube_url = ""

    return {
        "hand_number": hand_number,
        "table_name": table_name,
        "video_id": video_id,
        "youtube_url": youtube_url,
        "timestamp_seconds": ts,
        "has_timestamp": has_ts,
        "cards_count": _count_cards(block),
        "actions_count": _count_actions(block),
        "text": block,
    }


def rewrite_header(text, new_date=None, new_time=None):
    """Rewrite the header's date and/or time. new_date is 'YYYY-MM-DD' or
    'YYYY/MM/DD'; new_time is 'HH:MM:SS'. A None component is left untouched.
    Returns the text unchanged if neither is given or the header isn't found."""
    if not new_date and not new_time:
        return text
    date_slash = (new_date or "").replace("-", "/") if new_date else None

    def _sub(m):
        date = date_slash if date_slash else m.group(2)
        time = new_time if new_time else m.group(4)
        return f"{m.group(1)}{date}{m.group(3)}{time}{m.group(5)}"

    return _HEADER_DT_RE.sub(_sub, text, count=1)
