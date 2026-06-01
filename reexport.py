"""
Re-run pipeline + export from existing frames cache (no video download needed).
Usage: python reexport.py <cache_json_path> [--bb-ante AMOUNT]
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.video.pipeline import VideoPipeline, hand_states_to_dicts
from src.export.pt4_formatter import format_session
from src.transcript.parser import parse_vtt

parser = argparse.ArgumentParser(description="Re-export from a cached frames JSON file.")
parser.add_argument(
    "cache_path",
    nargs="?",
    default="output/frames_cache_p0Q7LW64ecM_2000-2120.json",
    help="Path to a frames_cache_*.json file produced by main.py",
)
parser.add_argument(
    "--bb-ante",
    type=int,
    default=0,
    metavar="AMOUNT",
    dest="bb_ante",
    help="Big-blind ante amount (e.g. --bb-ante 100). Spread equally as antes for all players.",
)
parser.add_argument(
    "--transcript",
    default=None,
    metavar="PATH",
    dest="transcript",
    help="Path to .vtt transcript file for action supplementation.",
)
args = parser.parse_args()

cache_path = args.cache_path
url = "https://www.youtube.com/watch?v=p0Q7LW64ecM"
output_path = "output/hands.txt"

print(f"Loading cache: {cache_path}")
with open(cache_path) as fh:
    frame_results = json.load(fh)
print(f"  {len(frame_results)} frames loaded")

# Generate fake timestamps (1 fps, starting at frame 0)
timestamps = [f"00:{i//60:02d}:{i%60:02d}.000" for i in range(len(frame_results))]

transcript_segs = parse_vtt(args.transcript) if args.transcript else None

vp = VideoPipeline()
completed, skipped = vp.process(frame_results, timestamps, transcript_segments=transcript_segs)
print(f"  {len(completed)} hand(s), {len(skipped)} skipped")

# ── Straddle hints: only for the 2000-2120 window ────────────────────────────
# OCR cannot detect Airball's CO straddle in that segment; supply it manually.
# Confirmed by preflop pot=450: SB(50)+BB(100)+straddle(200)+bb_ante(100).
# The hint also carries bb_ante so no session-level override is needed there.
_straddle_hints: list[dict] | None = None
_session_bb_ante = args.bb_ante

if "2000-2120" in cache_path:
    _straddle_hints = [
        {
            "straddle_player": "Airball",
            "straddle_amount": 200,
            "bb_ante": 100,
            "bb_player": "Dylan",
        }
    ]
    _session_bb_ante = 0  # bb_ante carried inside the per-hand hint

hands = hand_states_to_dicts(
    completed,
    stream_url=url,
    hints_list=_straddle_hints,
    bb_ante=_session_bb_ante,
)

raw_path = output_path.replace(".txt", "_raw.json")
with open(raw_path, "w") as f:
    json.dump({"hands": hands, "source": url, "mode": "video"}, f, indent=2)
print(f"  Saved raw JSON to {raw_path}")

txt = format_session(hands, stream_url=url)
with open(output_path, "w") as f:
    f.write(txt)
print(f"  Saved hand history to {output_path}")
print()
print(txt)
