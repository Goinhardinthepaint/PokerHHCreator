"""
One-call-per-hand analyzer.

Sends a stitched grid of key frames + a transcript slice to Claude and
receives structured JSON, then formats it via pt4_formatter into a
PokerStars-compatible hand history string.

For long hands (>16 frames), splits into multiple 4×4 grids and sends
all grids as separate images in the same API call, each labeled with its
frame range.
"""

import base64
import json
import math
import os
import re
import random
import time

import anthropic
import openai

from src.vision.grid_builder import build_hand_grid
from src.export.pt4_formatter import format_hand


# Friendly alias → (provider, real model id)
# Unknown aliases default to ("openai", alias) — passed through to the API as-is.
_MODEL_MAP = {
    "claude":             ("claude", "claude-sonnet-4-6"),
    "claude-sonnet-4-6":  ("claude", "claude-sonnet-4-6"),
    "gpt4o":              ("openai", "gpt-4o"),
    "gpt-4o":             ("openai", "gpt-4o"),
    "gpt4o-mini":         ("openai", "gpt-4o-mini"),
    "gpt-4o-mini":        ("openai", "gpt-4o-mini"),
    "gpt45":              ("openai", "gpt-4.5-preview"),
    "gpt-4.5":            ("openai", "gpt-4.5-preview"),
    "gpt5.5":             ("openai", "gpt-5.5"),
    "gpt-5.5":            ("openai", "gpt-5.5"),
}

FRAMES_PER_GRID = 16
MAX_GRIDS       = 4   # cap total grids per API call (4 grids × 16 frames = 64 key frames)


# ── Prompt ────────────────────────────────────────────────────────────────────

