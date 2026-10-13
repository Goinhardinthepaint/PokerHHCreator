"""
SQLite-backed accounts, hand histories, streams and payments for the hand builder.

A single SQLite file (DB_PATH) holds EVERYTHING persistent — accounts, hands +
PT4 text, streams + completion, payments, month assignments, per-stream default
lineups, tutorial/error state. It lives on the Railway volume (or the project
dir locally). Stdlib + werkzeug only, so the deployed app needs no extra deps.

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
from datetime import date, datetime, timezone, timedelta


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
SEED_JSON = os.path.join(HERE, "scripts", "hcl_streams.json")

# ALL persistent data (accounts, hands+PT4, streams, payments, month
# assignments, defaults, tutorial/error state) lives in this one SQLite file.
# Railway's container filesystem is ephemeral, so it MUST sit on the mounted
# volume or it's wiped on every deploy. Prefer the volume env var, then a /data
# mount, then the project dir for local dev.
DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or ("/data" if os.path.isdir("/data") else HERE)
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except OSError:
    DATA_DIR = HERE
DB_PATH = os.path.join(DATA_DIR, "users.db")

PIECE_RATE = 0.03
COMPLETION_BONUS = 0.10
STREAM_BONUS_PER_HAND = 0.05
SIDE_PAYOUT_BONUS = 0.25     # per bounty paid / squid round settled in a hand

MONTH_BONUS_DEFAULT = 150.0   # paid when a worker's assigned month is completed
MONTH_DEADLINE_DAYS = 14      # default deadline = 2 weeks from assignment
ERR_FULL = 0.10               # < 10% errors → full month bonus
ERR_HALF = 0.20               # 10–20% → half; > 20% → none


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# A row that counts as a worker's hand (paid, counted, exported). Other types:
# 'tutorial_bonus' (a one-off $1 credit) and 'duplicate' (a second copy of a
# hand that was already saved; kept for the record, never paid or counted).
HAND_ROW = "COALESCE(type,'hand') = 'hand'"
H_HAND_ROW = "COALESCE(h.type,'hand') = 'hand'"


def _parse_ts(s):
    """created_at / completed_at text -> aware UTC datetime (None if unreadable)."""
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _eastern_offset(day):
    """UTC offset (hours) of US Eastern time on a date: -4 from the second Sunday
    of March to the first Sunday of November, else -5. Computed by hand because
    Windows Pythons often ship without a time-zone database."""
    def nth_sunday(month, n):
        first = date(day.year, month, 1)
        return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))
    return -4 if nth_sunday(3, 2) <= day < nth_sunday(11, 1) else -5


def end_of_day_utc(day):
    """The UTC instant an Eastern-time calendar day ('YYYY-MM-DD' or date) ends."""
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    # The clocks change at 2am, so the midnight that ends `day` still carries
    # `day`'s own offset (the end of Oct 31 is EDT though Nov 1 is EST).
    nxt = day + timedelta(days=1)
    return datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc) - timedelta(hours=_eastern_offset(day))


def last_friday(today=None):
    """The most recent Friday that has fully ended in Eastern time."""
    if today is None:
        now = datetime.now(timezone.utc)
        today = (now + timedelta(hours=_eastern_offset(now.date()))).date()
    return today - timedelta(days=(today.weekday() - 4) % 7 or 7)


# ── Backend selection: Postgres when DATABASE_URL is set, else local SQLite ───
DATABASE_URL = os.environ.get("DATABASE_URL")
IS_PG = bool(DATABASE_URL)
if IS_PG:
    import psycopg2
    import psycopg2.extras
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    _INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

# Autoincrement PK differs by dialect.
PK = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"


def _q(sql):
    """SQLite uses '?' placeholders; Postgres uses '%s'. Queries are written with
    '?' and translated here. (No query uses a literal '%', so this is safe.)"""
    return sql.replace("?", "%s") if IS_PG else sql


class _Conn:
    """Uniform connection wrapper over psycopg2 / sqlite3. `.execute(sql, params)`
    takes '?'-style SQL and returns a cursor whose rows are accessible by column
    name (and dict(row) / row.keys() work on both)."""

    def __init__(self):
        if IS_PG:
            self._c = psycopg2.connect(DATABASE_URL)
        else:
            self._c = sqlite3.connect(DB_PATH)
            self._c.row_factory = sqlite3.Row
            self._c.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql, params=()):
        if IS_PG:
            cur = self._c.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = self._c.cursor()
        cur.execute(_q(sql), params)
        return cur

    def commit(self):
        self._c.commit()

    def rollback(self):
        try:
            self._c.rollback()
        except Exception:
            pass

    def close(self):
        self._c.close()


def get_db():
    return _Conn()


def _add_column(conn, table, coldef):
    """Add a column if missing (idempotent across dialects)."""
    if IS_PG:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {coldef}")
    else:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
        except sqlite3.OperationalError:
            pass  # column already exists


def _hand_earnings(cards, actions, side_payouts=0):
    return round((cards + actions) * PIECE_RATE + COMPLETION_BONUS + side_payouts * SIDE_PAYOUT_BONUS, 2)


# ── Schema + seed ────────────────────────────────────────────────────────────
def init_db():
    conn = get_db()
    for stmt in (
        f"CREATE TABLE IF NOT EXISTS users (id {PK}, username TEXT UNIQUE NOT NULL, "
        "email TEXT, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS hands (id {PK}, user_id INTEGER NOT NULL, stream_id TEXT, "
        "youtube_url TEXT, timestamp_seconds INTEGER, pt4_text TEXT, "
        "cards_count INTEGER NOT NULL DEFAULT 0, actions_count INTEGER NOT NULL DEFAULT 0, "
        "earnings REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS streams (id TEXT PRIMARY KEY, youtube_url TEXT, title TEXT, "
        "date TEXT, duration_minutes INTEGER, hands_estimated INTEGER, "
        "is_complete INTEGER NOT NULL DEFAULT 0, completed_at TEXT)",
        f"CREATE TABLE IF NOT EXISTS payments (id {PK}, user_id INTEGER NOT NULL, amount REAL NOT NULL, "
        "date TEXT, note TEXT, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS month_assignments (month TEXT PRIMARY KEY, user_id INTEGER, "
        "bonus_amount REAL NOT NULL DEFAULT 150, deadline TEXT, assigned_at TEXT)",
        "CREATE INDEX IF NOT EXISTS idx_hands_user ON hands(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_hands_stream ON hands(stream_id)",
        "CREATE INDEX IF NOT EXISTS idx_pay_user ON payments(user_id)",
    ):
        conn.execute(stmt)
    conn.commit()

    # Columns added after the initial schema shipped (idempotent).
    _add_column(conn, "streams", "default_lineup TEXT")
    _add_column(conn, "streams", "resume_state TEXT")
    _add_column(conn, "hands", "next_state TEXT")          # post-hand snapshot
    _add_column(conn, "users", "error_count INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "users", "tutorial_completed INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "hands", "type TEXT NOT NULL DEFAULT 'hand'")  # 'hand' | 'tutorial_bonus'
    _add_column(conn, "hands", "hand_number TEXT")        # PokerStars Hand # (import dedup)
    _add_column(conn, "hands", "side_payouts INTEGER NOT NULL DEFAULT 0")  # bounty paid / squid settled
    conn.commit()
    # Every per-worker total (dashboard on each save, admin, earnings) filters
    # hands by user. The HH text lives in the same row (~1.5KB each), so
    # answering those from an index that carries the summed columns avoids
    # reading every row. Created after the migrations above: it needs `type`.
    conn.execute("DROP INDEX IF EXISTS idx_hands_user_totals")   # superseded: no side_payouts
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hands_user_totals2 ON hands "
        "(user_id, type, stream_id, cards_count, actions_count, side_payouts, earnings, created_at)"
    )
    conn.commit()
    try:
        dups = mark_duplicate_hands(conn)
        if dups:
            print(f"Marked {dups} duplicate hand submission(s) — no longer paid or counted", flush=True)
    except Exception as e:
        conn.rollback()
        print(f"duplicate check skipped: {e}", flush=True)

    # Bootstrap the admin account (username "admin", password from env).
    if not conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone():
        pw = os.environ.get("ADMIN_PASSWORD", "changeme")
        conn.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, created_at) "
            "VALUES (?,?,?,?,?)",
            ("admin", "", generate_password_hash(pw), 1, now_iso()),
        )
        conn.commit()

    # Top up the stream catalog from the committed scrape on every boot. This used
    # to run only when the table was empty, which meant a refreshed scrape could
    # never reach an already-populated database — new HCL streams simply never
    # appeared. Existing rows are never overwritten: DO UPDATE only fills columns
    # that are still NULL, so completion state, saved lineups and hand-edited
    # estimates all survive, and the dateless placeholder rows written by
    # set_default_lineup / set_resume_state get their metadata backfilled.
    if os.path.exists(SEED_JSON):
        try:
            with open(SEED_JSON, encoding="utf-8") as f:
                rows = json.load(f)
            before = conn.execute("SELECT COUNT(*) AS n FROM streams").fetchone()["n"]
            for s in rows:
                conn.execute(
                    "INSERT INTO streams "
                    "(id, youtube_url, title, date, duration_minutes, hands_estimated, is_complete) "
                    "VALUES (?,?,?,?,?,?,0) ON CONFLICT (id) DO UPDATE SET "
                    "youtube_url = COALESCE(streams.youtube_url, excluded.youtube_url), "
                    "title = COALESCE(streams.title, excluded.title), "
                    "date = COALESCE(streams.date, excluded.date), "
                    "duration_minutes = COALESCE(streams.duration_minutes, excluded.duration_minutes), "
                    "hands_estimated = COALESCE(streams.hands_estimated, excluded.hands_estimated)",
                    (s.get("id"), s.get("youtubeUrl"), s.get("title"), s.get("date"),
                     s.get("durationMinutes"), s.get("handsEstimated")),
                )
            conn.commit()
            added = conn.execute("SELECT COUNT(*) AS n FROM streams").fetchone()["n"] - before
            if added:
                print(f"Stream catalog: +{added} new stream(s) from the committed scrape", flush=True)
        except Exception as e:
            conn.rollback()  # seeding is best-effort; don't poison the connection
            print(f"stream seed skipped: {e}", flush=True)

    # Backfill post-hand snapshots for older hands that predate next_state, so
    # clicking them in the sidebar restores the table (esp. imported hands).
    try:
        backfill_snapshots(conn)
    except Exception as e:
        print(f"snapshot backfill skipped: {e}", flush=True)

    # Report what was loaded — confirms the DB persisted across restarts/deploys.
    n_users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    n_hands = conn.execute("SELECT COUNT(*) AS n FROM hands WHERE COALESCE(type,'hand') = 'hand'").fetchone()["n"]
    n_streams = conn.execute("SELECT COUNT(*) AS n FROM streams").fetchone()["n"]
    if IS_PG:
        print(f"Connected to Postgres — {n_users} users, {n_hands} hands, {n_streams} streams", flush=True)
    else:
        print(f"Connected to SQLite (local dev) — {n_users} users, {n_hands} hands, {n_streams} streams", flush=True)
    conn.close()


# ── Users ────────────────────────────────────────────────────────────────────
def create_user(username, email, password):
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required.")
    conn = get_db()
    try:
        row = conn.execute(
            "INSERT INTO users (username, email, password_hash, is_admin, created_at) "
            "VALUES (?,?,?,0,?) RETURNING id",
            (username, (email or "").strip(), generate_password_hash(password), now_iso()),
        ).fetchone()
        conn.commit()
        return get_user(row["id"])
    except _INTEGRITY_ERRORS:
        conn.rollback()
        raise ValueError("Username taken — try logging in, or contact admin to reset your password.")
    finally:
        conn.close()


def verify_user(username, password):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password or ""):
        return dict(row)
    return None


def login_check(username, password):
    """Authenticate, distinguishing the two failure modes so the UI can show a
    specific message. Returns (status, user) where status is one of
    'ok' | 'no_user' | 'bad_password'."""
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),)).fetchone()
    conn.close()
    if not row:
        return "no_user", None
    if not check_password_hash(row["password_hash"], password or ""):
        return "bad_password", None
    return "ok", dict(row)


def set_user_password(user_id, new_password):
    """Admin-driven password reset. Returns True if a user row was updated."""
    if not (new_password or "").strip():
        raise ValueError("New password is required.")
    conn = get_db()
    cur = conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return bool(updated)


def delete_user(user_id):
    """Remove a user and everything they own (hands, payments). Month
    assignments owned by the user are unassigned rather than deleted so the
    month row survives. Returns True if a user was deleted."""
    conn = get_db()
    try:
        conn.execute("DELETE FROM hands WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM payments WHERE user_id = ?", (user_id,))
        conn.execute("UPDATE month_assignments SET user_id = NULL WHERE user_id = ?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return bool(cur.rowcount)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def public_user(row):
    keys = row.keys()
    return {"id": row["id"], "username": row["username"], "email": row["email"],
            "is_admin": bool(row["is_admin"]),
            "tutorial_completed": bool(row["tutorial_completed"]) if "tutorial_completed" in keys else False}


def set_tutorial_complete(user_id, completed=True):
    conn = get_db()
    conn.execute("UPDATE users SET tutorial_completed = ? WHERE id = ?", (1 if completed else 0, user_id))
    conn.commit()
    conn.close()


TUTORIAL_BONUS = 1.00


def award_tutorial_bonus(user_id):
    """Credit the one-off $1 tutorial bonus, exactly once per user. Returns True
    if it was awarded just now, False if the user already had it."""
    conn = get_db()
    exists = conn.execute(
        "SELECT 1 FROM hands WHERE user_id = ? AND type = 'tutorial_bonus' LIMIT 1", (user_id,)
    ).fetchone()
    if exists:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO hands (user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, "
        "cards_count, actions_count, earnings, type, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, None, None, 0, "Tutorial completion bonus", 0, 0, TUTORIAL_BONUS,
         "tutorial_bonus", now_iso()),
    )
    conn.commit()
    conn.close()
    return True


# ── Hands ────────────────────────────────────────────────────────────────────
def insert_hand(user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, cards_count, actions_count,
                next_state=None, side_payouts=0):
    cards = int(cards_count or 0)
    actions = int(actions_count or 0)
    side_payouts = max(0, int(side_payouts or 0))
    # Normalize the stream key to the canonical 11-char video id so a hand always
    # matches the calendar's stream (whose id is the video id) — regardless of
    # whether the client sent a bare id, a full watch URL, or a /live/ link.
    stream_id = extract_video_id(stream_id) or extract_video_id(youtube_url) or (stream_id or "")
    earnings = _hand_earnings(cards, actions, side_payouts)
    conn = get_db()
    # A double-click, or a retry after a slow response, used to save the same
    # hand twice and pay for it twice (70 such hands in production by Oct 2026).
    # The same worker sending an identical hand history for the same timestamp
    # gets the hand already saved back instead of a second, paid copy.
    if pt4_text:
        dup = conn.execute(
            "SELECT id, earnings FROM hands WHERE user_id = ? AND stream_id = ? AND timestamp_seconds = ? "
            f"AND {HAND_ROW} AND pt4_text = ? ORDER BY id LIMIT 1",
            (user_id, stream_id, int(timestamp_seconds or 0), pt4_text),
        ).fetchone()
        if dup:
            conn.close()
            return dup["id"], dup["earnings"]
    row = conn.execute(
        "INSERT INTO hands (user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, "
        "cards_count, actions_count, side_payouts, earnings, next_state, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id",
        (user_id, stream_id, youtube_url, int(timestamp_seconds or 0), pt4_text or "",
         cards, actions, side_payouts, earnings, json.dumps(next_state) if next_state else None, now_iso()),
    ).fetchone()
    hand_id = row["id"]
    # Upsert a minimal stream row so the hand always ties to a known stream.
    if stream_id:
        conn.execute(
            "INSERT INTO streams (id, youtube_url, is_complete) VALUES (?,?,0) ON CONFLICT DO NOTHING",
            (stream_id, youtube_url),
        )
    conn.commit()
    conn.close()
    return hand_id, earnings


def _hand_row(r):
    d = dict(r)
    raw = d.pop("next_state", None)
    try:
        d["next_state"] = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        d["next_state"] = None
    return d


def user_hands(user_id, stream_id=None):
    """All of a user's hands (optionally one stream), ordered by timestamp."""
    cols = ("id, stream_id, youtube_url, timestamp_seconds, pt4_text, cards_count, "
            "actions_count, earnings, next_state, created_at")
    conn = get_db()
    if stream_id:
        sid = extract_video_id(stream_id) or stream_id
        rows = conn.execute(
            f"SELECT {cols} FROM hands WHERE user_id = ? AND stream_id = ? "
            "AND COALESCE(type,'hand') = 'hand' ORDER BY timestamp_seconds, id",
            (user_id, sid),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {cols} FROM hands WHERE user_id = ? AND COALESCE(type,'hand') = 'hand' "
            "ORDER BY timestamp_seconds, id",
            (user_id,),
        ).fetchall()
    conn.close()
    return [_hand_row(r) for r in rows]


