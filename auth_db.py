"""
SQLite-backed accounts, hand histories, streams and payments for the hand builder.

Single file `users.db` next to this module. Stdlib + werkzeug only (werkzeug ships
with Flask), so the deployed app needs no extra dependencies.

Earnings model (mirrors the frontend piece-rate):
  - PIECE_RATE per filled card / per voluntary action
  - COMPLETION_BONUS per completed hand
  - STREAM_BONUS_PER_HAND once every hand in a stream is marked complete
A hand's stored `earnings` is its BASE pay (cards + actions + completion bonus).
Stream bonuses are derived (not stored): $0.05 for each of a user's hands that sits
in a stream flagged complete — which is exactly a proportional split of
(0.05 x hands_in_stream) by hands contributed.
"""

import os
import json
import re
import sqlite3
from datetime import datetime, timezone


# Extract the 11-char YouTube video id from any common URL shape (watch?v=,
# youtu.be/, youtube.com/live/, /embed/, /shorts/) with or without timestamps /
# si / list params. A bare id passes through. This is the key a hand matches a
# stream on, so normalizing here keeps the calendar's handsCompleted correct
# even when the frontend sends a full URL or a /live/ link.
_VID_PATTERNS = [
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
]


def extract_video_id(url):
    if not url:
        return ""
    s = str(url).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    for pat in _VID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    return ""

from werkzeug.security import generate_password_hash, check_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "users.db")
SEED_JSON = os.path.join(HERE, "scripts", "hcl_streams.json")

PIECE_RATE = 0.03
COMPLETION_BONUS = 0.10
STREAM_BONUS_PER_HAND = 0.05


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _hand_earnings(cards, actions):
    return round((cards + actions) * PIECE_RATE + COMPLETION_BONUS, 2)


