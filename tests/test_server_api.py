"""API tests against a throwaway SQLite database: auth, pay integrity, catalog
writes, admin endpoints. Run with the rest: py -3 -m pytest tests/"""
import importlib
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture(scope="module")
def app_env():
    tmp = tempfile.mkdtemp()
    os.environ.update({"RAILWAY_VOLUME_MOUNT_PATH": tmp, "STREAM_REFRESH": "0",
                       "ADMIN_PASSWORD": "admin-test-pw", "SECRET_KEY": "test"})
    os.environ.pop("DATABASE_URL", None)
    import auth_db
    import server
    importlib.reload(auth_db)   # pick up the temp data dir even if imported earlier
    importlib.reload(server)
    server.app.config["TESTING"] = True
    yield server, auth_db
    shutil.rmtree(tmp, ignore_errors=True)


def login(client, username, password):
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.get_json()
    return r


@pytest.fixture
def worker(app_env):
    server, auth_db = app_env
    name = f"w{os.urandom(3).hex()}"
    auth_db.create_user(name, "", "pw")
    c = server.app.test_client()
    login(c, name, "pw")
    return c


@pytest.fixture
def admin(app_env):
    server, _ = app_env
    c = server.app.test_client()
    login(c, "admin", "admin-test-pw")
    return c


HAND = {
    "game": {"stakes": "50/100"}, "timestamp_start": "00:20:00", "button_seat": 1,
    "players": [{"name": "A", "seat": 1, "stack": 10000, "position": "BTN"},
                {"name": "B", "seat": 2, "stack": 10000, "position": "SB"},
                {"name": "C", "seat": 3, "stack": 10000, "position": "BB"}],
    "action": {"preflop": [
        {"player": "B", "action": "posts_sb", "amount": 50},
        {"player": "C", "action": "posts_bb", "amount": 100},
        {"player": "A", "action": "raises", "amount": 300},
        {"player": "B", "action": "folds"},
        {"player": "C", "action": "folds"},
    ], "flop": [], "turn": [], "river": []},
    "board": {}, "showdown": [], "winner": "A",
}


def submit(client, **over):
    body = {"hand": HAND, "stream_id": "AbCdEfGhIjK", "youtube_url": "https://youtu.be/AbCdEfGhIjK?t=60",
            "timestamp_seconds": 60, "cards_count": 0, "actions_count": 3, "stream_url": "x"}
    body.update(over)
    return client.post("/api/hands/submit", json=body)


# ── Auth ─────────────────────────────────────────────────────────────────────
def test_scraper_endpoints_need_admin(app_env, worker):
    server, _ = app_env
    anon = server.app.test_client()
    for method, path in (("post", "/api/run"), ("post", "/api/stop"), ("get", "/api/download"), ("get", "/api/status")):
        assert getattr(anon, method)(path).status_code == 403, path
        assert getattr(worker, method)(path).status_code == 403, path


def test_unknown_api_path_is_json_404(app_env):
    server, _ = app_env
    r = server.app.test_client().get("/api/no-such-thing")
    assert r.status_code == 404 and r.get_json() == {"error": "Not found."}


def test_ui_cookie_picks_the_page(app_env):
    server, _ = app_env
    if not os.path.exists(os.path.join(server._DIST, "classic.html")):
        pytest.skip("frontend not built")
    c = server.app.test_client()
    assert b"(Classic)" in c.get("/").data              # no cookie: UI_DEFAULT (the original)
    c.set_cookie("ui", "classic")
    r = c.get("/calendar")
    assert b"Classic" in r.data and r.headers["Cache-Control"] == "no-store"
    c.set_cookie("ui", "new")
    assert b"(Classic)" not in c.get("/").data


# ── Pay integrity ────────────────────────────────────────────────────────────
def test_honest_submission_is_paid_as_sent(worker):
    r = submit(worker, cards_count=4, actions_count=3).get_json()
    # 3 non-post actions in the hand, 4 cards (well under 2x3 players + 5 board)
    assert r["earnings"] == round((4 + 3) * 0.03 + 0.10, 2)