def get_hand(hand_id, user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM hands WHERE id = ? AND user_id = ?", (hand_id, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_hand(hand_id, user_id):
    conn = get_db()
    cur = conn.execute("DELETE FROM hands WHERE id = ? AND user_id = ?", (hand_id, user_id))
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted > 0


def backfill_snapshots(conn=None):
    """Generate next_state snapshots for every real hand that lacks one, by
    parsing its stored pt4_text. Idempotent — only fills NULL/empty next_state.
    Returns the number of hands updated."""
    import hand_import

    own = conn is None
    if own:
        conn = get_db()
    rows = conn.execute(
        "SELECT id, pt4_text FROM hands WHERE COALESCE(type,'hand') = 'hand' "
        "AND (next_state IS NULL OR next_state = '') "
        "AND pt4_text IS NOT NULL AND pt4_text != ''"
    ).fetchall()
    n = 0
    for r in rows:
        try:
            ns = hand_import.next_state_from_pt4(r["pt4_text"])
        except Exception:
            ns = None
        if ns:
            conn.execute("UPDATE hands SET next_state = ? WHERE id = ?",
                         (json.dumps(ns), r["id"]))
            n += 1
    conn.commit()
    if own:
        conn.close()
    if n:
        print(f"Backfilled {n} hand snapshot(s)", flush=True)
    return n


def import_hands(raw_text, user_id):
    """Bulk-import raw PT4 hand-history text under `user_id` (the admin).

    For each hand: parse it, dedupe (by Hand # or by video-id+timestamp), look up
    the stream (auto-creating one from the URL if absent), rewrite the header's
    date (and time, when a timestamp is known) from the stream's date, score
    earnings, and insert. Returns a summary dict:
        { imported, skipped, errors, error_details, streams: {vid: count} }."""
    import hand_import  # local import avoids a circular import at module load

    blocks = hand_import.split_hands(raw_text)
    imported = skipped = errors = 0
    error_details = []
    affected = set()
    conn = get_db()
    try:
        for block in blocks:
            try:
                p = hand_import.parse_hand(block)
            except Exception as e:
                errors += 1
                error_details.append(str(e))
                continue

            hand_number = p["hand_number"]
            vid = p["video_id"]
            ts = p["timestamp_seconds"]

            # ── Duplicate detection ───────────────────────────────────────────
            dup = False
            if hand_number and conn.execute(
                "SELECT 1 FROM hands WHERE hand_number = ? LIMIT 1", (hand_number,)
            ).fetchone():
                dup = True
            # Same video + same (non-zero) timestamp is the same hand. ts==0 means
            # "no timestamp", so we don't dedupe distinct Livestream hands on it.
            if not dup and vid and ts > 0 and conn.execute(
                "SELECT 1 FROM hands WHERE stream_id = ? AND timestamp_seconds = ? "
                "AND COALESCE(type,'hand') = 'hand' LIMIT 1", (vid, ts)
            ).fetchone():
                dup = True
            if dup:
                skipped += 1
                continue

            # ── Stream lookup / auto-create ───────────────────────────────────
            stream_date = None
            if vid:
                srow = conn.execute("SELECT date FROM streams WHERE id = ?", (vid,)).fetchone()
                if srow:
                    stream_date = srow["date"]
                else:
                    conn.execute(
                        "INSERT INTO streams (id, youtube_url, is_complete) VALUES (?,?,0) "
                        "ON CONFLICT DO NOTHING", (vid, p["youtube_url"] or None),
                    )
                affected.add(vid)

            # ── Date/time rewrite ─────────────────────────────────────────────
            # Time always comes from the YouTube timestamp (00:00:00 when none);
            # date comes from the stream when known, else the original is kept.
            text = hand_import.rewrite_header(
                p["text"], new_date=stream_date, new_time=hand_import.secs_to_hms(ts))

            # ── Earnings + post-hand snapshot + insert ────────────────────────
            cards, actions = p["cards_count"], p["actions_count"]
            earnings = _hand_earnings(cards, actions)
            try:
                next_state = hand_import.next_state_from_pt4(text)
            except Exception:
                next_state = None
            conn.execute(
                "INSERT INTO hands (user_id, stream_id, youtube_url, timestamp_seconds, "
                "pt4_text, cards_count, actions_count, earnings, type, hand_number, "
                "next_state, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, vid or "", p["youtube_url"] or None, ts, text,
                 cards, actions, earnings, "hand", hand_number,
                 json.dumps(next_state) if next_state else None, now_iso()),
            )
            imported += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Recalculate per-stream hand counts (derived) for the affected streams.
    streams = {}
    for vid in affected:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM hands WHERE stream_id = ? AND COALESCE(type,'hand') = 'hand'",
            (vid,),
        ).fetchone()
        streams[vid] = row["n"]
    conn.close()
    return {
        "imported": imported, "skipped": skipped, "errors": errors,
        "error_details": error_details[:20], "streams": streams,
    }