# ── Schema + seed ────────────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            email         TEXT,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hands (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            stream_id        TEXT,
            youtube_url      TEXT,
            timestamp_seconds INTEGER,
            pt4_text         TEXT,
            cards_count      INTEGER NOT NULL DEFAULT 0,
            actions_count    INTEGER NOT NULL DEFAULT 0,
            earnings         REAL NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS streams (
            id               TEXT PRIMARY KEY,
            youtube_url      TEXT,
            title            TEXT,
            date             TEXT,
            duration_minutes INTEGER,
            hands_estimated  INTEGER,
            is_complete      INTEGER NOT NULL DEFAULT 0,
            completed_at     TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            amount     REAL NOT NULL,
            date       TEXT,
            note       TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_hands_user ON hands(user_id);
        CREATE INDEX IF NOT EXISTS idx_hands_stream ON hands(stream_id);
        CREATE INDEX IF NOT EXISTS idx_pay_user ON payments(user_id);
        """
    )
    conn.commit()

    # Bootstrap the admin account (username "admin", password from env).
    admin = c.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        pw = os.environ.get("ADMIN_PASSWORD", "changeme")
        c.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, created_at) "
            "VALUES (?,?,?,?,?)",
            ("admin", "", generate_password_hash(pw), 1, now_iso()),
        )
        conn.commit()

    # Seed the stream catalog from the committed scrape (once).
    have = c.execute("SELECT COUNT(*) AS n FROM streams").fetchone()["n"]
    if have == 0 and os.path.exists(SEED_JSON):
        try:
            with open(SEED_JSON, encoding="utf-8") as f:
                rows = json.load(f)
            for s in rows:
                c.execute(
                    "INSERT OR IGNORE INTO streams "
                    "(id, youtube_url, title, date, duration_minutes, hands_estimated, is_complete) "
                    "VALUES (?,?,?,?,?,?,0)",
                    (s.get("id"), s.get("youtubeUrl"), s.get("title"), s.get("date"),
                     s.get("durationMinutes"), s.get("handsEstimated")),
                )
            conn.commit()
        except Exception:
            pass  # seeding is best-effort
    conn.close()


# ── Users ────────────────────────────────────────────────────────────────────
def create_user(username, email, password):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required.")
    conn = get_db()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, created_at) "
            "VALUES (?,?,?,0,?)",
            (username, (email or "").strip(), generate_password_hash(password), now_iso()),
        )
        conn.commit()
        return get_user(cur.lastrowid)
    except sqlite3.IntegrityError:
        raise ValueError("That username is already taken.")
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password or ""):
        return dict(row)
    return None


def get_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def public_user(row):
    return {"id": row["id"], "username": row["username"], "email": row["email"],
            "is_admin": bool(row["is_admin"])}


# ── Hands ────────────────────────────────────────────────────────────────────
def insert_hand(user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, cards_count, actions_count):
    cards = int(cards_count or 0)
    actions = int(actions_count or 0)
    # Normalize the stream key to the canonical 11-char video id so a hand always
    # matches the calendar's stream (whose id is the video id) — regardless of
    # whether the client sent a bare id, a full watch URL, or a /live/ link.
    stream_id = extract_video_id(stream_id) or extract_video_id(youtube_url) or (stream_id or "")
    earnings = _hand_earnings(cards, actions)
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO hands (user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, "
        "cards_count, actions_count, earnings, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, stream_id, youtube_url, int(timestamp_seconds or 0), pt4_text or "",
         cards, actions, earnings, now_iso()),
    )
    # Upsert a minimal stream row so the hand always ties to a known stream.
    if stream_id:
        conn.execute(
            "INSERT OR IGNORE INTO streams (id, youtube_url, is_complete) VALUES (?,?,0)",
            (stream_id, youtube_url),
        )
    conn.commit()
    hand_id = cur.lastrowid
    conn.close()
    return hand_id, earnings


# ── Earnings / stats ─────────────────────────────────────────────────────────
def _earnings_breakdown(conn, user_id):
    """Full earnings breakdown for one user from hands + completed streams + payments."""
    agg = conn.execute(
        "SELECT COUNT(*) AS hands, COALESCE(SUM(cards_count),0) AS cards, "
        "COALESCE(SUM(actions_count),0) AS actions, COALESCE(SUM(earnings),0) AS base "
        "FROM hands WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    hands = agg["hands"]
    cards = agg["cards"]
    actions = agg["actions"]

    # Stream bonus = $0.05 per the user's hands that live in a completed stream.
    bonus_hands = conn.execute(
        "SELECT COUNT(*) AS n FROM hands h JOIN streams s ON h.stream_id = s.id "
        "WHERE h.user_id = ? AND s.is_complete = 1",
        (user_id,),
    ).fetchone()["n"]

    paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS p FROM payments WHERE user_id = ?", (user_id,)
    ).fetchone()["p"]

    cards_income = round(cards * PIECE_RATE, 2)
    actions_income = round(actions * PIECE_RATE, 2)
    completion = round(hands * COMPLETION_BONUS, 2)
    stream_bonus = round(bonus_hands * STREAM_BONUS_PER_HAND, 2)
    base = round(cards_income + actions_income + completion, 2)
    total = round(base + stream_bonus, 2)
    return {
        "hands": hands,
        "pieces": cards + actions,
        "cards": cards,
        "actions": actions,
        "cards_income": cards_income,
        "actions_income": actions_income,
        "completion_bonus": completion,
        "stream_bonus": stream_bonus,
        "base": base,
        "total": total,
        "paid": round(paid, 2),
        "owed": round(total - paid, 2),
    }


def user_dashboard(user_id):
    conn = get_db()
    bd = _earnings_breakdown(conn, user_id)
    bd["streams_worked"] = conn.execute(
        "SELECT h.stream_id AS stream_id, COALESCE(s.title, h.stream_id) AS title, "
        "MAX(s.is_complete) AS is_complete, COUNT(*) AS hands "
        "FROM hands h LEFT JOIN streams s ON h.stream_id = s.id "
        "WHERE h.user_id = ? GROUP BY h.stream_id ORDER BY hands DESC",
        (user_id,),
    ).fetchall()
    bd["streams_worked"] = [dict(r) for r in bd["streams_worked"]]
    bd["streams_count"] = len(bd["streams_worked"])
    recent = conn.execute(
        "SELECT id, stream_id, youtube_url, timestamp_seconds, cards_count, actions_count, "
        "earnings, created_at FROM hands WHERE user_id = ? ORDER BY id DESC LIMIT 25",
        (user_id,),
    ).fetchall()
    bd["recent_hands"] = [dict(r) for r in recent]
    conn.close()
    return bd


# ── Streams ──────────────────────────────────────────────────────────────────
def stream_state():
    """Per-stream live hand count + completion, for the calendar to overlay."""
    conn = get_db()
    counts = {r["stream_id"]: r["n"] for r in conn.execute(
        "SELECT stream_id, COUNT(*) AS n FROM hands WHERE stream_id IS NOT NULL GROUP BY stream_id"
    ).fetchall()}
    states = {}
    for r in conn.execute("SELECT id, is_complete FROM streams").fetchall():
        states[r["id"]] = {"handsCompleted": counts.get(r["id"], 0), "isComplete": bool(r["is_complete"])}
    # Streams that have hands but no catalog row (manually added in builder).
    for sid, n in counts.items():
        states.setdefault(sid, {"handsCompleted": n, "isComplete": False})
    conn.close()
    return states


def upsert_stream(stream):
    conn = get_db()
    conn.execute(
        "INSERT INTO streams (id, youtube_url, title, date, duration_minutes, hands_estimated) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "youtube_url=COALESCE(excluded.youtube_url, youtube_url), "
        "title=COALESCE(excluded.title, title), date=COALESCE(excluded.date, date), "
        "duration_minutes=COALESCE(excluded.duration_minutes, duration_minutes), "
        "hands_estimated=COALESCE(excluded.hands_estimated, hands_estimated)",
        (stream.get("id"), stream.get("youtubeUrl"), stream.get("title"), stream.get("date"),
         stream.get("durationMinutes"), stream.get("handsEstimated")),
    )
    conn.commit()
    conn.close()


def set_stream_complete(stream_id, complete=True, meta=None):
    """Mark a stream complete (upserting it first if needed). Returns bonus info."""
    if meta:
        meta = {**meta, "id": stream_id}
        upsert_stream(meta)
    conn = get_db()
    conn.execute("INSERT OR IGNORE INTO streams (id, is_complete) VALUES (?,0)", (stream_id,))
    conn.execute(
        "UPDATE streams SET is_complete = ?, completed_at = ? WHERE id = ?",
        (1 if complete else 0, now_iso() if complete else None, stream_id),
    )
    conn.commit()
    # Per-user bonus from this stream (for feedback).
    shares = conn.execute(
        "SELECT user_id, COUNT(*) AS hands FROM hands WHERE stream_id = ? GROUP BY user_id",
        (stream_id,),
    ).fetchall()
    conn.close()
    return [
        {"user_id": r["user_id"], "hands": r["hands"],
         "bonus": round(r["hands"] * STREAM_BONUS_PER_HAND, 2)}
        for r in shares
    ]


# ── Admin ────────────────────────────────────────────────────────────────────
def admin_overview():
    conn = get_db()
    users = []
    total_earned = 0.0
    total_paid = 0.0
    for u in conn.execute("SELECT * FROM users ORDER BY id").fetchall():
        bd = _earnings_breakdown(conn, u["id"])
        last = conn.execute(
            "SELECT MAX(created_at) AS t FROM hands WHERE user_id = ?", (u["id"],)
        ).fetchone()["t"]
        users.append({
            **public_user(u),
            "hands": bd["hands"], "earnings": bd["total"], "base": bd["base"],
            "stream_bonus": bd["stream_bonus"], "paid": bd["paid"], "owed": bd["owed"],
            "last_active": last or u["created_at"],
        })
        total_earned += bd["total"]
        total_paid += bd["paid"]

    streams = []
    for s in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM hands h WHERE h.stream_id = s.id) AS hands_done "
        "FROM streams s WHERE s.is_complete = 1 OR "
        "(SELECT COUNT(*) FROM hands h WHERE h.stream_id = s.id) > 0 ORDER BY s.date DESC"
    ).fetchall():
        contributors = conn.execute(
            "SELECT u.username AS username, COUNT(*) AS hands FROM hands h "
            "JOIN users u ON u.id = h.user_id WHERE h.stream_id = ? GROUP BY h.user_id ORDER BY hands DESC",
            (s["id"],),
        ).fetchall()
        streams.append({
            "id": s["id"], "title": s["title"], "date": s["date"],
            "youtube_url": s["youtube_url"], "is_complete": bool(s["is_complete"]),
            "completed_at": s["completed_at"], "hands_done": s["hands_done"],
            "contributors": [dict(c) for c in contributors],
        })

    total_hands = conn.execute("SELECT COUNT(*) AS n FROM hands").fetchone()["n"]
    conn.close()
    platform = {
        "users": len(users),
        "hands": total_hands,
        "earned": round(total_earned, 2),
        "paid": round(total_paid, 2),
        "owed": round(total_earned - total_paid, 2),
    }
    return {"users": users, "streams": streams, "platform": platform}


def admin_user_detail(user_id):
    u = get_user(user_id)
    if not u:
        return None
    conn = get_db()
    bd = _earnings_breakdown(conn, user_id)
    hands = conn.execute(
        "SELECT id, stream_id, youtube_url, timestamp_seconds, pt4_text, cards_count, "
        "actions_count, earnings, created_at FROM hands WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    pays = conn.execute(
        "SELECT id, amount, date, note, created_at FROM payments WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return {
        "user": public_user(u),
        "stats": bd,
        "hands": [dict(h) for h in hands],
        "payments": [dict(p) for p in pays],
    }


def record_payment(user_id, amount, date, note):
    conn = get_db()
    conn.execute(
        "INSERT INTO payments (user_id, amount, date, note, created_at) VALUES (?,?,?,?,?)",
        (user_id, round(float(amount), 2), date or now_iso()[:10], note or "", now_iso()),
    )
    conn.commit()
    conn.close()


def export_all_text():
    """The entire database as one human-readable .txt (hands grouped by user)."""
    conn = get_db()
    lines = []
    lines.append("=" * 70)
    lines.append("POKER HAND BUILDER — FULL DATABASE EXPORT")
    lines.append(f"Generated: {now_iso()}")
    lines.append("=" * 70)
    for u in conn.execute("SELECT * FROM users ORDER BY id").fetchall():
        bd = _earnings_breakdown(conn, u["id"])
        lines.append("")
        lines.append(f"USER #{u['id']}  {u['username']}" + ("  [ADMIN]" if u["is_admin"] else ""))
        lines.append(f"  email: {u['email'] or '-'}   joined: {u['created_at']}")
        lines.append(f"  hands: {bd['hands']}   base: ${bd['base']:.2f}   "
                     f"stream bonus: ${bd['stream_bonus']:.2f}   total: ${bd['total']:.2f}")
        lines.append(f"  paid: ${bd['paid']:.2f}   owed: ${bd['owed']:.2f}")
        pays = conn.execute(
            "SELECT amount, date, note FROM payments WHERE user_id = ? ORDER BY id", (u["id"],)
        ).fetchall()
        for p in pays:
            lines.append(f"    payment ${p['amount']:.2f} on {p['date']} — {p['note'] or ''}")
        hands = conn.execute(
            "SELECT * FROM hands WHERE user_id = ? ORDER BY id", (u["id"],)
        ).fetchall()
        for h in hands:
            lines.append("")
            lines.append(f"  --- hand #{h['id']}  stream={h['stream_id']}  "
                         f"t={h['timestamp_seconds']}s  {h['created_at']} ---")
            if h["youtube_url"]:
                lines.append(f"  {h['youtube_url']}")
            if h["pt4_text"]:
                for ln in h["pt4_text"].splitlines():
                    lines.append("  " + ln)
    conn.close()
    return "\n".join(lines) + "\n"
