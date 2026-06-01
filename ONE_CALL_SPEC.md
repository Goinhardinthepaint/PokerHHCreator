# One-Call-Per-Hand Architecture — Implementation Spec

## Overview

REPLACE the current per-frame action inference with a single API call per hand.
The boundary detection (board clearing, pot reset) stays the same.
What changes: instead of 60 individual frame analyses → reconstruct actions,
we send ~15 key frames as a grid image + transcript chunk → get complete HH back.

## How It Works

### Step 1: Boundary Detection (KEEP AS-IS)
Use the existing cheap frame scan to find hand start/end timestamps.
This already works. No changes needed.

### Step 2: For Each Detected Hand

1. Extract ALL frames for that hand's time window
2. Select ~15 key frames spread across the hand:
   - 4 preflop frames (evenly spaced)
   - 4 flop frames
   - 3 turn frames
   - 3 river frames
   - 1 showdown/final frame
3. Crop each frame to the overlay regions (bottom-left + bottom-right)
4. Stitch into a 4x4 grid image (single JPEG)
5. Slice the transcript (.vtt) to this hand's timestamp window
6. Send grid + transcript to Claude/GPT in ONE call
7. Parse the returned hand history text
8. Validate (pot math, card uniqueness, action legality)
9. Export to PT4 format

## The Prompt

```
You are transcribing a poker hand from a livestream into PokerStars hand history format.

Below are 15 frames from the hand in chronological order (left to right, top to bottom in the grid).
The frames show the HCL (Hustler Casino Live) overlay which displays:
- Player names, hole cards, stack sizes, position labels (BTN, SB, BB, UTG, HJ, CO, STR)
- Action text (BET, RAISE, CALL, FOLD, CHECK, ALL-IN, 3-BET)
- Bet amounts and "TO CALL" amounts
- Community board cards in the center
- Pot size on the right
- Stakes on the right

IMPORTANT: The HCL overlay only shows 2-3 players at a time. It cycles through players.
A player not being shown does NOT mean they folded. Reconstruct who is in the hand by
looking across ALL frames.

IMPORTANT: Card suits — RED cards are hearts (h) or diamonds (d) ONLY.
BLACK/DARK cards are spades (s) or clubs (c) ONLY. Never mix.

TRANSCRIPT OF COMMENTARY (may mention actions the overlay doesn't show):
{transcript_chunk}

GAME INFO:
- Stakes: $50/$100 NL Hold'em
- BB Ante: $100 (spread as $17 per player across {num_players} players — adjust stacks accordingly)
- If a player has position label "STR" or posts a forced bet of $200, that's a straddle
- Straddle can be from ANY position (Mississippi straddle)

OUTPUT FORMAT:
Write the complete hand in PokerStars format. Follow these rules EXACTLY:

1. Header: PokerStars Hand #{hand_id}:  Hold'em No Limit ($50/$100 USD) - {timestamp} ET
2. Table: 'Livestream {video_id}' 9-max Seat #{button_seat} is the button
3. Antes: ALL players post $17 ante (antes BEFORE blinds)
4. Blinds: SB $50, BB $100
5. Straddle (if detected): "PlayerName: posts straddle $200" AFTER blinds
6. Stack adjustment: Non-BB players get +$17 added to their visible stack.
   BB player gets -$83 from their visible stack. This makes post-ante stacks match reality.
7. Preflop action order:
   - No straddle: UTG first, clockwise to BB
   - With straddle: start left of straddler, straddler acts last
8. Postflop action order: SB first, clockwise
9. "Dealt to PlayerName [Xs Ys]" for EVERY player with visible hole cards
10. Raise format: "raises $increment to $total" — increment is additional money, total is street commitment
11. Call format: "calls $amount" — amount is money ADDED, not total
12. If a player's stack can't cover a call/raise, they're all-in for their remaining stack
13. Summary: only (button), (small blind), (big blind) position labels. No others.
14. "folded before Flop (didn't bet)" only for players who posted no voluntary money
15. Calculate pot from actual contributions, not from the overlay pot number
16. Evaluate showdown hands: "a pair of Kings", "two pair", "a straight", etc.

DO NOT include any explanation. Output ONLY the hand history text.
```

## Grid Builder

```python
# src/vision/grid_builder.py

from PIL import Image
import os

def build_hand_grid(
    frame_paths: list[str],
    max_frames: int = 16,
    thumb_width: int = 400,
    thumb_height: int = 225,
    cols: int = 4,
) -> str:
    """
    Select key frames, crop to overlay regions, stitch into a grid.
    Returns path to the saved grid JPEG.
    """
    # Select evenly spaced frames if we have more than max_frames
    if len(frame_paths) > max_frames:
        indices = [int(i * len(frame_paths) / max_frames) for i in range(max_frames)]
        selected = [frame_paths[i] for i in indices]
    else:
        selected = frame_paths
    
    rows = (len(selected) + cols - 1) // cols
    grid_w = thumb_width * cols
    grid_h = thumb_height * rows
    grid = Image.new("RGB", (grid_w, grid_h), (0, 0, 0))
    
    for i, path in enumerate(selected):
        img = Image.open(path)
        w, h = img.size
        
        # Crop to overlay regions: bottom-left (0-40%, 55-100%) + bottom-right (70-100%, 55-100%)
        left_crop = img.crop((0, int(h * 0.55), int(w * 0.4), h))
        right_crop = img.crop((int(w * 0.7), int(h * 0.55), w, h))
        
        # Stitch left + right side by side
        combined_w = left_crop.width + right_crop.width
        combined = Image.new("RGB", (combined_w, left_crop.height))
        combined.paste(left_crop, (0, 0))
        combined.paste(right_crop, (left_crop.width, 0))
        
        # Resize to thumbnail
        combined = combined.resize((thumb_width, thumb_height), Image.LANCZOS)
        
        # Place in grid
        col = i % cols
        row = i // cols
        grid.paste(combined, (col * thumb_width, row * thumb_height))
    
    # Save
    grid_path = os.path.join(os.path.dirname(frame_paths[0]), "_hand_grid.jpg")
    grid.save(grid_path, "JPEG", quality=85)
    return grid_path
```