HAND_PROMPT_JSON = """\
You are transcribing a poker hand from a livestream into structured JSON.

You will receive one or more frame grids, each showing 4×4 frames from the hand
in chronological order (left to right, top to bottom). Each grid is labeled with
its frame range. Together they cover the full hand in order.

The frames show the HCL (Hustler Casino Live) overlay which displays:
- Player names, hole cards, stack sizes, position labels (BTN, SB, BB, UTG, HJ, CO, STR)
- Action text (BET, RAISE, CALL, FOLD, CHECK, ALL-IN, 3-BET)
- Bet amounts and "TO CALL" amounts
- Community board cards in the center
- Pot size on the right
- Stakes on the right

IMPORTANT: The HCL overlay only shows 2-3 players at a time. It cycles through players.
A player not being shown does NOT mean they folded. Reconstruct who is in the hand by
looking across ALL frames and ALL grids.

IMPORTANT: Card suits — RED cards are hearts (h) or diamonds (d) ONLY.
BLACK/DARK cards are spades (s) or clubs (c) ONLY. Never mix.

TRANSCRIPT OF COMMENTARY (may mention actions the overlay doesn't show):
{transcript_chunk}

GAME INFO:
- Stakes: $50/$100 NL Hold'em
- BB Ante: ${bb_ante} (spread as ${ante_per_player} per player across {num_players} players)
- If a player has position label "STR" or posts a forced bet of $200+, that is a straddle
- Straddle can be from ANY position (Mississippi straddle)

OUTPUT: A single JSON object. No preamble, no markdown, no explanation.
The very first character of your response must be "{{".

JSON SCHEMA:
{{
  "players": [
    {{
      "seat": <int 1-9>,
      "name": <string, Title Case real name>,
      "position": <"BTN"|"SB"|"BB"|"UTG"|"HJ"|"CO"|"STR">,
      "stack": <int, stack shown in overlay — do NOT apply ante adjustment>,
      "hole_cards": <list of 2 strings e.g. ["Kd","5c"], or null if not visible>
    }}
  ],
  "action": {{
    "preflop": [ <action objects — see below> ],
    "flop":    [ <action objects> ],
    "turn":    [ <action objects> ],
    "river":   [ <action objects> ]
  }},
  "board": {{
    "flop":  <list of 3 card strings, or null>,
    "turn":  <single card string or null>,
    "river": <single card string or null>,
    "first_run":  {{"flop": [...], "turn": "card", "river": "card"}} or null,
    "second_run": {{"flop": [...], "turn": "card", "river": "card"}} or null
  }},
  "showdown": [
    {{
      "player": <name>,
      "hole_cards": <list of 2 card strings>,
      "hand_description": <e.g. "two pair, Aces and Kings">,
      "result": <"wins" or "loses">,
      "run": <1 or 2 — only present if board was run twice>
    }}
  ]
}}

ACTION OBJECT:
  {{"player": <name>, "action": <type>, "amount": <int or null>}}

ACTION TYPES:
  "posts_ante"     amount = ${ante_per_player} (ante per player)
  "posts_sb"       amount = 50 (small blind)
  "posts_bb"       amount = 100 (big blind)
  "posts_straddle" amount = straddle size
  "folds"          no amount
  "checks"         no amount
  "calls"          amount = chips ADDED to call (not total)
  "bets"           amount = bet size
  "raises"         amount = player's TOTAL street commitment (e.g. raise to $600 → 600)
  "all_in"         amount = player's TOTAL street commitment when going all-in

PREFLOP ARRAY ORDER:
  1. One "posts_ante" per player (${ante_per_player} each), in seat order
  2. "posts_sb" for the SB player
  3. "posts_bb" for the BB player
  4. "posts_straddle" if there is a straddle
  5. Voluntary actions in position order:
     - No straddle: UTG first, clockwise ending at BB
     - With straddle: player left of straddler first, clockwise ending at straddler

POSTFLOP ARRAYS: SB acts first, clockwise to BTN.

IMPORTANT — COMPLETENESS: List EVERY player's action on EVERY betting round,
even if they just check or fold. Do NOT skip any player. Output actions in
strict seat order (position order). If you did not see a player act, they
either folded or checked — include that action explicitly.

PLAYER NAMES:
  - Real names in Title Case: "Dylan", "Airball", "Francisco", "Alex", "Steve", "Greedo"
  - NEVER use position labels as names
  - NEVER ALL CAPS: "Airball" not "AIRBALL"
  - If name unknown, use "Villain"

CARD NOTATION:
  - Ten is ALWAYS "T": "Td", "Th", "Ts", "Tc" (never "10d")
  - Ranks: 2 3 4 5 6 7 8 9 T J Q K A
  - Suits: s h d c
  - Red cards: h or d. Black/dark cards: s or c.

STACK VALUES: Exact overlay value, no commas. Do NOT apply ante adjustments.

IMPORTANT:
  - Only include flop/turn/river if those board cards are visible
  - If a street's cards are not visible, set to null and omit its actions
  - If players are all-in before the river, STILL include ALL board cards that
    were dealt (flop, turn, river) in the board field — even if no betting happens
  - CRITICAL: If 2 or more players are still active after preflop and the hand
    reaches showdown, a board MUST have been dealt. Search ALL frames carefully
    for the community cards. If you can see any cards on the board, include them
    even if you are not 100% certain — your best reading is always better than null.
    Only return null for board.flop if you are certain no flop was dealt.
  - If the board was run twice: put both complete boards in board.first_run and
    board.second_run (each with flop/turn/river keys); set board.flop/turn/river
    to cards dealt before the all-in (null if all-in was preflop); tag each
    showdown entry with "run": 1 or "run": 2 for which board it applies to
  - Include showdown only if hands were revealed; otherwise set showdown to []
  - Output ONLY the JSON object — absolutely nothing before or after it\
"""


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Strip markdown and extract the outermost JSON object from model response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in Claude response")

    depth, end = 0, -1
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        raise ValueError("Unmatched braces in model response JSON")

    return json.loads(text[start:end])


