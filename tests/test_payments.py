"""Worker pay: duplicate submissions, month bonuses, deadlines, square-ups and
the Eastern-time date helpers. Throwaway SQLite database per module."""
import importlib
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from test_server_api import HAND, login, submit  # noqa: E402


@pytest.fixture(scope="module")
def env():
    tmp = tempfile.mkdtemp()
    os.environ.update({"RAILWAY_VOLUME_MOUNT_PATH": tmp, "STREAM_REFRESH": "0",
                       "ADMIN_PASSWORD": "admin-test-pw", "SECRET_KEY": "test"})
    os.environ.pop("DATABASE_URL", None)
    import auth_db
    import server
    importlib.reload(auth_db)
    importlib.reload(server)
    server.app.config["TESTING"] = True
    yield server, auth_db
    shutil.rmtree(tmp, ignore_errors=True)


def new_worker(env):
    server, auth_db = env
    name = f"w{os.urandom(3).hex()}"
    uid = auth_db.create_user(name, "", "pw")
    if not isinstance(uid, int):
        uid = auth_db.get_db().execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()["id"]
    c = server.app.test_client()
    login(c, name, "pw")
    return uid, c


def sql(auth_db, q, params=()):
    conn = auth_db.get_db()
    cur = conn.execute(q, params)
    rows = cur.fetchall() if cur.description else None
    conn.commit()
    conn.close()
    return rows


