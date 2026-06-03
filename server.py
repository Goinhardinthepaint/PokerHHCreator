"""
Poker Scraper — Web API Server

Run: python server.py
Serves at http://localhost:8000

Endpoints:
  POST /api/run     — Start the pipeline, streams progress events as NDJSON
  POST /api/stop    — Stop the running pipeline
  GET  /api/download — Download the latest hands.txt
"""

import json
import os
import sys
import threading
import time
import queue
from functools import wraps
from flask import Flask, request, Response, send_file, jsonify, send_from_directory, session
from flask_cors import CORS

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Only the hand-builder formatter/evaluator are imported eagerly — they're
# stdlib-only. The heavy scraper deps (opencv, yt-dlp, …) are imported lazily
# inside the pipeline functions so the deployed app needs only flask/flask-cors.
from src.export.pt4_formatter import format_hand, HandValidationError
from src.export.hand_evaluator import evaluate_best_hand
import auth_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,  # 30 days
)
# supports_credentials so the session cookie rides along with fetch(credentials).
CORS(app, supports_credentials=True)

# Create tables, bootstrap the admin account, seed the stream catalog.
auth_db.init_db()


# ── Auth helpers ─────────────────────────────────────────────────────────────
def current_user():
    uid = session.get("user_id")
    return auth_db.get_user(uid) if uid else None


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Login required."}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or not u["is_admin"]:
            return jsonify({"error": "Admin access only."}), 403
        return f(*args, **kwargs)
    return wrapper


def _me_payload(uid):
    u = auth_db.get_user(uid)
    return {"user": auth_db.public_user(u), "dashboard": auth_db.user_dashboard(uid)}

# Global state
_event_queue = queue.Queue()
_stop_flag = threading.Event()
_running = False


def _emit(event_type, **kwargs):
    """Push an event to the SSE queue."""
    _event_queue.put(json.dumps({"type": event_type, **kwargs}))


def _ts_to_sec(ts):
    try:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0


def _scan_transcript_side_games(vtt_path, start_sec, end_sec):
    from src.transcript.parser import parse_vtt
    from src.video.detector import is_side_game

    side_game_times = set()
    try:
        segments = parse_vtt(vtt_path)
    except Exception:
        return side_game_times
    for seg in segments:
        seg_s = _ts_to_sec(seg.start)
        in_window = (
            (start_sec is None or seg_s >= start_sec - 60)
            and (end_sec is None or seg_s <= end_sec + 60)
        )
        if in_window and is_side_game(seg.text):
            side_game_times.add(int(seg_s))
    return side_game_times