def _build_grids(
    frame_paths: list[str],
    grid_output_path: str,
) -> tuple[list[str], list[str]]:
    """Build one 4×4 grid per 16-frame chunk, capped at MAX_GRIDS total.

    When there are more frames than MAX_GRIDS×FRAMES_PER_GRID, evenly
    sample down so the call never exceeds MAX_GRIDS images.

    Returns (grids_b64, labels). Grid 1 always saves to grid_output_path;
    subsequent chunks get a _part2, _part3, ... suffix.
    """
    base, ext = os.path.splitext(grid_output_path)
    max_frames_total = MAX_GRIDS * FRAMES_PER_GRID
    if len(frame_paths) > max_frames_total:
        step = len(frame_paths) / max_frames_total
        frame_paths = [frame_paths[int(i * step)] for i in range(max_frames_total)]
    chunks = [
        frame_paths[i : i + FRAMES_PER_GRID]
        for i in range(0, len(frame_paths), FRAMES_PER_GRID)
    ]
    total = len(chunks)

    grids_b64: list[str] = []
    labels: list[str] = []

    for idx, chunk in enumerate(chunks, 1):
        if idx == 1:
            chunk_path = grid_output_path
        else:
            chunk_path = f"{base}_part{idx}{ext}"

        build_hand_grid(chunk, chunk_path)
        with open(chunk_path, "rb") as fh:
            grids_b64.append(base64.b64encode(fh.read()).decode())

        start_frame = (idx - 1) * FRAMES_PER_GRID + 1
        end_frame   = start_frame + len(chunk) - 1
        labels.append(f"Grid {idx} of {total} (frames {start_frame}–{end_frame})")

    return grids_b64, labels


# ── Analyzer ──────────────────────────────────────────────────────────────────

class HandAnalyzer:
    """Analyzes a complete hand using a grid of frames + transcript."""

    def __init__(self, model: str = "gpt4o"):
        provider, model_id = _MODEL_MAP.get(model, ("openai", model))
        self.provider = provider
        self.model = model_id
        if provider == "claude":
            self.client = anthropic.Anthropic()
        else:
            self.client = openai.OpenAI()

    def analyze_hand_json(
        self,
        frame_paths: list[str],
        grid_output_path: str,
        transcript_chunk: str,
        num_players: int = 6,
        bb_ante: int = 100,
        stakes: str = "50/100",
    ) -> dict:
        """Call the model; return the raw extracted JSON dict (no formatting).

        The returned dict has the schema expected by hand_feeder.feed_hand():
        players, action{preflop/flop/turn/river}, board, showdown.
        """
        ante_per_player = math.ceil(bb_ante / num_players) if num_players else bb_ante

        grids_b64, labels = _build_grids(frame_paths, grid_output_path)

        prompt = HAND_PROMPT_JSON.format(
            transcript_chunk=transcript_chunk or "(no transcript available)",
            bb_ante=bb_ante,
            ante_per_player=ante_per_player,
            num_players=num_players,
        )

        if self.provider == "claude":
            raw = self._call_claude(grids_b64, labels, prompt)
        else:
            raw = self._call_openai(grids_b64, labels, prompt)

        return _extract_json(raw)

    # ── Provider backends ─────────────────────────────────────────────────────

    def _call_claude(self, grids_b64: list[str], labels: list[str], prompt: str) -> str:
        content = []
        for b64, label in zip(grids_b64, labels):
            content.append({"type": "text", "text": label})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            })
        content.append({"type": "text", "text": prompt})

        messages = [{"role": "user", "content": content}]
        for attempt in range(1, 4):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    messages=messages,
                )
                return response.content[0].text.strip()
            except anthropic.APIStatusError as exc:
                if exc.status_code == 500 and attempt < 3:
                    wait = 10 * attempt
                    print(f"500 error, retrying in {wait}s (attempt {attempt}/3)...",
                          end=" ", flush=True)
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError("Claude call failed after 3 attempts")

    def _call_openai(self, grids_b64: list[str], labels: list[str], prompt: str) -> str:
        content = []
        for b64, label in zip(grids_b64, labels):
            content.append({"type": "text", "text": label})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        content.append({"type": "text", "text": prompt})

        # Use max_completion_tokens (supported by all current OpenAI models).
        # Set 25000 to budget enough for GPT-5.5's internal reasoning tokens (~3500)
        # plus the JSON output (~500-1500 tokens).
        # timeout=600s: GPT-5.5 reasoning on 4 grids can take 4-8 minutes.
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": content}],
            max_completion_tokens=25000,
            timeout=600.0,
        )
        text = response.choices[0].message.content or ""
        if not text.strip():
            finish = response.choices[0].finish_reason
            raise ValueError(
                f"Empty response from {self.model} (finish_reason={finish!r})"
            )
        return text.strip()