def stream_meta(stream_id):
    sid = extract_video_id(stream_id) or (stream_id or "")
    conn = get_db()
    row = conn.execute(
        "SELECT id, youtube_url, title, date, duration_minutes, hands_estimated, is_complete "
        "FROM streams WHERE id = ?", (sid,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _stream_row(r):
    """DB row -> the calendar's camelCase stream shape."""
    return {
        "id": r["id"],
        "youtubeUrl": r["youtube_url"] or "",
        "title": r["title"] or "HCL Stream",
        "date": r["date"] or "",
        "durationMinutes": r["duration_minutes"] or 0,
        "handsCompleted": 0,          # overlaid client-side from stream_state()
        "handsEstimated": r["hands_estimated"] or 0,
        "isComplete": bool(r["is_complete"]),
        "addedAt": "",
    }


def list_streams():
    """The whole stream catalog, newest last, in calendar shape.

    Rows without a date are skipped: set_default_lineup / set_resume_state /
    set_stream_complete insert bare `(id, is_complete)` placeholder rows for
    streams typed straight into the builder, and those would otherwise show up
    on the calendar as undated blanks.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, youtube_url, title, date, duration_minutes, hands_estimated, is_complete "
        "FROM streams WHERE date IS NOT NULL AND date <> '' ORDER BY date"
    ).fetchall()
    conn.close()
    return [_stream_row(r) for r in rows]


def last_stream_timestamp(stream_id):
    """Latest hand timestamp on a stream (across all workers) — the resume point."""
    sid = extract_video_id(stream_id) or (stream_id or "")
    conn = get_db()
    row = conn.execute(
        "SELECT MAX(timestamp_seconds) AS t FROM hands WHERE stream_id = ?", (sid,)
    ).fetchone()
    conn.close()
    return row["t"] if row else None


def set_resume_state(stream_id, state):
    sid = extract_video_id(stream_id) or (stream_id or "")
    if not sid:
        return
    conn = get_db()
    conn.execute("INSERT INTO streams (id, is_complete) VALUES (?,0) ON CONFLICT DO NOTHING", (sid,))
    conn.execute("UPDATE streams SET resume_state = ? WHERE id = ?", (json.dumps(state), sid))
    conn.commit()
    conn.close()


def get_resume_state(stream_id):
    sid = extract_video_id(stream_id) or (stream_id or "")
    conn = get_db()
    row = conn.execute("SELECT resume_state FROM streams WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if row and row["resume_state"]:
        try:
            return json.loads(row["resume_state"])
        except (ValueError, TypeError):
            return None
    return None


# ── Month assignments + bonus ────────────────────────────────────────────────
def _month_stats_row(month, total, complete, est, done):
    return {
        "month": month, "total_streams": total, "complete_streams": complete,
        "is_complete": total > 0 and complete == total,
        "hands_estimated": est, "hands_done": done,
        "pct": round(100 * done / est) if est else 0,
    }


def _month_stats(conn, month):
    """Streams + hands progress for a calendar month ("YYYY-MM"), across all workers."""
    rows = conn.execute(
        "SELECT is_complete, hands_estimated, completed_at FROM streams WHERE substr(date,1,7) = ?", (month,)
    ).fetchall()
    done = conn.execute(
        "SELECT COUNT(*) AS n FROM hands h JOIN streams s ON h.stream_id = s.id "
        f"WHERE substr(s.date,1,7) = ? AND {H_HAND_ROW}", (month,)
    ).fetchone()["n"]
    ms = _month_stats_row(
        month, len(rows), sum(1 for r in rows if r["is_complete"]),
        sum((r["hands_estimated"] or 0) for r in rows), done,
    )
    # When the month's last stream was marked complete (checked against the
    # bonus deadline).
    stamps = [t for t in (_parse_ts(r["completed_at"]) for r in rows if r["is_complete"]) if t]
    ms["completed_at"] = max(stamps).isoformat(timespec="seconds") if ms["is_complete"] and stamps else None
    return ms


def _user_errors(conn, user_id, hands=None):
    """(errors, hands, rate). Pass `hands` when the caller already counted them."""
    if hands is None:
        hands = conn.execute(f"SELECT COUNT(*) AS n FROM hands WHERE user_id = ? AND {HAND_ROW}", (user_id,)).fetchone()["n"]
    errors = conn.execute("SELECT COALESCE(error_count,0) AS e FROM users WHERE id = ?", (user_id,)).fetchone()["e"]
    return errors, hands, (errors / hands if hands else 0.0)


def _past(deadline, when):
    """True if `when` (aware datetime) is after the Eastern end of `deadline`'s day."""
    try:
        return bool(deadline) and when >= end_of_day_utc(deadline)
    except (TypeError, ValueError):
        return False


def _month_bonuses(conn, user_id, hands=None):
    """The month-completion bonus for every month assigned to a worker, newest
    first, each tiered by error rate.

    A worker can own several months and each pays its own bonus (this used to
    read one arbitrary assignment, so a second completed month paid nothing).
    A month pays only if its last stream was marked complete by the end of its
    deadline day, Eastern time; finished after that it is 'late' and pays $0.
    The error tier uses the worker's overall error rate.
    """
    rows = conn.execute(
        "SELECT * FROM month_assignments WHERE user_id = ? ORDER BY month DESC", (user_id,)
    ).fetchall()
    if not rows:
        return []
    errors, hands, rate = _user_errors(conn, user_id, hands)
    now = datetime.now(timezone.utc)
    out = []
    for a in rows:
        ms = _month_stats(conn, a["month"])
        bonus_amount = a["bonus_amount"] if a["bonus_amount"] is not None else MONTH_BONUS_DEFAULT
        finished = _parse_ts(ms["completed_at"]) if ms["completed_at"] else None
        if not ms["is_complete"]:
            status, amount = "incomplete", 0.0
        elif finished and _past(a["deadline"], finished):
            status, amount = "late", 0.0
        elif rate < ERR_FULL:
            status, amount = "full", bonus_amount
        elif rate <= ERR_HALF:
            status, amount = "half", round(bonus_amount / 2, 2)
        else:
            status, amount = "forfeited", 0.0
        out.append({
            "month": a["month"], "bonus_amount": bonus_amount, "deadline": a["deadline"],
            "is_complete": ms["is_complete"], "completed_at": ms["completed_at"],
            "overdue": not ms["is_complete"] and _past(a["deadline"], now),
            "total_streams": ms["total_streams"],
            "complete_streams": ms["complete_streams"], "hands_done": ms["hands_done"],
            "hands_estimated": ms["hands_estimated"], "pct": ms["pct"],
            "error_count": errors, "error_rate": round(rate, 4),
            "status": status, "amount": amount,
        })
    return out


def set_month_assignment(month, user_id, bonus_amount=None, deadline=None):
    conn = get_db()
    if not user_id:
        conn.execute("DELETE FROM month_assignments WHERE month = ?", (month,))
    else:
        if not deadline:
            deadline = (datetime.now(timezone.utc) + timedelta(days=MONTH_DEADLINE_DAYS)).date().isoformat()
        conn.execute(
            "INSERT INTO month_assignments (month, user_id, bonus_amount, deadline, assigned_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (month) DO UPDATE SET "
            "user_id=excluded.user_id, bonus_amount=excluded.bonus_amount, "
            "deadline=excluded.deadline, assigned_at=excluded.assigned_at",
            (month, user_id, float(bonus_amount) if bonus_amount is not None else MONTH_BONUS_DEFAULT,
             deadline, now_iso()),
        )
    conn.commit()
    conn.close()


def set_error_count(user_id, count):
    conn = get_db()
    conn.execute("UPDATE users SET error_count = ? WHERE id = ?", (max(0, int(count or 0)), user_id))
    conn.commit()
    conn.close()


def month_list(admin=False):
    """Every month that has streams, with its owner + progress. Admin variant adds
    owner-vs-helper hand counts and a neglect flag."""
    conn = get_db()
    # One grouped query per figure rather than three or four queries per month:
    # every month with streams is listed (30+ and growing), and the per-month
    # version re-scanned streams and hands for each one.
    stats = conn.execute(
        "SELECT substr(date,1,7) AS m, COUNT(*) AS total, "
        "SUM(CASE WHEN is_complete <> 0 THEN 1 ELSE 0 END) AS complete, "
        "SUM(COALESCE(hands_estimated,0)) AS est "
        "FROM streams WHERE date IS NOT NULL AND date != '' "
        "GROUP BY substr(date,1,7) ORDER BY m DESC"
    ).fetchall()
    done = {r["m"]: r["n"] for r in conn.execute(
        "SELECT substr(s.date,1,7) AS m, COUNT(*) AS n FROM hands h JOIN streams s ON h.stream_id = s.id "
        f"WHERE s.date IS NOT NULL AND s.date != '' AND {H_HAND_ROW} GROUP BY substr(s.date,1,7)"
    ).fetchall()}
    assigned = {r["month"]: r for r in conn.execute(
        "SELECT ma.month, ma.user_id, ma.bonus_amount, ma.deadline, u.username FROM month_assignments ma "
        "LEFT JOIN users u ON u.id = ma.user_id"
    ).fetchall()}
    owner_done = {}
    if admin:
        owner_done = {r["m"]: r["n"] for r in conn.execute(
            "SELECT ma.month AS m, COUNT(*) AS n FROM month_assignments ma "
            "JOIN hands h ON h.user_id = ma.user_id "
            "JOIN streams s ON s.id = h.stream_id AND substr(s.date,1,7) = ma.month "
            f"WHERE {H_HAND_ROW} GROUP BY ma.month"
        ).fetchall()}
    out = []
    for r in stats:
        m = r["m"]
        ms = _month_stats_row(m, r["total"], r["complete"] or 0, r["est"] or 0, done.get(m, 0))
        a = assigned.get(m)
        owner = {"id": a["user_id"], "username": a["username"]} if (a and a["user_id"]) else None
        row = {**ms, "owner": owner,
               "bonus_amount": (a["bonus_amount"] if a else None),
               "deadline": (a["deadline"] if a else None)}
        if admin and owner:
            oh = owner_done.get(m, 0)
            row["owner_hands"] = oh
            row["helper_hands"] = ms["hands_done"] - oh
            # Owner neglecting their own month: incomplete, work happening, but the
            # owner is contributing under a third of it.
            row["neglect"] = bool(not ms["is_complete"] and ms["hands_done"] > 0 and oh < ms["hands_done"] / 3)
        elif admin:
            row["owner_hands"] = 0
            row["helper_hands"] = ms["hands_done"]
            row["neglect"] = False
        out.append(row)
    conn.close()
    return out


# ── Earnings / stats ─────────────────────────────────────────────────────────
def _earnings_breakdown(conn, user_id):
    """Full earnings breakdown for one user from hands + completed streams + payments."""
    agg = conn.execute(
        "SELECT COUNT(*) AS hands, COALESCE(SUM(cards_count),0) AS cards, "
        "COALESCE(SUM(actions_count),0) AS actions, COALESCE(SUM(side_payouts),0) AS side_payouts "
        "FROM hands WHERE user_id = ? AND COALESCE(type,'hand') = 'hand'",
        (user_id,),
    ).fetchone()
    hands = agg["hands"]
    cards = agg["cards"]
    actions = agg["actions"]
    side_payouts = agg["side_payouts"]
    tutorial_bonus = conn.execute(
        "SELECT COALESCE(SUM(earnings),0) AS b FROM hands WHERE user_id = ? AND type = 'tutorial_bonus'",
        (user_id,),
    ).fetchone()["b"]

    # Stream bonus = $0.05 per the user's hands that live in a completed stream.
    bonus_hands = conn.execute(
        "SELECT COUNT(*) AS n FROM hands h JOIN streams s ON h.stream_id = s.id "
        f"WHERE h.user_id = ? AND s.is_complete = 1 AND {H_HAND_ROW}",
        (user_id,),
    ).fetchone()["n"]

    paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS p FROM payments WHERE user_id = ?", (user_id,)
    ).fetchone()["p"]

    cards_income = round(cards * PIECE_RATE, 2)
    actions_income = round(actions * PIECE_RATE, 2)
    completion = round(hands * COMPLETION_BONUS, 2)
    side_game_bonus = round(side_payouts * SIDE_PAYOUT_BONUS, 2)
    stream_bonus = round(bonus_hands * STREAM_BONUS_PER_HAND, 2)
    base = round(cards_income + actions_income + completion + side_game_bonus, 2)
    months = _month_bonuses(conn, user_id, hands)
    month_bonus = round(sum(m["amount"] for m in months), 2)
    total = round(base + stream_bonus + month_bonus + tutorial_bonus, 2)
    return {
        "hands": hands,
        "pieces": cards + actions,
        "cards": cards,
        "actions": actions,
        "cards_income": cards_income,
        "actions_income": actions_income,
        "completion_bonus": completion,
        "side_payouts": side_payouts,
        "side_game_bonus": side_game_bonus,
        "stream_bonus": stream_bonus,
        "month_bonus": month_bonus,
        "month_bonus_detail": months[0] if months else None,  # newest assigned month
        "month_bonuses": months,
        "tutorial_bonus": round(tutorial_bonus, 2),
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
        "WHERE h.user_id = ? AND COALESCE(h.type,'hand') = 'hand' "
        "GROUP BY h.stream_id, s.title ORDER BY hands DESC",
        (user_id,),
    ).fetchall()
    bd["streams_worked"] = [dict(r) for r in bd["streams_worked"]]
    bd["streams_count"] = len(bd["streams_worked"])
    recent = conn.execute(
        "SELECT id, stream_id, youtube_url, timestamp_seconds, cards_count, actions_count, "
        "earnings, created_at FROM hands WHERE user_id = ? AND COALESCE(type,'hand') = 'hand' "
        "ORDER BY id DESC LIMIT 25",
        (user_id,),
    ).fetchall()
    bd["recent_hands"] = [dict(r) for r in recent]

    # Assigned month + split of the worker's own hands into their month vs others.
    bd["assigned_month"] = bd.get("month_bonus_detail")
    month = bd["assigned_month"]["month"] if bd["assigned_month"] else None
    if month:
        own = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(earnings),0) AS e FROM hands h "
            f"JOIN streams s ON h.stream_id = s.id WHERE h.user_id = ? AND substr(s.date,1,7) = ? AND {H_HAND_ROW}",
            (user_id, month),
        ).fetchone()
        bd["own_month_hands"] = {"count": own["n"], "earnings": round(own["e"], 2)}
        bd["other_month_hands"] = {"count": bd["hands"] - own["n"], "earnings": round(bd["base"] - own["e"], 2)}
    else:
        bd["own_month_hands"] = None
        bd["other_month_hands"] = {"count": bd["hands"], "earnings": bd["base"]}
    conn.close()
    return bd


