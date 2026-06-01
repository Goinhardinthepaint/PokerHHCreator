"""
Verify a reconstructed hand history against the actual YouTube video using Gemini.

Usage:
    python -m src.verify.gemini_checker <hand_file> <hand_number> <youtube_url> <timestamp_sec>

Example:
    python -m src.verify.gemini_checker output/hands_final.txt 1 https://www.youtube.com/watch?v=p0Q7LW64ecM 2300
"""

import os
import re
import sys

from google import genai
from google.genai import types


def _extract_hand(path: str, hand_number: int) -> str:
    """Return the Nth hand (1-indexed) from a multi-hand file."""
    with open(path, encoding="utf-8") as f:
        content = f.read()

    parts = re.split(r"(?=PokerStars Hand #)", content)
    hands = [p.strip() for p in parts if p.strip().startswith("PokerStars Hand #")]

    if hand_number < 1 or hand_number > len(hands):
        raise ValueError(f"Hand {hand_number} not found — file contains {len(hands)} hand(s)")

    return hands[hand_number - 1]


def verify_hand(
    hand_text: str,
    youtube_url: str,
    timestamp_sec: int,
    model_name: str = "gemini-2.0-flash",
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable not set")

    client = genai.Client(api_key=api_key)

    ts_min = timestamp_sec // 60
    ts_s = timestamp_sec % 60
    timestamp_str = f"{ts_min}:{ts_s:02d} ({timestamp_sec}s)"

    prompt = f"""You are a poker expert verifying a machine-reconstructed hand history against actual video footage.

Please watch the YouTube video provided and focus on the segment starting around timestamp {timestamp_str}.

Answer these specific questions:

1. How many players are seated at the table in that segment?
2. What are the names of all seated players (exactly as shown on the on-screen overlay)?
3. What are the approximate chip stacks for each player?
4. What is the game and stake level shown (e.g. NL Hold'em $50/$100)?

Then compare against our reconstructed hand history below and tell us:
5. Does the number of players match?
6. Do the player names match (exact spelling matters)?
7. Do the chip stacks match (within ~5% is acceptable)?
8. Does the action sequence match what actually happened (preflop raises, calls, folds; flop bets, etc.)?
9. Does the board (community cards) match?
10. Does the winner and final pot size match?

List EVERY discrepancy you find, no matter how small. If something cannot be verified from the video (e.g. hole cards), say so explicitly.

--- BEGIN RECONSTRUCTED HAND HISTORY ---
{hand_text}
--- END RECONSTRUCTED HAND HISTORY ---
"""

    print(f"Sending request to Gemini ({model_name})...")
    print(f"  Video : {youtube_url}")
    print(f"  Time  : {timestamp_str}")
    print(f"  Hand  : {len(hand_text)} chars")
    print()

    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part(
                file_data=types.FileData(
                    file_uri=youtube_url,
                    mime_type="video/*",
                )
            ),
            types.Part(text=prompt),
        ],
    )
    return response.text


def main():
    if len(sys.argv) < 5:
        print("Usage: python -m src.verify.gemini_checker <hand_file> <hand_number> <youtube_url> <timestamp_sec>")
        sys.exit(1)

    hand_file    = sys.argv[1]
    hand_num     = int(sys.argv[2])
    youtube_url  = sys.argv[3]
    timestamp_sec = int(sys.argv[4])

    hand_text = _extract_hand(hand_file, hand_num)
    print(f"=== Hand {hand_num} extracted ({len(hand_text)} chars) ===")
    print(hand_text)
    print()

    result = verify_hand(hand_text, youtube_url, timestamp_sec)

    print("=" * 60)
    print("GEMINI VERIFICATION RESULT")
    print("=" * 60)
    print(result)


if __name__ == "__main__":
    main()