def test_inflated_counts_are_capped(worker):
    r = submit(worker, cards_count=100000, actions_count=5000, timestamp_seconds=61).get_json()
    # caps: 2*3 + 5 = 11 cards, 3 actions
    assert r["earnings"] == round((11 + 3) * 0.03 + 0.10, 2)


def test_negative_counts_are_zero(worker):
    r = submit(worker, cards_count=-500, actions_count=-500, timestamp_seconds=62).get_json()
    assert r["earnings"] == 0.10


def test_side_game_record_has_no_pieces_or_text(worker, app_env):
    _, auth_db = app_env
    r = worker.post("/api/hands/submit", json={
        "stream_id": "AbCdEfGhIjK", "youtube_url": "https://youtu.be/AbCdEfGhIjK?t=70", "timestamp_seconds": 70,
        "pt4_text": "PokerStars Hand #1: forged", "cards_count": 999, "actions_count": 999,
    }).get_json()
    assert r["earnings"] == 0.10
    conn = auth_db.get_db()
    row = conn.execute("SELECT pt4_text, cards_count, actions_count FROM hands WHERE id = ?", (r["hand_id"],)).fetchone()
    conn.close()
    assert (row["pt4_text"], row["cards_count"], row["actions_count"]) == ("", 0, 0)


# ── Catalog ──────────────────────────────────────────────────────────────────
def test_upsert_stream_inserts_and_updates(app_env):
    _, auth_db = app_env
    auth_db.upsert_stream({"id": "UpsertTest1", "title": "T", "date": "2026-10-01", "durationMinutes": 300, "handsEstimated": 100})
    auth_db.upsert_stream({"id": "UpsertTest1", "title": "Renamed"})
    m = auth_db.stream_meta("UpsertTest1")
    assert (m["title"], m["date"], m["hands_estimated"]) == ("Renamed", "2026-10-01", 100)


def test_mark_complete_fills_but_never_overwrites(worker, app_env):
    _, auth_db = app_env
    auth_db.upsert_stream({"id": "FillOnly001", "title": "Real title", "date": "2026-09-22"})
    r = worker.post("/api/streams/complete", json={"id": "FillOnly001", "complete": True,
                                                   "title": "Builder label", "date": "2030-01-01", "durationMinutes": 333})
    assert r.status_code == 200
    m = auth_db.stream_meta("FillOnly001")
    assert (m["title"], m["date"], m["duration_minutes"], m["is_complete"]) == ("Real title", "2026-09-22", 333, 1)


# ── Admin ────────────────────────────────────────────────────────────────────
def test_payment_rejects_nan_and_unknown_user(admin):
    assert admin.post("/api/admin/payment", json={"user_id": 1, "amount": "nan"}).status_code == 400
    assert admin.post("/api/admin/payment", json={"user_id": 99999, "amount": 5}).status_code == 404


def test_user_detail_is_lean_and_hand_text_is_on_demand(admin, worker, app_env):
    _, auth_db = app_env
    hid = submit(worker, timestamp_seconds=90).get_json()["hand_id"]
    uid = auth_db.get_db().execute("SELECT user_id FROM hands WHERE id = ?", (hid,)).fetchone()["user_id"]
    det = admin.get(f"/api/admin/user/{uid}").get_json()
    h = next(x for x in det["hands"] if x["id"] == hid)
    assert "pt4_text" not in h and h["has_text"] == 1
    text = admin.get(f"/api/admin/hand/{hid}").get_json()["pt4_text"]
    assert text.startswith("PokerStars Hand #")
    assert worker.get(f"/api/admin/hand/{hid}").status_code == 403


def test_tutorial_bonus_is_not_a_stream(worker):
    worker.post("/api/tutorial-complete", json={"completed": True, "award": True})
    d = worker.get("/api/me").get_json()["dashboard"]
    assert all(s["stream_id"] for s in d["streams_worked"])