## Per-Hand Analyzer

```python
# src/vision/hand_analyzer.py

import base64
import json
import re
from pathlib import Path

import anthropic

from src.vision.grid_builder import build_hand_grid

HAND_PROMPT = """..."""  # The prompt from above


class HandAnalyzer:
    """Analyzes a complete hand from a grid of frames + transcript."""
    
    def __init__(self, model="claude-sonnet-4-6"):
        self.client = anthropic.Anthropic()
        self.model = model
    
    def analyze_hand(
        self,
        frame_paths: list[str],
        transcript_chunk: str,
        hand_id: str,
        video_id: str,
        timestamp: str,
        num_players: int = 6,
        bb_ante: int = 100,
        stakes: str = "50/100",
    ) -> str:
        """
        Send a grid of frames + transcript to Claude and get back
        the complete hand history as text.
        
        Returns: PokerStars-format hand history string
        """
        # Build grid image
        grid_path = build_hand_grid(frame_paths)
        
        # Encode to base64
        with open(grid_path, "rb") as f:
            grid_b64 = base64.b64encode(f.read()).decode()
        
        # Build prompt
        prompt = HAND_PROMPT.format(
            transcript_chunk=transcript_chunk or "(no transcript available)",
            num_players=num_players,
            hand_id=hand_id,
            video_id=video_id,
            timestamp=timestamp,
            button_seat=1,  # Will be read from frames
        )
        
        # Call API
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": grid_b64,
                        }
                    },
                    {
                        "type": "text",
                        "text": prompt,
                    }
                ]
            }]
        )
        
        # Extract hand history text
        hh_text = response.content[0].text.strip()
        
        # Clean up any markdown fencing
        hh_text = re.sub(r"^```(?:text)?\s*", "", hh_text)
        hh_text = re.sub(r"```\s*$", "", hh_text)
        
        return hh_text
```

## Updated Pipeline Flow

```
main.py receives YouTube URL + options
    │
    ▼
Step 1: Download video + transcript (existing code)
    │
    ▼
Step 2: Extract frames at 1fps (existing code)
    │
    ▼
Step 3: CHEAP boundary scan
    For each frame, just check: board card count + pot
    Use existing _is_visual_boundary logic
    Output: list of (start_frame, end_frame) per hand
    NO per-frame Claude/GPT calls here
    │
    ▼
Step 4: For each detected hand:
    a) Collect frame paths for this hand
    b) Slice transcript to this time window
    c) Build grid image (15 cropped frames stitched together)
    d) Send grid + transcript to Claude/GPT → get HH text back
    e) Validate the returned HH
    f) Append to output
    │
    ▼
Step 5: Write all hands to output/hands.txt
```

## Key Changes from Current Architecture

1. DELETE: _infer_actions_from_frames() — no longer needed
2. DELETE: per-frame FrameAnalyzer calls in main.py — replaced by per-hand calls
3. KEEP: Frame extraction (extract_frames)
4. KEEP: Boundary detection logic (_is_visual_boundary, board clearing, pot reset)
5. KEEP: PT4 format validation
6. KEEP: Transcript parsing
7. NEW: grid_builder.py — stitches frames into grid
8. NEW: hand_analyzer.py — one-call-per-hand using grid + transcript
9. MODIFY: main.py — use boundary detection + hand_analyzer instead of frame-by-frame

## Boundary Detection Without Per-Frame API Calls

The current boundary detection relies on per-frame API results (board count, pot).
Without those, we need LOCAL boundary detection from raw frames.

Option A: Use OpenCV to detect board card changes locally (free, fast)
- Compare bottom-center region of consecutive frames
- When this region changes significantly AND the overlay regions change = new street
- When board region goes blank for 3+ frames = new hand

Option B: Use the transcript for boundaries (free)
- "New hand", "button moves", "posts the blind" = hand start
- This is what we originally tried but only found 18/200+ hands

Option C: Cheap API scan at 0.2fps
- Send every 5th frame to GPT-4o mini with minimal prompt
- "How many board cards? What's the pot?" 
- $0.001 per frame, $6 for 8 hours
- This is the two-pass segmenter approach

RECOMMENDATION: Option C (cheap scan) is most reliable. But for the MVP,
we can use the EXISTING per-frame results from the 1800-frame run we already
paid for. The cached frames_cache JSON already has board counts and pot values.
Use those for boundary detection, then do one-call-per-hand for the action inference.

## Cost Comparison

For 30 minutes of stream (~15 hands):
- Current: 1800 frames × $0.015 = $27.00
- One-call: 15 hands × $0.05 = $0.75
- With cheap boundary scan: 360 frames × $0.002 + 15 × $0.05 = $1.47

For full 8-hour stream (~150 hands):
- Current: would be $400+
- One-call: 150 × $0.05 = $7.50
- With boundary scan: $6 + $7.50 = $13.50

## Testing

Use the existing cached data:
1. Load frames_cache_p0Q7LW64ecM_2300-4100.json
2. Use board counts to find hand boundaries (already in the cache)
3. For each hand, collect the frame JPEGs from downloads/frames/
4. Build grid, call hand_analyzer
5. Compare output to what we know from watching the stream

This tests the new approach WITHOUT any new frame extraction or boundary API calls.
