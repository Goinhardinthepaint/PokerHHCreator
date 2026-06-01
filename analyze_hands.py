"""
One-call-per-hand analysis pipeline.

Uses cached frame data (from a prior per-frame run) for boundary detection,
then calls Claude once per hand with a stitched frame grid + transcript slice
to reconstruct the full hand history.

Usage:
    python analyze_hands.py output/frames_cache_p0Q7LW64ecM_2300-4100.json \\
        --bb-ante 100 \\
        --transcript downloads/p0Q7LW64ecM.en.vtt \\
        --limit 3
"""

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.video.pipeline import VideoPipeline
from src.video.detector import classify_boundary
from src.transcript.parser import parse_vtt, _ts_to_seconds
from src.vision.hand_analyzer import HandAnalyzer
from src.vision.hand_feeder import feed_hand, FeedError
from src.export.pt4_formatter import format_hand, HandValidationError


def _ensure_api_key(name: str) -> None:
    """If an API key is absent from the process env, read it from the Windows user env."""
    if os.environ.get(name):
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            if value:
                os.environ[name] = value
    except Exception:
        pass


def _ts_to_sec(ts: str) -> float:
    """Convert 'HH:MM:SS.mmm' to fractional seconds."""
    try:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0


def _count_players(hand_frames: list[dict]) -> int:
    """Return the number of distinct non-placeholder players seen in a hand."""
    _PLACEHOLDERS = {"unknown", "player", "hero", "villain", ""}
    names: set[str] = set()
    for f in hand_frames:
        if (f.get("confidence") or 0) < 0.5:
            continue
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip().casefold()
            if name and name not in _PLACEHOLDERS:
                names.add(name)
    return max(len(names), 2)