def add_hand(auth_db, uid, stream, ts, created, text="HH", pieces=10):
    sql(auth_db, "INSERT INTO hands (user_id, stream_id, youtube_url, timestamp_seconds, pt4_text, cards_count, "
                 "actions_count, earnings, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, stream, "", ts, text, pieces, 0, auth_db._hand_earnings(pieces, 0), created))


# ── Eastern dates ────────────────────────────────────────────────────────────
def test_eastern_dates(env):
    _, auth_db = env
    assert auth_db.last_friday(date(2026, 10, 12)) == date(2026, 10, 9)   # Monday
    assert auth_db.last_friday(date(2026, 10, 10)) == date(2026, 10, 9)   # Saturday
    assert auth_db.last_friday(date(2026, 10, 9)) == date(2026, 10, 2)    # a Friday: the one before
    assert auth_db.end_of_day_utc("2026-10-09") == datetime(2026, 10, 10, 4, tzinfo=timezone.utc)   # EDT
    assert auth_db.end_of_day_utc("2026-12-04") == datetime(2026, 12, 5, 5, tzinfo=timezone.utc)    # EST
    assert auth_db.end_of_day_utc("2026-10-31") == datetime(2026, 11, 1, 4, tzinfo=timezone.utc)    # DST ends Nov 1


# ── Duplicates ───────────────────────────────────────────────────────────────
def test_double_submit_is_saved_and_paid_once(env):
    _, auth_db = env
    uid, c = new_worker(env)
    a = submit(c).get_json()
    b = submit(c).get_json()
    assert a["hand_id"] == b["hand_id"]
    assert b["dashboard"]["hands"] == 1
    # a different timestamp is a different hand
    assert submit(c, timestamp_seconds=61).get_json()["dashboard"]["hands"] == 2


def test_existing_duplicates_are_marked_not_paid(env):
    _, auth_db = env
    uid, _ = new_worker(env)
    for created in ("2026-10-01T10:00:00+00:00", "2026-10-01T10:00:01+00:00", "2026-10-01T15:00:00+00:00"):
        add_hand(auth_db, uid, "StreamAAAAA", 100, created, text="same hand")
    add_hand(auth_db, uid, "StreamAAAAA", 100, "2026-10-01T10:00:02+00:00", text="", pieces=0)  # side-game count
    add_hand(auth_db, uid, "StreamAAAAA", 200, "2026-10-01T10:05:00+00:00", text="same hand")    # other timestamp
    assert auth_db.mark_duplicate_hands() == 2
    assert auth_db.mark_duplicate_hands() == 0                  # idempotent
    d = auth_db.user_dashboard(uid)
    assert d["hands"] == 3
    assert auth_db.stream_state()["StreamAAAAA"]["handsCompleted"] == 3


# ── Month bonuses ────────────────────────────────────────────────────────────
def _complete_month(auth_db, uid, month, completed_at):
    sid = (month.replace("-", "") + "XXXXXXXX")[:11]
    auth_db.upsert_stream({"id": sid, "date": f"{month}-05", "title": month, "handsEstimated": 1})
    add_hand(auth_db, uid, sid, 5, f"{month}-06T12:00:00+00:00")
    auth_db.set_stream_complete(sid)
    sql(auth_db, "UPDATE streams SET completed_at = ? WHERE id = ?", (completed_at, sid))


def test_every_assigned_month_pays_its_bonus(env):
    _, auth_db = env
    uid, _ = new_worker(env)
    for m in ("2019-03", "2019-04"):
        _complete_month(auth_db, uid, m, f"{m}-20T12:00:00+00:00")
        auth_db.set_month_assignment(m, uid, 150, f"{m}-28")
    d = auth_db.user_dashboard(uid)
    assert d["month_bonus"] == 300.0
    assert [m["status"] for m in d["month_bonuses"]] == ["full", "full"]


def test_month_finished_after_deadline_pays_nothing(env):
    _, auth_db = env
    uid, _ = new_worker(env)
    _complete_month(auth_db, uid, "2019-05", "2019-06-02T12:00:00+00:00")
    auth_db.set_month_assignment("2019-05", uid, 150, "2019-05-31")
    mb = auth_db.user_dashboard(uid)["month_bonus_detail"]
    assert (mb["status"], mb["amount"]) == ("late", 0.0)
    # the same work on time pays
    auth_db.set_month_assignment("2019-05", uid, 150, "2019-06-02")
    assert auth_db.user_dashboard(uid)["month_bonus_detail"]["status"] == "full"


# ── Square-up ────────────────────────────────────────────────────────────────
def test_square_up_through_friday_leaves_only_later_work_owed(env):
    _, auth_db = env
    uid, _ = new_worker(env)
    add_hand(auth_db, uid, "StreamBBBBB", 1, "2026-10-09T20:00:00+00:00", pieces=10)   # Fri 4pm ET
    add_hand(auth_db, uid, "StreamBBBBB", 2, "2026-10-10T03:30:00+00:00", pieces=20)   # Fri 11:30pm ET
    add_hand(auth_db, uid, "StreamBBBBB", 3, "2026-10-10T04:30:00+00:00", pieces=30)   # Sat 12:30am ET
    auth_db.record_payment(uid, 0.25, "2026-10-01", "earlier")
    s = auth_db.square_up(uid, date(2026, 10, 9))
    assert s["hands"] == 2 and s["earned"] == round(0.40 + 0.70, 2) and s["to_pay"] == round(1.10 - 0.25, 2)
    d = auth_db.user_dashboard(uid)
    assert d["owed"] == 1.00                                   # just Saturday's hand: 30 x .03 + .10
    assert auth_db.square_up(uid, date(2026, 10, 9))["to_pay"] == 0   # nothing more to log
    pays = sql(auth_db, "SELECT amount, date, note FROM payments WHERE user_id = ? ORDER BY id", (uid,))
    assert [tuple(p) for p in pays][-1] == (0.85, "2026-10-09", "Square-up: paid in full through Fri 2026-10-09")


def test_square_up_endpoint(env):
    server, auth_db = env
    uid, _ = new_worker(env)
    add_hand(auth_db, uid, "StreamCCCCC", 1, "2026-01-02T15:00:00+00:00", pieces=10)
    a = server.app.test_client()
    login(a, "admin", "admin-test-pw")
    pre = a.get(f"/api/admin/user/{uid}/square-up?through=2026-01-02").get_json()
    assert pre["to_pay"] == 0.40
    assert a.get(f"/api/admin/user/{uid}/square-up?through=2999-01-01").status_code == 400
    assert a.get(f"/api/admin/user/{uid}/square-up?through=nope").status_code == 400
    assert a.get(f"/api/admin/user/{uid}/square-up").get_json()["through"] == str(auth_db.last_friday())
    r = a.post(f"/api/admin/user/{uid}/square-up", json={"through": "2026-01-02"}).get_json()
    assert r["stats"]["owed"] == 0 and r["square_up"]["to_pay"] == 0.40
    _, w = new_worker(env)
    assert w.get(f"/api/admin/user/{uid}/square-up").status_code in (401, 403)


def test_platform_owed_leaves_out_admin_test_hands(env):
    server, auth_db = env
    a = server.app.test_client()
    login(a, "admin", "admin-test-pw")
    assert submit(a, timestamp_seconds=999).status_code == 200
    data = a.get("/api/admin/users").get_json()
    workers = [u for u in data["users"] if not u["is_admin"]]
    assert data["platform"]["earned"] == round(sum(u["earnings"] for u in workers), 2)
    assert any(u["is_admin"] and u["earnings"] > 0 for u in data["users"])


# ── $0.25 per side-game payout ───────────────────────────────────────────────
BOUNTY_PAID = {
    "levels": {"3": 300, "4": 400, "5": 500}, "step": 100,
    "rows": [{"seat": 1, "base": "A", "before": 2, "after": 3, "result": "won"},
             {"seat": 2, "base": "B", "before": 0, "after": 0, "result": "lost"},
             {"seat": 3, "base": "C", "before": 0, "after": 0, "result": "lost"}],
    "payouts": [{"payer_seat": 2, "payer": "B", "winner_seat": 1, "winner": "A", "amount": 300, "streak": 3},
                {"payer_seat": 3, "payer": "C", "winner_seat": 1, "winner": "A", "amount": 300, "streak": 3}],
}
SQUID_SETTLED = {
    "round": 1, "total": 2, "value": 200, "mode": "progressive", "left_before": 1, "left_after": 0,
    "rows": [{"seat": 1, "base": "A", "before": 1, "after": 2, "won": True},
             {"seat": 2, "base": "B", "before": 0, "after": 0, "won": False},
             {"seat": 3, "base": "C", "before": 0, "after": 0, "won": False}],
    "settled": True,
    "payouts": [{"payer_seat": 2, "payer": "B", "holder_seat": 1, "holder": "A", "amount": 400, "buttons": 2},
                {"payer_seat": 3, "payer": "C", "holder_seat": 1, "holder": "A", "amount": 400, "buttons": 2}],
}


def _side_hand(side_game):
    return {**HAND, "side_game": side_game}


def test_each_side_game_payout_pays_the_worker_a_quarter(env):
    _, auth_db = env
    uid, c = new_worker(env)
    plain = submit(c, timestamp_seconds=10).get_json()["earnings"]
    bounty = submit(c, hand=_side_hand({"bounty": BOUNTY_PAID}), timestamp_seconds=11).get_json()
    both = submit(c, hand=_side_hand({"bounty": BOUNTY_PAID, "squid": SQUID_SETTLED}), timestamp_seconds=12).get_json()
    unsettled = {**SQUID_SETTLED, "settled": False, "payouts": [], "left_after": 1}
    quiet = submit(c, hand=_side_hand({"squid": unsettled}), timestamp_seconds=13).get_json()
    assert bounty["earnings"] == round(plain + 0.25, 2)
    assert both["earnings"] == round(plain + 0.50, 2)
    assert quiet["earnings"] == plain
    d = quiet["dashboard"]
    assert d["side_payouts"] == 3 and d["side_game_bonus"] == 0.75
    assert d["total"] == round(4 * plain + 0.75 + d["tutorial_bonus"], 2)
    # a count the browser claims is ignored
    claimed = submit(c, side_payouts=9, timestamp_seconds=14).get_json()
    assert claimed["earnings"] == plain
    # and the square-up counts it
    assert auth_db.earned_through(uid, date(2999, 1, 1))["earned"] == round(d["total"] + plain, 2)


SEVEN_TWO_PAID = {
    "value": 200,
    "rows": [{"seat": 2, "base": "B", "cards": ["7h", "2c"], "result": "won"}],
    "payouts": [{"payer_seat": 1, "payer": "A", "winner_seat": 2, "winner": "B", "amount": 200},
                {"payer_seat": 3, "payer": "C", "winner_seat": 2, "winner": "B", "amount": 200}],
}


def test_seven_two_lines_and_the_payout_quarter(env):
    _, auth_db = env
    from src.export.side_game import side_game_lines
    assert side_game_lines({"seven_two": SEVEN_TWO_PAID}) == [
        "*** SIDE GAME ***",
        "7-2 game: $200 from each player dealt in",
        "7-2 hand: Seat 2 (B) [7h 2c] won",
        "7-2 payout: Seat 1 (A) pays Seat 2 (B) $200 for 7-2",
        "7-2 payout: Seat 3 (A) pays Seat 2 (B) $200 for 7-2".replace("(A)", "(C)"),
    ]
    assert side_game_lines({"seven_two": {"value": 200, "rows": [], "payouts": []}}) == []   # no 7-2 dealt
    uid, c = new_worker(env)
    plain = submit(c, timestamp_seconds=20).get_json()["earnings"]
    paid = submit(c, hand=_side_hand({"seven_two": SEVEN_TWO_PAID}), timestamp_seconds=21).get_json()["earnings"]
    lost = {**SEVEN_TWO_PAID, "rows": [{**SEVEN_TWO_PAID["rows"][0], "result": "lost"}], "payouts": []}
    quiet = submit(c, hand=_side_hand({"seven_two": lost}), timestamp_seconds=22).get_json()["earnings"]
    assert paid == round(plain + 0.25, 2) and quiet == plain


def test_a_credit_counts_in_totals_and_square_ups_from_its_date(env):
    _, auth_db = env
    uid, _ = new_worker(env)
    add_hand(auth_db, uid, "StreamDDDDD", 1, "2026-10-08T20:00:00+00:00", pieces=10)          # $0.40
    auth_db.add_credit(uid, 5.50, "duplicates already paid", "2026-10-09T05:00:00+00:00")
    auth_db.add_credit(uid, 2.00, "later credit", "2026-10-12T15:00:00+00:00")
    d = auth_db.user_dashboard(uid)
    assert d["credits"] == 7.50 and d["total"] == round(0.40 + 7.50, 2) and d["hands"] == 1
    assert [c["note"] for c in d["credit_items"]] == ["duplicates already paid", "later credit"]
    s = auth_db.earned_through(uid, date(2026, 10, 9))
    assert s["credits"] == 5.50 and s["earned"] == 5.90
