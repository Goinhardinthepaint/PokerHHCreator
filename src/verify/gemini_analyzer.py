"""
src/verify/gemini_analyzer.py

Ask Gemini to watch a YouTube poker hand and return structured JSON with
player positions, actions, board cards, and outcome.

Usage:
    python -m src.verify.gemini_analyzer
"""

import json
import os

from google import genai
from google.genai import types


HAND_PROMPT = """Watch this YouTube video from timestamp 33:20 to 35:20 (the first poker hand of the session).
This is Hustler Casino Live, $50/$100 NL Hold'em with a $100 BB ante.

Players at the table:
Seat 1: Greedo
Seat 2: Francisco
Seat 3: Dylan
Seat 4: Otto
Seat 5: Alex
Seat 6: Airball

The button is on Seat 1 (Greedo).

Watch carefully and tell me:
1. What position does each player have? (BTN, SB, BB, UTG, HJ, CO)
2. Is there a straddle? If so, who straddles and how much?
3. What are each player's hole cards?
4. What happens preflop? List every action in order (raises, calls, folds, checks).
5. What are the flop cards?
6. What happens on the flop?
7. What are the turn and river cards?
8. What happens on turn and river?
9. Who wins and how much?

Return ONLY valid JSON with this exact structure:
{
  "players": [
    {"seat": 1, "name": "Greedo", "position": "BTN", "hole_cards": ["Xs", "Xh"], "stack": 0}
  ],
  "straddle": {"player": "X", "amount": 0},
  "action": {
    "preflop": [{"player": "X", "action": "raises", "amount": 0}],
    "flop": [],
    "turn": [],
    "river": []
  },
  "board": {"flop": ["Xc", "Xd", "Xh"], "turn": "Xs", "river": "Xd"},
  "showdown": [{"player": "X", "hole_cards": ["X", "X"], "result": "wins"}],
  "pot": 0,
  "notes": "any observations about unclear actions or card reads"
}

Set straddle to null if there is none.
Use standard card notation: rank + suit (2c 3d 4h 5s Tc Jd Qh Ks Ac).
If a value is unknown, use null.
"""

YOUTUBE_URL = "https://www.youtube.com/watch?v=p0Q7LW64ecM"


def analyze_hand(
    youtube_url: str = YOUTUBE_URL,
    prompt: str = HAND_PROMPT,
    model_name: str = "gemini-2.5-flash",
) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable not set")

    client = genai.Client(api_key=api_key)

    print(f"Querying {model_name} with YouTube video...")
    print(f"URL: {youtube_url}")
    print()

    # Specify the 2-minute window (33:20–35:20 = 2000s–2120s) so Gemini
    # doesn't try to process the entire multi-hour stream.
    response = client.models.generate_content(
        model=model_name,
        contents=[
            types.Part(
                file_data=types.FileData(
                    file_uri=youtube_url,
                    mime_type="video/*",
                ),
                video_metadata=types.VideoMetadata(
                    start_offset="2000s",
                    end_offset="2120s",
                ),
            ),
            types.Part(text=prompt),
        ],
    )
    return response.text


def main() -> None:
    import sys
    # Reconfigure stdout to UTF-8 so card suit symbols (♦ ♣ ♥ ♠) print correctly
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    raw = analyze_hand()

    print("=" * 60)
    print("RAW GEMINI RESPONSE")
    print("=" * 60)
    print(raw)
    print()

    # Try to parse as JSON
    import re
    # Strip markdown fences if present
    clean = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
    try:
        data = json.loads(clean)
        print("=" * 60)
        print("PARSED JSON")
        print("=" * 60)
        print(json.dumps(data, indent=2))

        # Save to file
        out_path = "output/gemini_hand1_analysis.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\nSaved to {out_path}")
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}")
        print("(Saving raw response anyway)")
        out_path = "output/gemini_hand1_raw.txt"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(raw)
        print(f"Saved to {out_path}")


if __name__ == "__main__":
    main()