def _transcript_chunk(
    segs,
    hand_start_sec: float,
    hand_end_sec: float,
    pre_buf: float = 10.0,
    post_buf: float = 30.0,
) -> str:
    """Return VTT commentary lines for a hand's time window."""
    lines = []
    for seg in segs:
        seg_sec = _ts_to_seconds(seg.start)
        if hand_start_sec - pre_buf <= seg_sec <= hand_end_sec + post_buf:
            lines.append(f"[{seg.start}] {seg.text}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="One-call-per-hand analysis from cached frame data"
    )
    parser.add_argument(
        "cache_path",
        help="Path to frames_cache_*.json produced by main.py",
    )
    parser.add_argument(
        "--bb-ante",
        type=int,
        default=100,
        metavar="AMOUNT",
        dest="bb_ante",
    )
    parser.add_argument(
        "--transcript",
        default=None,
        metavar="PATH",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        metavar="N",
        help="Only process the first N hands (default 3 for testing)",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=1,
        metavar="N",
        help="Start processing from hand N (1-based, for resuming after failures)",
    )
    parser.add_argument(
        "--model",
        default="gpt4o",
        help="Model alias or ID: claude, gpt4o, gpt4o-mini, gpt45, or any OpenAI model ID (default: gpt4o)",
    )
    parser.add_argument(
        "--output",
        default="output/hands_onecall.txt",
        metavar="PATH",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to output file instead of overwriting",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=0,
        metavar="N",
        help="Max extra GPT retries per hand if validation fails (default 0)",
    )
    args = parser.parse_args()

    # ── API keys: fall back to Windows user-scope environment if not in process ─
    _ensure_api_key("ANTHROPIC_API_KEY")
    _ensure_api_key("OPENAI_API_KEY")

    # ── Load cache ─────────────────────────────────────────────────────────────
    print(f"Loading cache: {args.cache_path}")
    with open(args.cache_path) as fh:
        frame_results = json.load(fh)
    print(f"  {len(frame_results)} frames")

    # ── Derive frames directory from cache filename ────────────────────────────
    # Cache:      output/frames_cache_p0Q7LW64ecM_2300-4100.json
    # Frames dir: downloads/frames/p0Q7LW64ecM_2300-4100/
    cache_base = os.path.basename(args.cache_path)
    window_tag = cache_base.replace("frames_cache_", "").replace(".json", "")
    video_id   = window_tag.split("_")[0]
    frames_dir = os.path.join("downloads", "frames", window_tag)
    if not os.path.isdir(frames_dir):
        print(f"ERROR: frames directory not found: {frames_dir}")
        sys.exit(1)
    print(f"  Frames dir: {frames_dir}")

    # ── Compute real stream timestamps from window offset in filename ─────────
    # Cache filename: frames_cache_p0Q7LW64ecM_2300-4100.json
    # window_tag:     p0Q7LW64ecM_2300-4100  → start_sec = 2300
    window_start_sec = 0
    try:
        window_part = window_tag.split("_", 1)[1]   # "2300-4100"
        window_start_sec = int(window_part.split("-")[0])
    except Exception:
        pass  # non-windowed cache; timestamps start at 0

    timestamps = []
    for i in range(len(frame_results)):
        real_sec = window_start_sec + i
        h  = real_sec // 3600
        m  = (real_sec % 3600) // 60
        s  = real_sec % 60
        timestamps.append(f"{h:02d}:{m:02d}:{s:02d}.000")

    # Build reverse index: stream timestamp → frame index (0-based)
    ts_to_idx: dict[str, int] = {}
    for i, ts in enumerate(timestamps):
        if ts not in ts_to_idx:
            ts_to_idx[ts] = i

    # ── Load transcript ────────────────────────────────────────────────────────
    transcript_segs = []
    if args.transcript:
        transcript_segs = parse_vtt(args.transcript)
        print(f"  Transcript: {len(transcript_segs)} segments")

    # ── Boundary detection (existing pipeline, no API calls) ──────────────────
    print("\nDetecting hand boundaries...")
    vp = VideoPipeline()
    completed, skipped = vp.process(
        frame_results,
        timestamps,
        transcript_segments=transcript_segs or None,
    )
    print(f"  {len(completed)} hand(s) kept, {len(skipped)} skipped")

    if not completed:
        print("No hands detected.")
        sys.exit(0)

    # ── One-call-per-hand analysis ─────────────────────────────────────────────
    start_idx = max(0, args.start - 1)
    hands_to_process = completed[start_idx : start_idx + args.limit]
    print(f"\nAnalyzing hand(s) {args.start}–{args.start + len(hands_to_process) - 1} "
          f"of {len(completed)} with model: {args.model}...")

    analyzer = HandAnalyzer(model=args.model)
    all_hh: list[str] = []
    grids_dir = os.path.join("output", "grids")
    os.makedirs(grids_dir, exist_ok=True)

    for hand_num, hand in enumerate(hands_to_process, start=1):
        print(f"\n-- Hand {hand_num} / {len(hands_to_process)} "
              f"(starts {hand.timestamp_start}) --")

        # ── Map hand → frame file paths ────────────────────────────────────────
        start_idx   = ts_to_idx.get(hand.timestamp_start, 0)
        n_frames    = len(hand.frames)
        end_idx     = start_idx + n_frames - 1
        num_players = _count_players(hand.frames)
        ante_pp     = math.ceil(args.bb_ante / num_players)

        # ── Extend window forward if no board cards visible in cached frames ──
        # Preflop all-ins often run out after the hand window ends (next hand's
        # territory).  Scan up to 120 frames forward, stopping at a new hand
        # boundary (pot reset, board-clear debounce, or text cue).
        max_board_in_hand = max(
            (len((frame_results[start_idx + i] if start_idx + i < len(frame_results)
                  else {}).get("board") or [])
             for i in range(end_idx - start_idx + 1)),
            default=0,
        )
        if num_players >= 2 and max_board_in_hand == 0:
            print(f"  No board cards in window — scanning forward for runout...")
            max_scan    = min(end_idx + 121, len(frame_results))
            new_end_idx = end_idx
            board_seen         = False
            board_empty_streak = 0
            prev_had_board     = False
            prev_pot_ext = (frame_results[end_idx].get("pot") or 0) if end_idx < len(frame_results) else 0

            for ext_idx in range(end_idx + 1, max_scan):
                ext_frame = frame_results[ext_idx]
                ext_board_count = len(ext_frame.get("board") or [])
                ext_pot         = ext_frame.get("pot") or 0

                # Boundary: pot reset (winner collected, new hand starting).
                # Ignore pot=0 — it is a transient overlay-absent frame, not a
                # genuine collapse.  Only fire when the pot shows a small positive
                # value (e.g., next-hand antes).
                if prev_pot_ext > 2000 and 0 < ext_pot < prev_pot_ext * 0.20:
                    print(f"    Stopped +{ext_idx - end_idx} frames: pot reset "
                          f"({prev_pot_ext} -> {ext_pot})")
                    break

                # Boundary: explicit text cue in notes
                if classify_boundary(ext_frame.get("notes") or "").is_boundary:
                    print(f"    Stopped +{ext_idx - end_idx} frames: text boundary")
                    break

                if ext_board_count > 0:
                    board_empty_streak = 0
                    prev_had_board     = True
                    if ext_board_count >= 3:
                        board_seen  = True
                        new_end_idx = ext_idx
                else:
                    board_empty_streak += 1

                # Board appeared then cleared → runout captured, new hand boundary
                if prev_had_board and board_empty_streak == 3:
                    print(f"    Stopped +{ext_idx - end_idx} frames: board cleared after runout")
                    break

                if ext_pot > 0:
                    prev_pot_ext = ext_pot

            if board_seen and new_end_idx > end_idx:
                added = new_end_idx - end_idx
                print(f"  Extended window +{added} frames (idx {end_idx} -> {new_end_idx})")
                end_idx  = new_end_idx
                n_frames = end_idx - start_idx + 1
            else:
                print(f"  No runout found in forward scan — likely preflop fold")

        frame_paths: list[str] = []
        for idx in range(start_idx, end_idx + 1):
            path = os.path.join(frames_dir, f"frame_{idx + 1:06d}.jpg")
            if os.path.exists(path):
                frame_paths.append(path)

        print(f"  {n_frames} cached frames, {len(frame_paths)} JPEGs found "
              f"(indices {start_idx}–{end_idx})")

        if len(frame_paths) < 2:
            print("  SKIP: too few JPEG files found, skipping hand")
            continue

        # ── Transcript chunk ───────────────────────────────────────────────────
        hand_start_sec = _ts_to_sec(hand.timestamp_start)
        # Use computed timestamps list — frame dicts have "unknown" timestamps
        safe_end_idx = min(end_idx, len(timestamps) - 1)
        hand_end_sec = _ts_to_sec(timestamps[safe_end_idx])
        chunk = _transcript_chunk(
            transcript_segs, hand_start_sec, hand_end_sec
        )
        n_chunk_lines = chunk.count("\n") + 1 if chunk else 0
        print(f"  Transcript chunk: {n_chunk_lines} line(s) "
              f"({hand_start_sec:.0f}s – {hand_end_sec:.0f}s)")

        print(f"  Players: {num_players}, ante per player: ${ante_pp}")

        # ── Grid path ─────────────────────────────────────────────────────────
        grid_path = os.path.join(
            grids_dir, f"grid_{video_id}_hand{hand_num:03d}.jpg"
        )

        # ── Timestamp string for HH header ────────────────────────────────────
        now_str = datetime.now().strftime("%Y/%m/%d %H:%M:%S")

        # ── Debug: copy grid for first two hands ─────────────────────────────
        if hand_num <= 2:
            import shutil
            debug_path = os.path.join("output", f"debug_grid_hand{hand_num}.jpg")
            # Grid is written inside analyze_hand; copy after the call below.
            # Store path for post-call copy.
            _debug_grid_path = debug_path
        else:
            _debug_grid_path = None

        # ── Steps 1-3: call model → state machine → formatter (with retries) ─────
        max_attempts = 1 + args.retries
        hh_text = None

        for attempt in range(1, max_attempts + 1):
            attempt_label = (
                f"  Calling {args.model}" if attempt == 1
                else f"  Retry {attempt - 1}/{args.retries} — calling {args.model}"
            )
            print(f"{attempt_label}... ", end="", flush=True)

            # Step 1: GPT call
            try:
                hand_json = analyzer.analyze_hand_json(
                    frame_paths=frame_paths,
                    grid_output_path=grid_path,
                    transcript_chunk=chunk,
                    num_players=num_players,
                    bb_ante=args.bb_ante,
                )
            except Exception as exc:
                if attempt < max_attempts:
                    print(f"ERROR (model) — will retry: {exc}")
                    continue
                print(f"ERROR (model): {exc}")
                break

            print("done")
            print(f"  Grid saved: {grid_path}")
            if _debug_grid_path and os.path.exists(grid_path):
                import shutil
                shutil.copy2(grid_path, _debug_grid_path)
                print(f"  Debug grid: {_debug_grid_path}")

            # Step 2: state machine
            try:
                pt4_dict = feed_hand(
                    hand_json,
                    bb_ante=args.bb_ante,
                    stakes="50/100",
                    button_seat=1,
                )
            except FeedError as exc:
                if attempt < max_attempts:
                    print(f"  [retry] State machine: {exc}")
                    continue
                print(f"  [skip] State machine: {exc}")
                break
            except Exception as exc:
                if attempt < max_attempts:
                    print(f"  [retry] State machine unexpected: {exc}")
                    continue
                print(f"  [skip] State machine unexpected: {exc}")
                break

            for entry in pt4_dict.pop("_auto_advance_log", []):
                print(f"  {entry}")

            pt4_dict["timestamp_start"] = now_str

            # Step 3: formatter
            try:
                hand_index = random.randint(0, 9999)
                hh_text = format_hand(pt4_dict, stream_url=video_id, hand_index=hand_index)
            except HandValidationError as exc:
                hh_text = None
                if attempt < max_attempts:
                    print(f"  [retry] Validation: {exc}")
                    continue
                print(f"  [skip] Validation: {exc}")
                break
            except Exception as exc:
                hh_text = None
                if attempt < max_attempts:
                    print(f"  [retry] Formatter error: {exc}")
                    continue
                print(f"  [skip] Formatter error: {exc}")
                break

            break  # success — exit retry loop

        if not hh_text:
            continue

        # ── Write immediately so progress survives a mid-run kill ─────────────
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        is_first_write = not all_hh  # True before we append to all_hh
        if is_first_write and not args.append:
            open_mode = "w"
            prefix    = ""
        else:
            open_mode = "a"
            prefix    = "\n\n\n"
        with open(args.output, open_mode, encoding="utf-8") as fh:
            fh.write(prefix + hh_text)

        all_hh.append(hh_text)
        print()
        print(hh_text)
        print(f"  [saved -> {args.output}]")
        print()

    if all_hh:
        print(f"\n{'Appended' if args.append else 'Saved'} {len(all_hh)} hand(s) to {args.output}")
    else:
        print("\nNo hands produced output.")


if __name__ == "__main__":
    main()