def _run_pipeline(params):
    """Run the full pipeline in a background thread, emitting events."""
    # Heavy scraper deps loaded only when the scraper actually runs.
    from src.ingest.youtube import ingest_youtube
    from src.video.extractor import extract_frames
    from src.video.pipeline import VideoPipeline, hand_states_to_dicts
    from src.vision.analyzer import FrameAnalyzer
    from src.validate.validator import validate_session
    from src.export.pt4_formatter import format_session

    global _running
    _running = True
    _stop_flag.clear()

    try:
        url = params["url"]
        bb_ante = params.get("bb_ante", 0)
        fps = params.get("fps", 1.0)
        start_sec = params.get("start_sec")
        end_sec = params.get("end_sec")
        vision_model = params.get("vision_model", "claude")
        model_id = {
            "claude": "claude-sonnet-4-6",
            "openai": "gpt-4o",
            "openai-mini": "gpt-4o-mini",
        }.get(vision_model, "claude-sonnet-4-6")

        # ── Step 1: Download ──────────────────────────────────────────────
        _emit("phase", phase="downloading")
        _emit("log", message=f"Downloading from YouTube: {url}")

        ingest_result = ingest_youtube(
            url=url,
            output_dir="downloads",
            skip_video=False,
            start_sec=start_sec,
            end_sec=end_sec,
        )
        video_path = ingest_result.video_path
        transcript_path = ingest_result.transcript_path
        video_id = ingest_result.video_id

        if not video_path:
            _emit("error", message="Video download failed")
            return

        _emit("log", message=f"Video: {video_path}")
        _emit("log", message=f"Transcript: {transcript_path or 'none'}")

        # ── Step 2: Extract frames ────────────────────────────────────────
        _emit("phase", phase="extracting")

        window_tag = (
            f"{int(start_sec or 0)}-{int(end_sec or 0)}"
            if (start_sec is not None or end_sec is not None)
            else "full"
        )
        frames_dir = os.path.join("downloads", "frames", f"{video_id}_{window_tag}")
        time_offset = start_sec or 0.0
        duration = (end_sec - (start_sec or 0.0)) if end_sec else None

        frame_pairs = extract_frames(
            video_path=video_path,
            output_dir=frames_dir,
            fps=fps,
            time_offset_sec=time_offset,
            duration_sec=duration,
        )

        total_frames = len(frame_pairs)
        _emit("log", message=f"Extracted {total_frames} frames at {fps} FPS")

        if not frame_pairs:
            _emit("error", message="No frames extracted")
            return

        # ── Step 3: Analyze frames ────────────────────────────────────────
        _emit("phase", phase="analyzing")

        # TODO: swap analyzer based on vision_model param
        analyzer = FrameAnalyzer(model=model_id)
        frame_results = []

        for i, (img_path, ts) in enumerate(frame_pairs):
            if _stop_flag.is_set():
                _emit("log", message="Pipeline stopped by user", level="warn")
                return

            result = analyzer.analyze_frame(img_path, ts)
            frame_results.append(result)

            detail = ""
            if "error" not in result:
                board = result.get("board") or []
                pot = result.get("pot") or 0
                players = len(result.get("players") or [])
                detail = f"board={len(board)} pot={pot:,} players={players}"

            _emit("progress", current=i + 1, total=total_frames, detail=detail)

        # Cache frames for re-export
        cache_path = f"output/frames_cache_{video_id}_{window_tag}.json"
        os.makedirs("output", exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(frame_results, f)
        _emit("log", message=f"Cached frames to {cache_path}")

        # ── Step 4: Detect hands ──────────────────────────────────────────
        _emit("phase", phase="detecting")

        timestamps = [ts for _, ts in frame_pairs]

        # Side game filtering from transcript
        side_game_times = set()
        if transcript_path:
            side_game_times = _scan_transcript_side_games(
                transcript_path, start_sec, end_sec
            )

        vp = VideoPipeline()
        completed, skipped = vp.process(
            frame_results, timestamps,
            transcript_side_game_sec=side_game_times,
        )

        for h in completed:
            _emit("hand_detected", players=len(h.frames[0].get("players", [])) if h.frames else 0,
                   pot=h.frames[-1].get("pot", 0) if h.frames else 0)

        for s in skipped:
            _emit("hand_skipped", reason=s.side_game_type or "filtered")

        # Convert to hand dicts
        hands = hand_states_to_dicts(completed, stream_url=url, bb_ante=bb_ante)

        _emit("log", message=f"{len(hands)} hands converted")

        # ── Step 5: Validate and export ───────────────────────────────────
        _emit("phase", phase="formatting")

        issues = validate_session(hands)
        valid_hands = [
            h for i, h in enumerate(hands)
            if not any(idx == i and not r.valid for idx, r in issues)
        ]

        pt4_output = format_session(valid_hands, stream_url=url)
        output_path = "output/hands.txt"
        with open(output_path, "w") as f:
            f.write(pt4_output)

        _emit("log", message=f"Exported {len(valid_hands)} hands to {output_path}")
        _emit("complete", hands_exported=len(valid_hands), hands_skipped=len(skipped),
              output_path=output_path)

    except Exception as e:
        _emit("error", message=str(e))
    finally:
        _running = False


@app.route("/api/run", methods=["POST"])
def api_run():
    global _running
    if _running:
        return jsonify({"error": "Pipeline already running"}), 409

    params = request.json or {}

    # Start pipeline in background thread
    thread = threading.Thread(target=_run_pipeline, args=(params,), daemon=True)
    thread.start()

    # Stream events as NDJSON
    def generate():
        while _running or not _event_queue.empty():
            try:
                event = _event_queue.get(timeout=1)
                yield event + "\n"
            except queue.Empty:
                # Send keepalive
                yield "\n"

        # Drain remaining events
        while not _event_queue.empty():
            yield _event_queue.get() + "\n"

    return Response(generate(), mimetype="application/x-ndjson")


@app.route("/api/stop", methods=["POST"])
def api_stop():
    _stop_flag.set()
    return jsonify({"status": "stopping"})


@app.route("/api/download", methods=["GET"])
def api_download():
    path = "output/hands.txt"
    if os.path.exists(path):
        return send_file(path, as_attachment=True, download_name="hands.txt")
    return jsonify({"error": "No output file found"}), 404


def _full_board(run: dict) -> list:
    """Flatten a run/board dict {flop:[...], turn, river} into an ordered list."""
    cards = list(run.get("flop") or [])
    if run.get("turn"):
        cards.append(run["turn"])
    if run.get("river"):
        cards.append(run["river"])
    return [c for c in cards if c]


@app.route("/api/format", methods=["POST"])
def api_format():
    """Format a single manually-entered hand into PT4 text.

    Body: { "hand": <hand dict for format_hand>, "stream_url": str,
            "hand_index": int, "start_sec": int, "end_sec": int }
    stream_url + start_sec/end_sec feed the table name and hand id (e.g. a
    timestamped YouTube link). Auto-fills showdown hand descriptions.
    """
    data = request.json or {}
    hand = data.get("hand") or {}
    stream_url = data.get("stream_url", "")
    hand_index = data.get("hand_index", 0)
    start_sec = int(data.get("start_sec") or 0)
    end_sec = int(data.get("end_sec") or 0)

    if not hand.get("players"):
        return jsonify({"error": "Hand has no players"}), 400

    # ── Auto-fill hand descriptions for showdown entries ─────────────────────
    board = hand.get("board") or {}
    is_rit = bool(board.get("first_run") and board.get("second_run"))
    if is_rit:
        board_run1 = _full_board(board["first_run"])
        board_run2 = _full_board(board["second_run"])
    else:
        board_run1 = _full_board(board)
        board_run2 = board_run1

    for sd in hand.get("showdown") or []:
        cards = sd.get("hole_cards") or []
        if cards and not sd.get("hand_description"):
            b = board_run2 if sd.get("run") == 2 else board_run1
            _score, desc = evaluate_best_hand(cards, b)
            if desc and "insufficient" not in desc:
                sd["hand_description"] = desc

    try:
        text = format_hand(hand, stream_url, hand_index, start_sec, end_sec)
    except HandValidationError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 400

    return jsonify({"text": text})


@app.route("/api/evaluate", methods=["POST"])
def api_evaluate():
    """Determine the winning hand(s) from hole cards + board.

    Body: { "players": [{"name": str, "hole_cards": [str, str]}], "board": [str] }
    Returns: { "winner": name|null, "winners": [name],
               "descriptions": {name: "a pair of Kings"} }
    Winners are decided by hand strength — ties yield multiple winners.
    """
    data = request.json or {}
    players = data.get("players") or []
    board = data.get("board") or []

    results = []
    for p in players:
        name = p.get("name", "")
        hole = p.get("hole_cards") or []
        score, desc = evaluate_best_hand(hole, board)
        results.append({"name": name, "score": tuple(score), "description": desc})

    winners = []
    if results:
        best = max(r["score"] for r in results)
        winners = [r["name"] for r in results if r["score"] == best]

    return jsonify({
        "winner": winners[0] if winners else None,
        "winners": winners,
        "descriptions": {r["name"]: r["description"] for r in results},
    })


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({"running": _running})


# ── Accounts ─────────────────────────────────────────────────────────────────
@app.route("/api/register", methods=["POST"])
def api_register():
    d = request.json or {}
    try:
        u = auth_db.create_user(d.get("username"), d.get("email"), d.get("password"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    session["user_id"] = u["id"]
    session.permanent = True
    return jsonify(_me_payload(u["id"]))


@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.json or {}
    u = auth_db.verify_user(d.get("username"), d.get("password"))
    if not u:
        return jsonify({"error": "Invalid username or password."}), 401
    session["user_id"] = u["id"]
    session.permanent = True
    return jsonify(_me_payload(u["id"]))


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def api_me():
    uid = session.get("user_id")
    if not uid or not auth_db.get_user(uid):
        session.clear()
        return jsonify({"error": "Not authenticated."}), 401
    return jsonify(_me_payload(uid))


# ── Hands ────────────────────────────────────────────────────────────────────
@app.route("/api/hands/submit", methods=["POST"])
@login_required
def api_hands_submit():
    uid = session["user_id"]
    d = request.json or {}
    hand_id, earnings = auth_db.insert_hand(
        uid, d.get("stream_id"), d.get("youtube_url"), d.get("timestamp_seconds"),
        d.get("pt4_text"), d.get("cards_count"), d.get("actions_count"),
    )
    # Snapshot the state for the NEXT hand so anyone can resume this stream.
    next_state = d.get("next_state")
    sid = d.get("stream_id") or d.get("youtube_url")
    if next_state and sid:
        auth_db.set_resume_state(sid, next_state)
    return jsonify({"hand_id": hand_id, "earnings": earnings,
                    "dashboard": auth_db.user_dashboard(uid)})


@app.route("/api/hands", methods=["GET"])
@login_required
def api_hands_list():
    uid = session["user_id"]
    stream_id = request.args.get("stream_id")
    hands = auth_db.user_hands(uid, stream_id)
    payload = {"hands": hands}
    if stream_id:
        payload["stream"] = auth_db.stream_meta(stream_id)
        payload["resume_state"] = auth_db.get_resume_state(stream_id)
        payload["last_timestamp"] = max((h["timestamp_seconds"] or 0 for h in hands), default=None)
    return jsonify(payload)


@app.route("/api/hands/<int:hand_id>", methods=["DELETE"])
@login_required
def api_hands_delete(hand_id):
    ok = auth_db.delete_hand(hand_id, session["user_id"])
    if not ok:
        return jsonify({"error": "No such hand."}), 404
    return jsonify({"ok": True, "dashboard": auth_db.user_dashboard(session["user_id"])})


@app.route("/api/streams/<sid>/resume", methods=["GET"])
@login_required
def api_stream_resume(sid):
    return jsonify({
        "youtube_url": (auth_db.stream_meta(sid) or {}).get("youtube_url"),
        "resume_state": auth_db.get_resume_state(sid),
        "last_timestamp": auth_db.last_stream_timestamp(sid),
    })


# ── Streams (calendar) ───────────────────────────────────────────────────────
@app.route("/api/streams/state", methods=["GET"])
@login_required
def api_streams_state():
    return jsonify({"states": auth_db.stream_state()})


@app.route("/api/streams/complete", methods=["POST"])
@login_required
def api_streams_complete():
    d = request.json or {}
    sid = d.get("id") or d.get("stream_id")
    if not sid:
        return jsonify({"error": "stream id required"}), 400
    meta = {k: d.get(k) for k in ("youtubeUrl", "title", "date", "durationMinutes", "handsEstimated")}
    shares = auth_db.set_stream_complete(sid, bool(d.get("complete", True)), meta)
    return jsonify({"ok": True, "shares": shares,
                    "dashboard": auth_db.user_dashboard(session["user_id"])})


@app.route("/api/streams/<sid>/default", methods=["GET"])
@login_required
def api_get_default_lineup(sid):
    return jsonify({"lineup": auth_db.get_default_lineup(sid)})


@app.route("/api/streams/<sid>/default", methods=["POST"])
@login_required
def api_set_default_lineup(sid):
    lineup = (request.json or {}).get("lineup") or []
    auth_db.set_default_lineup(sid, lineup)
    return jsonify({"ok": True, "lineup": auth_db.get_default_lineup(sid)})


@app.route("/api/streams", methods=["POST"])
@login_required
def api_streams_add():
    d = request.json or {}
    if not d.get("id"):
        return jsonify({"error": "id required"}), 400
    auth_db.upsert_stream(d)
    return jsonify({"ok": True})


@app.route("/api/streams/import", methods=["POST"])
@login_required
def api_streams_import():
    rows = (request.json or {}).get("rows") or []
    n = 0
    for r in rows:
        if r.get("id"):
            auth_db.upsert_stream(r)
            n += 1
    return jsonify({"ok": True, "imported": n})


# ── Admin ────────────────────────────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@admin_required
def api_admin_users():
    return jsonify(auth_db.admin_overview())


@app.route("/api/admin/user/<int:uid>", methods=["GET"])
@admin_required
def api_admin_user(uid):
    det = auth_db.admin_user_detail(uid)
    if not det:
        return jsonify({"error": "No such user."}), 404
    return jsonify(det)


@app.route("/api/admin/payment", methods=["POST"])
@admin_required
def api_admin_payment():
    d = request.json or {}
    uid, amount = d.get("user_id"), d.get("amount")
    if not uid or amount is None:
        return jsonify({"error": "user_id and amount required"}), 400
    try:
        auth_db.record_payment(uid, amount, d.get("date"), d.get("note"))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    return jsonify(auth_db.admin_user_detail(uid))


@app.route("/api/admin/export", methods=["GET"])
@admin_required
def api_admin_export():
    text = auth_db.export_all_text()
    return Response(text, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=hand_database.txt"})


# ── Serve the built React frontend (single server, one port) ─────────────────
_DIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "dist")


@app.route("/")
def serve_frontend():
    return send_from_directory(_DIST, "index.html")


@app.route("/<path:path>")
def serve_static(path):
    # Serve the requested asset if it exists, else fall back to index.html
    # (so refreshes / deep links still load the app).
    full = os.path.join(_DIST, path)
    if os.path.isfile(full):
        return send_from_directory(_DIST, path)
    return send_from_directory(_DIST, "index.html")


if __name__ == "__main__":
    os.makedirs("output", exist_ok=True)
    os.makedirs("downloads", exist_ok=True)
    port = int(os.environ.get("PORT", 8000))  # Railway provides $PORT
    print(f"Starting Poker app on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
