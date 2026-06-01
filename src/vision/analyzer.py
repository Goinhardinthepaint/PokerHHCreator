"""
Vision frame analyzer.

Sends video frames to Claude's vision API and extracts structured game state:
player stacks, hole cards, board cards, pot size, and action text.
"""

import base64
import io
import json
import re
from pathlib import Path

import anthropic
from PIL import Image


# ── Name normalisation ─────────────────────────────────────────────────────────

def _normalize_name(name: str) -> str:
    """Title-case a player name: 'NIK AIRBALL' → 'Nik Airball'."""
    return name.strip().title() if name else name


def names_match(a: str, b: str) -> bool:
    """Return True if two player names refer to the same player (case-insensitive)."""
    return a.strip().casefold() == b.strip().casefold()


def _normalize_player_names(result: dict) -> dict:
    """Apply title-case normalisation to every player name in a frame result."""
    for player in result.get("players") or []:
        if player.get("name"):
            player["name"] = _normalize_name(player["name"])
    for entry in result.get("showdown") or []:
        if entry.get("player"):
            entry["player"] = _normalize_name(entry["player"])
    return result

FRAME_ANALYSIS_PROMPT = """You are analyzing a frame from a poker livestream. Extract all visible game state information.

Look for and extract:
1. Player names and their stack sizes (usually shown in player boxes at the bottom of the screen).
2. Hole cards for each player (shown face-up next to their name).
3. Board cards (community cards shown in the center).
4. Pot size (usually shown center-right).
5. Current bet amounts (shown near each player).
6. Player positions (BTN, SB, BB, etc. — sometimes labeled).
7. Action text (BET, RAISE, CALL, FOLD, CHECK, ALL-IN).
8. Stakes/game info (usually shown near the pot).

COUNT HOLE CARDS: If any player has 4 hole cards visible instead of 2, this is a PLO bomb pot. Set is_side_game to true and side_game_type to 'plo_bomb_pot'.

Return a JSON object:
```json
{
  "timestamp": "{timestamp}",
  "is_side_game": false,
  "side_game_type": null,
  "players": [
    {
      "name": "PlayerName",
      "stack": 50000,
      "hole_cards": ["Kh", "Qd"],
      "hole_card_count": 2,
      "position": "HJ",
      "current_bet": 1500,
      "action_text": "BET"
    }
  ],
  "board": ["2h", "8d", "3c"],
  "pot": 5250,
  "stakes": "50/100",
  "confidence": 0.95,
  "notes": "any ambiguities or partial reads"
}
```

CRITICAL — Card suits by colour:
- RED cards: suit is ONLY hearts (h) or diamonds (d). Never spades or clubs.
- BLACK/DARK cards: suit is ONLY spades (s) or clubs (c). Never hearts or diamonds.
Hearts = red heart symbol. Diamonds = red diamond/rhombus. Spades = black spade. Clubs = black/dark clover.
If a card is visually red, write h or d. If visually black/dark, write s or c. Never mix colours and suits.

Card notation: Rank + suit letter. A=Ace, K=King, Q=Queen, J=Jack, T=Ten, 9-2 as numbers. s=spades, h=hearts, d=diamonds, c=clubs.

Return ONLY valid JSON."""


# ── Overlay crop helpers ───────────────────────────────────────────────────────
#
# The poker overlay lives in two strips at the bottom of the frame:
#   left  — player name/stack/card boxes  (x: 0–40%, y: 60–100%)
#   right — pot size and stakes display   (x: 70–100%, y: 60–100%)
# Stitching them side-by-side sends ~25% of the original pixels to the API,
# cutting cost and focusing the model on the data-rich regions.

def _crop_and_stitch_path(image_path: str) -> tuple[bytes, str]:
    """Open an image file, crop to overlay regions, return stitched JPEG bytes."""
    img = Image.open(image_path).convert("RGB")
    w, h = img.size
    left  = img.crop((0,             int(h * 0.60), int(w * 0.40), h))
    right = img.crop((int(w * 0.70), int(h * 0.60), w,             h))
    stitched = Image.new("RGB", (left.width + right.width, left.height))
    stitched.paste(left,  (0, 0))
    stitched.paste(right, (left.width, 0))
    buf = io.BytesIO()
    stitched.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


def _crop_and_stitch_array(arr) -> tuple[bytes, str]:
    """Crop a numpy H×W×3 array to overlay regions, return stitched JPEG bytes."""
    import numpy as np
    h, w = arr.shape[:2]
    left  = arr[int(h * 0.60):h, 0:int(w * 0.40)]
    right = arr[int(h * 0.60):h, int(w * 0.70):w]
    stitched_arr = np.concatenate([left, right], axis=1)
    stitched = Image.fromarray(stitched_arr)
    buf = io.BytesIO()
    stitched.save(buf, format="JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


class FrameAnalyzer:
    def __init__(self, model: str = "claude-opus-4-7"):
        self._client = anthropic.Anthropic()
        self._model = model

    def analyze_frame(self, image_input, timestamp: str = "") -> dict:
        """Send a video frame to Claude Vision and return structured game state.

        Args:
            image_input: Path to a JPEG/PNG frame, or a numpy array (H×W×3).
            timestamp:   HH:MM:SS timestamp for the frame (embedded in the prompt).

        Returns:
            Parsed dict matching the FRAME_ANALYSIS_PROMPT schema, or an error dict.
        """
        if isinstance(image_input, str):
            image_data, media_type = _crop_and_stitch_path(image_input)
        else:
            image_data, media_type = _crop_and_stitch_array(image_input)
        b64 = base64.standard_b64encode(image_data).decode("utf-8")

        prompt = FRAME_ANALYSIS_PROMPT.replace("{timestamp}", timestamp)

        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )

        raw = response.content[0].text
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw)

        try:
            return _normalize_player_names(json.loads(raw.strip()))
        except json.JSONDecodeError as exc:
            return {"error": str(exc), "raw": raw[:500], "timestamp": timestamp}
