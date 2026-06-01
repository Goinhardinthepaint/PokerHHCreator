"""
main.py — End-to-end poker hand history pipeline (Gemini video analysis).

Usage:
    python main.py <youtube_url> [options]

    python main.py "https://www.youtube.com/watch?v=p0Q7LW64ecM" \\
        --config src/session/session_config.json

Steps:
    1. Load session config (player roster, stakes, button rotation)
    2. Resolve hand windows (from config["hand_windows"] or hardcoded defaults)
    3. For each window: Gemini analyzes the YouTube clip → JSON
    4. State machine converts JSON → PT4 hand history
    5. Linter validates every hand; only PASS hands written to output
    6. Summary: detected / formatted / linter-passed / written

Hand boundary detection is currently hard-coded (TODO: auto-detect via
a cheap Gemini Flash scan pass).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.pipeline.gemini_pipeline import GeminiPipeline
from src.validate.pt4_linter import lint_hand


# ── Default hand windows ──────────────────────────────────────────────────────
# Hard-coded for https://www.youtube.com/watch?v=p0Q7LW64ecM
# TODO: replace with automatic boundary detection (cheap Gemini Flash scan)
_DEFAULT_HAND_WINDOWS: list[tuple[int, int]] = [
    (2000, 2120),  # hand 1  — 33:20–35:20
    (2120, 2300),  # hand 2  — 35:20–38:20
    (2300, 2520),  # hand 3  — 38:20–42:00
    (2520, 2700),  # hand 4  — 42:00–45:00
    (2700, 2880),  # hand 5  — 45:00–48:00
    (2880, 3060),  # hand 6  — 48:00–51:00
    (3060, 3240),  # hand 7  — 51:00–54:00
    (3240, 3420),  # hand 8  — 54:00–57:00
    (3420, 3600),  # hand 9  — 57:00–60:00
    (3600, 3780),  # hand 10 — 60:00–63:00
]


def _ts(sec: int) -> str:
    return f"{sec // 60}:{sec % 60:02d}"


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Poker hand history pipeline: YouTube URL → validated PT4 hands",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py https://youtu.be/XYZ --config src/session/session_config.json\n"
            "  python main.py https://youtu.be/XYZ --hands 1 3 5   # specific hands only\n"
            "  python main.py https://youtu.be/XYZ --multipass      # 3-pass Gemini verification\n"
        ),
    )
    parser.add_argument("url", help="YouTube stream URL")
    parser.add_argument(
        "--config", default="src/session/session_config.json",
        metavar="PATH",
        help="Session config JSON (default: src/session/session_config.json)",
    )
    parser.add_argument(
        "--output", default="output/hands_validated.txt",
        metavar="PATH",
        help="Output file for passing hands (default: output/hands_validated.txt)",
    )
    parser.add_argument(
        "--multipass", action="store_true",
        help="Enable Gemini 3-pass verification (slower, higher accuracy)",
    )
    parser.add_argument(
        "--hands", nargs="+", type=int, metavar="N",
        help="Process only specific hand number(s), e.g. --hands 1 3 5",
    )
    args = parser.parse_args()

    # ── Load session config ───────────────────────────────────────────────────
    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    # ── Resolve hand windows ──────────────────────────────────────────────────
    # Optional: add "hand_windows": [[start, end], ...] to session_config.json
    # to override the hard-coded defaults for a different stream.
    raw_windows = cfg.get("hand_windows")
    all_windows: list[tuple[int, int]] = (
        [tuple(w) for w in raw_windows] if raw_windows else _DEFAULT_HAND_WINDOWS
    )

    if args.hands:
        hand_windows = [
            (n, all_windows[n - 1])
            for n in sorted(set(args.hands))
            if 1 <= n <= len(all_windows)
        ]
        if not hand_windows:
            print(f"No valid hand numbers in --hands {args.hands} (1–{len(all_windows)} available)")
            sys.exit(1)
    else:
        hand_windows = list(enumerate(all_windows, 1))

    # ── Banner ────────────────────────────────────────────────────────────────
    print(f"Stream   : {args.url}")
    print(f"Config   : {args.config}")
    print(f"Output   : {args.output}")
    print(f"Windows  : {len(hand_windows)} hand(s) to process")
    print(f"Multipass: {'yes' if args.multipass else 'no'}")
    if args.hands:
        print(f"Selection: hands {sorted(args.hands)}")
    print()

    # ── Pipeline ──────────────────────────────────────────────────────────────
    pipeline = GeminiPipeline(args.url, args.config)

    n_detected      = len(hand_windows)
    n_formatted     = 0
    n_passed        = 0
    passed_hands:    list[str]              = []
    pipeline_errors: list[str]             = []
    linter_fails:    list[tuple[int, list]] = []

    for hand_num, (start, end) in hand_windows:
        hhtext, _, err = pipeline.process_hand(
            hand_num, start, end,
            enable_multipass=args.multipass,
        )

        if err or not hhtext:
            pipeline_errors.append(
                f"Hand {hand_num} [{_ts(start)}–{_ts(end)}]: {err or 'no output'}"
            )
            continue

        n_formatted += 1
        result = lint_hand(hhtext, hand_num)
        status = "PASS" if result.passed else "FAIL"
        print(f"  Linter: [{status}]")
        for v in result.violations:
            print(f"    {v}")

        if result.passed:
            n_passed += 1
            passed_hands.append(hhtext)
        else:
            linter_fails.append((hand_num, result.violations))

    # ── Write validated output ────────────────────────────────────────────────
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for hhtext in passed_hands:
            fh.write(hhtext)
            if not hhtext.endswith("\n\n"):
                fh.write("\n\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"  Hands detected  : {n_detected}")
    print(f"  Formatted OK    : {n_formatted}")
    print(f"  Linter PASS     : {n_passed}")
    print(f"  Linter FAIL     : {len(linter_fails)}")
    print(f"  Pipeline errors : {len(pipeline_errors)}")
    print(f"  Written to      : {out_path}  ({n_passed} hand(s))")

    if pipeline_errors:
        print("\nPipeline errors:")
        for e in pipeline_errors:
            print(f"  {e}")

    if linter_fails:
        print("\nLinter failures:")
        for hand_num, viols in linter_fails:
            for v in viols:
                print(f"  Hand {hand_num}: {v}")

    sys.exit(0 if n_passed == n_detected else 1)


if __name__ == "__main__":
    main()