# ── Streams ──────────────────────────────────────────────────────────────────
def stream_state():
    """Per-stream live hand count + completion, for the calendar to overlay."""
    conn = get_db()
    counts = {r["stream_id"]: r["n"] for r in conn.execute(
        f"SELECT stream_id, COUNT(*) AS n FROM hands WHERE stream_id IS NOT NULL AND {HAND_ROW} GROUP BY stream_id"
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
        # The existing row's columns MUST be qualified with the table name here:
        # unqualified, Postgres reads "youtube_url" as ambiguous between the row
        # being inserted and the row being updated and rejects the whole
        # statement — so on Postgres this used to raise every time it ran, taking
        # Add Stream, Import and Mark Complete (via set_stream_complete) with it.
        "VALUES (?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET "
        "youtube_url=COALESCE(excluded.youtube_url, streams.youtube_url), "
        "title=COALESCE(excluded.title, streams.title), date=COALESCE(excluded.date, streams.date), "
        "duration_minutes=COALESCE(excluded.duration_minutes, streams.duration_minutes), "
        "hands_estimated=COALESCE(excluded.hands_estimated, streams.hands_estimated)",
        (stream.get("id"), stream.get("youtubeUrl"), stream.get("title"), stream.get("date"),
         stream.get("durationMinutes"), stream.get("handsEstimated")),
    )
    conn.commit()
    conn.close()


def get_default_lineup(stream_id):
    """Saved default lineup [{seat, name}] for a stream (by video id)."""
    sid = extract_video_id(stream_id) or (stream_id or "")
    conn = get_db()
    row = conn.execute("SELECT default_lineup FROM streams WHERE id = ?", (sid,)).fetchone()
    conn.close()
    if row and row["default_lineup"]:
        try:
            return json.loads(row["default_lineup"])
        except (ValueError, TypeError):
            return []
    return []


def set_default_lineup(stream_id, lineup):
    sid = extract_video_id(stream_id) or (stream_id or "")
    if not sid:
        return
    # Keep only seat + name (never stacks).
    clean = [{"seat": r.get("seat"), "name": (r.get("name") or "").strip()}
             for r in (lineup or []) if r.get("seat") is not None]
    conn = get_db()
    conn.execute("INSERT INTO streams (id, is_complete) VALUES (?,0) ON CONFLICT DO NOTHING", (sid,))
    conn.execute("UPDATE streams SET default_lineup = ? WHERE id = ?", (json.dumps(clean), sid))
    conn.commit()
    conn.close()


def set_stream_complete(stream_id, complete=True, meta=None):
    """Mark a stream complete (upserting it first if needed). Returns bonus info."""
    conn = get_db()
    if meta:
        # Fill missing catalog fields only. This used the overwriting upsert, so
        # finishing a stream from the builder replaced the catalog's date and
        # title with whatever the builder had (e.g. a stale "Video Date").
        conn.execute(
            "INSERT INTO streams (id, youtube_url, title, date, duration_minutes, hands_estimated) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (id) DO UPDATE SET "
            "youtube_url = COALESCE(streams.youtube_url, excluded.youtube_url), "
            "title = COALESCE(streams.title, excluded.title), "
            "date = COALESCE(streams.date, excluded.date), "
            "duration_minutes = COALESCE(streams.duration_minutes, excluded.duration_minutes), "
            "hands_estimated = COALESCE(streams.hands_estimated, excluded.hands_estimated)",
            (stream_id, meta.get("youtubeUrl"), meta.get("title"), meta.get("date"),
             meta.get("durationMinutes"), meta.get("handsEstimated")),
        )
    conn.execute("INSERT INTO streams (id, is_complete) VALUES (?,0) ON CONFLICT DO NOTHING", (stream_id,))
    conn.execute(
        "UPDATE streams SET is_complete = ?, completed_at = ? WHERE id = ?",
        (1 if complete else 0, now_iso() if complete else None, stream_id),
    )
    conn.commit()
    # Per-user bonus from this stream (for feedback).
    shares = conn.execute(
        f"SELECT user_id, COUNT(*) AS hands FROM hands WHERE stream_id = ? AND {HAND_ROW} GROUP BY user_id",
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
        errors, _hands, rate = _user_errors(conn, u["id"], bd["hands"])
        users.append({
            **public_user(u),
            "hands": bd["hands"], "earnings": bd["total"], "base": bd["base"],
            "stream_bonus": bd["stream_bonus"], "month_bonus": bd["month_bonus"],
            "month_bonus_detail": bd["month_bonus_detail"],
            "error_count": errors, "error_rate": round(rate, 4),
            "paid": bd["paid"], "owed": bd["owed"],
            "last_active": last or u["created_at"],
        })
        # Platform totals are what workers are owed; an admin's own test hands
        # aren't wages (they used to inflate "Earnings owed").
        if not u["is_admin"]:
            total_earned += bd["total"]
            total_paid += bd["paid"]

    # Contributors for every stream in one query (was one query per stream).
    contributors = {}
    for c in conn.execute(
        "SELECT h.stream_id AS stream_id, u.username AS username, COUNT(*) AS hands FROM hands h "
        f"JOIN users u ON u.id = h.user_id WHERE h.stream_id IS NOT NULL AND {H_HAND_ROW} "
        "GROUP BY h.stream_id, h.user_id, u.username ORDER BY hands DESC, u.username"
    ).fetchall():
        contributors.setdefault(c["stream_id"], []).append({"username": c["username"], "hands": c["hands"]})
    streams = []
    for s in conn.execute(
        f"SELECT s.*, (SELECT COUNT(*) FROM hands h WHERE h.stream_id = s.id AND {H_HAND_ROW}) AS hands_done "
        "FROM streams s WHERE s.is_complete = 1 OR "
        f"(SELECT COUNT(*) FROM hands h WHERE h.stream_id = s.id AND {H_HAND_ROW}) > 0 ORDER BY s.date DESC"
    ).fetchall():
        streams.append({
            "id": s["id"], "title": s["title"], "date": s["date"],
            "youtube_url": s["youtube_url"], "is_complete": bool(s["is_complete"]),
            "completed_at": s["completed_at"], "hands_done": s["hands_done"],
            "contributors": contributors.get(s["id"], []),
        })

    total_hands = conn.execute("SELECT COUNT(*) AS n FROM hands WHERE COALESCE(type,'hand') = 'hand'").fetchone()["n"]
    conn.close()
    platform = {
        "users": len(users),
        "hands": total_hands,
        "earned": round(total_earned, 2),
        "paid": round(total_paid, 2),
        "owed": round(total_earned - total_paid, 2),
    }
    return {"users": users, "streams": streams, "platform": platform, "months": month_list(admin=True)}


def admin_user_detail(user_id):
    u = get_user(user_id)
    if not u:
        return None
    conn = get_db()
    bd = _earnings_breakdown(conn, user_id)
    # No pt4_text in the list: with thousands of hands it made every open (and
    # every payment saved from the drawer) a multi-MB response. The UI fetches a
    # hand's text from /api/admin/hand/<id> when it's expanded.
    hands = conn.execute(
        "SELECT id, stream_id, youtube_url, timestamp_seconds, cards_count, "
        "actions_count, earnings, created_at, "
        "CASE WHEN pt4_text IS NULL OR pt4_text = '' THEN 0 ELSE 1 END AS has_text "
        "FROM hands WHERE user_id = ? "
        "AND COALESCE(type,'hand') = 'hand' ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    pays = conn.execute(
        "SELECT id, amount, date, note, created_at FROM payments WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()
    errors, _h, rate = _user_errors(conn, user_id)
    conn.close()
    return {
        "user": {**public_user(u), "error_count": errors, "error_rate": round(rate, 4)},
        "stats": bd,
        "hands": [dict(h) for h in hands],
        "payments": [dict(p) for p in pays],
    }


def admin_hand(hand_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, user_id, stream_id, youtube_url, timestamp_seconds, pt4_text FROM hands WHERE id = ?",
        (hand_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def record_payment(user_id, amount, date, note):
    conn = get_db()
    conn.execute(
        "INSERT INTO payments (user_id, amount, date, note, created_at) VALUES (?,?,?,?,?)",
        (user_id, round(float(amount), 2), date or now_iso()[:10], note or "", now_iso()),
    )
    conn.commit()
    conn.close()


def mark_duplicate_hands(conn=None):
    """Mark second copies of an already-saved hand as type 'duplicate': the same
    worker, stream, timestamp and identical hand-history text, keeping the
    first. Duplicates stay in the table but are never paid, counted or exported.
    Idempotent; run on every boot. Undo with
    UPDATE hands SET type = 'hand' WHERE type = 'duplicate'."""
    own = conn is None
    conn = conn or get_db()
    n = conn.execute(
        f"UPDATE hands SET type = 'duplicate' WHERE {HAND_ROW} "
        "AND pt4_text IS NOT NULL AND pt4_text != '' AND EXISTS ("
        "SELECT 1 FROM hands o WHERE o.user_id = hands.user_id AND o.stream_id = hands.stream_id "
        "AND o.timestamp_seconds = hands.timestamp_seconds AND o.id < hands.id "
        "AND COALESCE(o.type,'hand') = 'hand' AND o.pt4_text = hands.pt4_text)"
    ).rowcount
    conn.commit()
    if own:
        conn.close()
    return n


def earned_through(user_id, through):
    """What a worker had earned by the end of `through` (an Eastern-time date),
    and how that compares with everything paid so far.

    Counts pay for every hand saved by then, the tutorial credit if given by
    then, the stream bonus on those hands for streams marked complete by then,
    and month bonuses for months completed by then. Duplicates never count.
    """
    cutoff = end_of_day_utc(through)
    conn = get_db()
    rows = conn.execute(
        "SELECT COALESCE(h.type,'hand') AS type, h.cards_count AS cards, h.actions_count AS actions, "
        "COALESCE(h.side_payouts,0) AS side_payouts, "
        "h.earnings AS earnings, h.created_at AS created_at, s.is_complete AS done, s.completed_at AS done_at "
        "FROM hands h LEFT JOIN streams s ON s.id = h.stream_id "
        "WHERE h.user_id = ? AND COALESCE(h.type,'hand') IN ('hand','tutorial_bonus')",
        (user_id,),
    ).fetchall()
    hands = cards = actions = bonus_hands = side_payouts = 0
    tutorial = 0.0
    for r in rows:
        t = _parse_ts(r["created_at"])
        if not t or t >= cutoff:
            continue
        if r["type"] == "tutorial_bonus":
            tutorial += r["earnings"] or 0
            continue
        hands += 1
        cards += r["cards"] or 0
        actions += r["actions"] or 0
        side_payouts += r["side_payouts"] or 0
        if r["done"]:
            done_at = _parse_ts(r["done_at"])
            if done_at is None or done_at < cutoff:
                bonus_hands += 1
    base = round(round(cards * PIECE_RATE, 2) + round(actions * PIECE_RATE, 2) + round(hands * COMPLETION_BONUS, 2)
                 + round(side_payouts * SIDE_PAYOUT_BONUS, 2), 2)
    stream_bonus = round(bonus_hands * STREAM_BONUS_PER_HAND, 2)
    month_bonus = round(sum(
        m["amount"] for m in _month_bonuses(conn, user_id)
        if m["completed_at"] and _parse_ts(m["completed_at"]) < cutoff
    ), 2)
    paid = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS p FROM payments WHERE user_id = ?", (user_id,)
    ).fetchone()["p"]
    conn.close()
    earned = round(base + stream_bonus + month_bonus + tutorial, 2)
    return {
        "through": str(through)[:10], "hands": hands, "base": base,
        "stream_bonus": stream_bonus, "month_bonus": month_bonus,
        "tutorial_bonus": round(tutorial, 2), "earned": earned,
        "paid": round(paid, 2), "to_pay": round(earned - paid, 2),
    }


def square_up(user_id, through):
    """Record that a worker is paid in full through `through` (Eastern date):
    logs one payment for whatever earned-through-then exceeds everything paid,
    so the balance left owing is exactly the work after that day. Logs nothing
    if they're already paid that far. Returns earned_through()'s figures."""
    s = earned_through(user_id, through)
    if s["to_pay"] > 0:
        d = date.fromisoformat(s["through"])
        record_payment(user_id, s["to_pay"], s["through"], f"Square-up: paid in full through {d:%a} {s['through']}")
    return s


def export_stream_text(stream_id):
    """All hands for one stream (any worker), PT4 text concatenated in timestamp
    order with two blank lines between — for the admin calendar's per-stream export."""
    sid = extract_video_id(stream_id) or (stream_id or "")
    conn = get_db()
    rows = conn.execute(
        "SELECT pt4_text FROM hands WHERE stream_id = ? AND COALESCE(type,'hand') = 'hand' "
        "ORDER BY timestamp_seconds, id", (sid,)
    ).fetchall()
    conn.close()
    return "\n\n\n".join((r["pt4_text"] or "").strip() for r in rows if r["pt4_text"]) + "\n"


def export_all_text():
    """Every stored hand as RAW PT4 text -- hand after hand, two blank lines
    between, with no headers, metadata, or indentation, so the .txt imports
    directly via PokerTracker's File -> Import Hand Histories."""
    conn = get_db()
    rows = conn.execute(
        "SELECT pt4_text FROM hands WHERE COALESCE(type,'hand') = 'hand' "
        "AND pt4_text IS NOT NULL AND pt4_text != '' "
        "ORDER BY stream_id, timestamp_seconds, id"
    ).fetchall()
    conn.close()
    return "\n\n\n".join(r["pt4_text"].strip() for r in rows) + "\n"


def export_user_text(user_id):
    """All of one worker's completed hands as RAW PT4 text, two blank lines
    between, sorted by stream date then timestamp — for the admin per-user export."""
    conn = get_db()
    rows = conn.execute(
        "SELECT h.pt4_text AS pt4_text FROM hands h "
        "LEFT JOIN streams s ON h.stream_id = s.id "
        "WHERE h.user_id = ? AND COALESCE(h.type,'hand') = 'hand' "
        "AND h.pt4_text IS NOT NULL AND h.pt4_text != '' "
        "ORDER BY s.date, h.timestamp_seconds, h.id", (user_id,)
    ).fetchall()
    conn.close()
    return "\n\n\n".join(r["pt4_text"].strip() for r in rows) + "\n"
