#!/usr/bin/env python3
"""
Import existing PT4 hand-history .txt files into the SQLite hands table.

Sources:
  - C:\\Users\\Owner\\Downloads\\session_3hands.txt
  - C:\\Users\\Owner\\Downloads\\session_2hands.txt
  - C:\\Users\\Owner\\Downloads\\hand_1.txt
  - C:\\Poker Scaper\\output\\test_hands.txt
  - every other .txt in output/ that contains PT4 hands

Each hand (a block starting with "PokerStars Hand #") is parsed for the YouTube
URL on its Table line, the video id + timestamp from that URL, the number of
distinct cards shown, and the number of voluntary actions. Hands are inserted
under the admin account via auth_db.insert_hand (which normalizes the stream id,
auto-creates the stream row, and prices earnings as cards*$0.03 + actions*$0.03
+ $0.10). Re-running is safe — an identical pt4_text already on the admin's
record is skipped.

Run:  python scripts/import_existing_hands.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import auth_db  # noqa: E402

EXPLICIT_FILES = [
    r"C:\Users\Owner\Downloads\session_3hands.txt",
    r"C:\Users\Owner\Downloads\session_2hands.txt",
    r"C:\Users\Owner\Downloads\hand_1.txt",
    os.path.join(ROOT, "output", "test_hands.txt"),
]

CARD_TOKEN = re.compile(r"\b([2-9TJQKA][shdc])\b")
ACTION_RE = re.compile(r":\s+(folds|checks|calls|bets|raises)\b", re.I)
TABLE_RE = re.compile(r"^Table '([^']*)'", re.M)
TS_RE = re.compile(r"[?&](?:t|start)=(\d+)")


def gather_files():
    """Explicit files + any other .txt in output/, de-duplicated, existing only."""
    files = list(EXPLICIT_FILES)
    out_dir = os.path.join(ROOT, "output")
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            if name.lower().endswith(".txt"):
                files.append(os.path.join(out_dir, name))
    seen, result = set(), []
    for f in files:
        key = os.path.normcase(os.path.abspath(f))
        if key in seen or not os.path.isfile(f):
            continue
        seen.add(key)
        result.append(f)
    return result


def split_hands(text):
    """Split a file into hands on each 'PokerStars Hand #' header (robust to how
    many blank lines separate them)."""
    parts = re.split(r"(?=^PokerStars Hand #)", text, flags=re.M)
    return [p.strip() for p in parts if "PokerStars Hand #" in p]


def parse_hand(block):
    """Extract {url, video_id, timestamp, cards, actions} from one hand block."""
    m = TABLE_RE.search(block)
    table_name = m.group(1).strip() if m else ""
    url = table_name if re.search(r"youtu\.?be|youtube\.com", table_name, re.I) else ""
    video_id = auth_db.extract_video_id(url)
    ts_m = TS_RE.search(url)
    timestamp = int(ts_m.group(1)) if ts_m else 0

    # Distinct cards shown (board + every revealed hole card). Cards only ever
    # appear inside [...] groups, so collect them there and de-duplicate.
    cards = set()
    for group in re.findall(r"\[([^\]]*)\]", block):
        for tok in CARD_TOKEN.findall(group):
            cards.add(tok)

    # Voluntary actions (blind/ante/straddle posts use "posts", not these verbs).
    actions = len(ACTION_RE.findall(block))

    return {"url": url, "video_id": video_id, "timestamp": timestamp,
            "cards": len(cards), "actions": actions}


def main():
    auth_db.init_db()
    conn = auth_db.get_db()
    admin = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        conn.close()
        sys.exit("No admin user found — run the server once to bootstrap it.")
    admin_id = admin["id"]
    existing = {row["pt4_text"] for row in
                conn.execute("SELECT pt4_text FROM hands WHERE user_id = ?", (admin_id,)).fetchall()}
    conn.close()

    files = gather_files()
    print(f"Scanning {len(files)} file(s):")
    for f in files:
        print(f"  - {f}")

    imported = 0
    skipped_dupe = 0
    streams = set()
    no_stream = 0

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError as e:
            print(f"  ! could not read {path}: {e}")
            continue
        for block in split_hands(text):
            if block in existing:
                skipped_dupe += 1
                continue
            info = parse_hand(block)
            # stream_id = the video id; insert_hand re-normalizes + auto-creates
            # the stream row. Hands with no YouTube URL import unlinked.
            stream_key = info["video_id"] or info["url"]
            auth_db.insert_hand(
                admin_id, stream_key, info["url"], info["timestamp"],
                block, info["cards"], info["actions"],
            )
            existing.add(block)
            imported += 1
            if info["video_id"]:
                streams.add(info["video_id"])
            else:
                no_stream += 1

    print()
    print(f"Imported {imported} hands across {len(streams)} streams"
          + (f" (+{no_stream} with no YouTube link)" if no_stream else "")
          + (f"; skipped {skipped_dupe} already-imported" if skipped_dupe else ""))

    # Report the per-stream completed counts (handsCompleted is derived from the
    # hands table, so inserting the hands is what updates it).
    if streams:
        conn = auth_db.get_db()
        print("Stream hand counts:")
        for sid in sorted(streams):
            n = conn.execute("SELECT COUNT(*) AS n FROM hands WHERE stream_id = ?", (sid,)).fetchone()["n"]
            title = (conn.execute("SELECT title FROM streams WHERE id = ?", (sid,)).fetchone() or {})
            tt = title["title"] if title and title["title"] else ""
            print(f"  {sid}: {n} hands  {('· ' + tt[:48]) if tt else ''}")
        conn.close()


if __name__ == "__main__":
    main()
